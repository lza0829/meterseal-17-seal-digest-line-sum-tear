from __future__ import annotations
import hashlib
import uuid
from datetime import datetime, timezone
from app.services import seal_metrics

from fastapi import HTTPException

from app.db import connect
from app.services import gate
from app.services.gate import (
    SEAL_MODE_BUILDING,
    SEAL_MODE_PERIOD,
    assert_building_writable,
    assert_period_writable,
    get_seal_mode,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _meter_ids(conn, period_id: str, building: str | None) -> list[str]:
    if building is None:
        rows = conn.execute(
            """SELECT DISTINCT m.meter_id FROM meters m
               WHERE m.meter_id IN (
                 SELECT meter_id FROM estimates WHERE period_id=?
                 UNION SELECT meter_id FROM actuals WHERE period_id=?
               ) ORDER BY m.meter_id""",
            (period_id, period_id),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT m.meter_id FROM meters m
               WHERE m.building=? AND m.meter_id IN (
                 SELECT meter_id FROM estimates WHERE period_id=?
                 UNION SELECT meter_id FROM actuals WHERE period_id=?
               ) ORDER BY m.meter_id""",
            (building, period_id, period_id),
        ).fetchall()
    return [r["meter_id"] for r in rows]


def run_align(period_id: str, building: str | None = None) -> dict:
    mode = get_seal_mode()
    # 分区架构下，对齐必须收敛到具体楼栋，避免跨栋取数、跨栋过期别人的开放批
    if mode == SEAL_MODE_BUILDING and not building:
        raise HTTPException(400, "分区封印架构下对齐必须指定楼栋 building")
    conn = connect()
    try:
        if building is not None:
            gate.assert_building_known(conn, building)
            assert_building_writable(conn, period_id, building)
        else:
            assert_period_writable(period_id)

        meter_ids = _meter_ids(conn, period_id, building)
        if not meter_ids:
            raise HTTPException(400, "该范围内无估抄/实抄可对齐")

        # 只过期“同作用域”的开放批：分区批按楼栋隔离，整期批按空楼栋隔离
        if building is None:
            conn.execute(
                """UPDATE reconcile_batches SET status='expired'
                   WHERE period_id=? AND status='open' AND sealed_scope='period' AND building IS NULL""",
                (period_id,),
            )
        else:
            conn.execute(
                """UPDATE reconcile_batches SET status='expired'
                   WHERE period_id=? AND status='open' AND sealed_scope='building' AND building=?""",
                (period_id, building),
            )

        placeholders = ",".join("?" for _ in meter_ids)
        est = {
            r["meter_id"]: r
            for r in conn.execute(
                f"SELECT * FROM estimates WHERE period_id=? AND meter_id IN ({placeholders})",
                (period_id, *meter_ids),
            ).fetchall()
        }
        act = {
            r["meter_id"]: r
            for r in conn.execute(
                f"SELECT * FROM actuals WHERE period_id=? AND meter_id IN ({placeholders})",
                (period_id, *meter_ids),
            ).fetchall()
        }
        keys = sorted(set(est) | set(act))

        batch_id = "B-" + uuid.uuid4().hex[:10]
        matched = 0
        variances = []
        for mid in keys:
            eq = est[mid]["qty"] if mid in est else None
            aq = act[mid]["qty"] if mid in act else None
            if eq is not None and aq is not None and abs(eq - aq) < 1e-9:
                matched += 1
                continue
            delta = (aq if aq is not None else 0.0) - (eq if eq is not None else 0.0)
            variances.append(
                (
                    "V-" + uuid.uuid4().hex[:10],
                    batch_id,
                    mid,
                    period_id,
                    eq,
                    aq,
                    delta,
                    "pending",
                )
            )

        scope = SEAL_MODE_BUILDING if building is not None else SEAL_MODE_PERIOD
        fp_src = f"{period_id}|{scope}|{building or ''}|{matched}|{len(variances)}|" + ",".join(keys)
        fingerprint = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
        conn.execute(
            """INSERT INTO reconcile_batches
               (batch_id,period_id,status,matched_count,variance_count,fingerprint,created_at,sealed_scope,building)
               VALUES (?,?, 'open',?,?,?,?,?,?)""",
            (batch_id, period_id, matched, len(variances), fingerprint, _now(), scope, building),
        )
        if variances:
            conn.executemany(
                """INSERT INTO variances(variance_id,batch_id,meter_id,period_id,estimate_qty,actual_qty,delta,fate)
                   VALUES (?,?,?,?,?,?,?,?)""",
                variances,
            )
        conn.commit()
        return {
            "batch_id": batch_id,
            "period_id": period_id,
            "building": building,
            "sealed_scope": scope,
            "matched_count": matched,
            "variance_count": len(variances),
            "fingerprint": fingerprint,
            "status": "open",
        }
    finally:
        conn.close()


def _validate_batch(conn, period_id: str, batch_id: str | None, building: str | None) -> dict:
    scope = SEAL_MODE_BUILDING if building is not None else SEAL_MODE_PERIOD
    if batch_id:
        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if not batch or batch["period_id"] != period_id:
            raise HTTPException(400, "对账批与账期不匹配")
        if batch["sealed_scope"] != scope or (batch["building"] or None) != building:
            raise HTTPException(400, "对账批作用域与封印请求不一致")
        if batch["status"] != "open":
            raise HTTPException(409, "对账批不可用")
    else:
        if building is None:
            batch = conn.execute(
                """SELECT * FROM reconcile_batches WHERE period_id=? AND status='open'
                   AND sealed_scope='period' AND building IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (period_id,),
            ).fetchone()
        else:
            batch = conn.execute(
                """SELECT * FROM reconcile_batches WHERE period_id=? AND status='open'
                   AND sealed_scope='building' AND building=?
                   ORDER BY created_at DESC LIMIT 1""",
                (period_id, building),
            ).fetchone()
        if not batch:
            raise HTTPException(400, "无匹配作用域的 open 对账批，请先对齐")
        batch_id = batch["batch_id"]
    return conn.execute("SELECT * FROM reconcile_batches WHERE batch_id=?", (batch_id,)).fetchone()


def seal_period(period_id: str, batch_id: str | None = None, building: str | None = None) -> dict:
    mode = get_seal_mode()
    # 入口口径分叉：分区架构必须带楼栋；整期架构禁止带楼栋宣称分区封
    if mode == SEAL_MODE_BUILDING and not building:
        raise HTTPException(400, "分区封印架构下封印请求必须带 building")
    if mode == SEAL_MODE_PERIOD and building:
        raise HTTPException(400, "当前为整账期封印架构，不接受楼栋分区封印")

    conn = connect()
    try:
        # 立即事务：BEGIN IMMEDIATE 抢写锁，并发两栋分区封串行化，杜绝互相覆盖/半截批
        conn.execute("BEGIN IMMEDIATE")
        period = conn.execute(
            "SELECT * FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            raise HTTPException(404, "账期不存在")

        if mode == SEAL_MODE_PERIOD:
            if period["status"] == "sealed":
                raise HTTPException(409, "账期已封印")
            batch = _validate_batch(conn, period_id, batch_id, None)
            bid = batch["batch_id"]
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
                (bid,),
            ).fetchone()["c"]
            sealed_at = _now()
            # 一把封关闭该 period 全部表的写闸
            conn.execute(
                "UPDATE periods SET status='sealed', sealed_at=?, batch_no=? WHERE period_id=?",
                (sealed_at, bid, period_id),
            )
            conn.execute(
                "UPDATE reconcile_batches SET status='sealed' WHERE batch_id=?", (bid,)
            )
            metrics = seal_metrics.paint_after_seal(conn, period_id, bid, None)
            conn.commit()
            return {
                "period_id": period_id,
                "building": None,
                "sealed_scope": SEAL_MODE_PERIOD,
                "batch_id": bid,
                "sealed_at": sealed_at,
                "pending_left": pending,
                "status": "sealed",
                "coverage": metrics.get("coverage"),
                "digest": metrics.get("digest"),
            }

        # 分区架构
        gate.assert_building_known(conn, building)
        if period["status"] == "sealed":
            raise HTTPException(409, "账期已整期封印")
        existing = conn.execute(
            "SELECT 1 FROM sealed_buildings WHERE period_id=? AND building=?",
            (period_id, building),
        ).fetchone()
        if existing:
            raise HTTPException(409, f"楼栋 {building} 在账期 {period_id} 已分区封印")
        batch = _validate_batch(conn, period_id, batch_id, building)
        bid = batch["batch_id"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
            (bid,),
        ).fetchone()["c"]
        sealed_at = _now()
        # 主键 (period_id, building) 兜底并发：同栋重复封直接冲突，不会产生第二条/半截记录
        conn.execute(
            """INSERT INTO sealed_buildings(period_id,building,batch_id,sealed_at)
               VALUES (?,?,?,?)""",
            (period_id, building, bid, sealed_at),
        )
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed' WHERE batch_id=?", (bid,)
        )
        metrics = seal_metrics.paint_after_seal(conn, period_id, bid, building)
        conn.commit()
        return {
            "period_id": period_id,
            "building": building,
            "sealed_scope": SEAL_MODE_BUILDING,
            "batch_id": bid,
            "sealed_at": sealed_at,
            "pending_left": pending,
            "status": "sealed",
            "coverage": metrics.get("coverage"),
            "digest": metrics.get("digest"),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_fate(variance_id: str, fate: str) -> dict:
    if fate not in ("pending", "accept_short", "accept_over", "quarantine"):
        raise HTTPException(400, "非法命运")
    conn = connect()
    try:
        row = conn.execute(
            """SELECT v.*, p.status AS period_status, m.building
               FROM variances v
               JOIN periods p ON p.period_id=v.period_id
               JOIN meters m ON m.meter_id=v.meter_id
               WHERE v.variance_id=?""",
            (variance_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "差额不存在")
        # 写闸同口径：整期封拒一切；分区架构下该栋已封也拒改命运
        assert_building_writable(conn, row["period_id"], row["building"])
        conn.execute("UPDATE variances SET fate=? WHERE variance_id=?", (fate, variance_id))
        conn.commit()
        return {"variance_id": variance_id, "fate": fate}
    finally:
        conn.close()
