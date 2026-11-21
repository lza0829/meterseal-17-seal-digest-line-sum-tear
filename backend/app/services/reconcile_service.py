from __future__ import annotations
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
    get_seal_mode,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_align(period_id: str, building: str | None = None) -> dict:
    mode = get_seal_mode()
    # 分区架构下，对齐必须收敛到具体楼栋，避免跨栋取数、跨栋过期别人的开放批
    if mode == SEAL_MODE_BUILDING and not building:
        raise HTTPException(400, "分区封印架构下对齐必须指定楼栋 building")
    conn = connect()
    try:
        # 立即事务：过期旧批 + 建新批 + 明细一把落库，并发装载/封印不会看见半截批
        conn.execute("BEGIN IMMEDIATE")
        if building is not None:
            gate.assert_building_known(conn, building)
            assert_building_writable(conn, period_id, building)
        elif gate.is_period_sealed(conn, period_id):
            raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")

        # 正式键真源：批头计数与 digest 都从正式估抄∪实抄对齐行重算
        m = seal_metrics.compute_metrics(conn, period_id, building)
        if m["formal_keys"] == 0:
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

        batch_id = "B-" + uuid.uuid4().hex[:10]
        scope = SEAL_MODE_BUILDING if building is not None else SEAL_MODE_PERIOD
        conn.execute(
            """INSERT INTO reconcile_batches
               (batch_id,period_id,status,matched_count,variance_count,fingerprint,created_at,sealed_scope,building)
               VALUES (?,?, 'open',?,?,?,?,?,?)""",
            (
                batch_id,
                period_id,
                m["matched_count"],
                m["variance_count"],
                m["digest"],
                _now(),
                scope,
                building,
            ),
        )
        # 对齐明细与批头同源：同一份正式行派生，不存在两套行集合
        seal_metrics.rebuild_batch_detail(conn, batch_id, period_id, m["rows"])
        conn.commit()
        return {
            "batch_id": batch_id,
            "period_id": period_id,
            "building": building,
            "sealed_scope": scope,
            "matched_count": m["matched_count"],
            "variance_count": m["variance_count"],
            "fingerprint": m["digest"],
            "status": "open",
        }
    except Exception:
        conn.rollback()
        raise
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


def _seal_scope(conn, period_id: str, building: str | None, batch) -> dict:
    """封印落库核心（调用方已持写事务）：按正式行重算并回写批头/明细/digest/投影。

    正式键架构的封印语义：批头覆盖与 digest 以正式估抄∪实抄对齐键集合为准，
    封印时刻重算落库 —— 回执数字即可被正式键重放，不是另一套。
    """
    bid = batch["batch_id"]
    m = seal_metrics.compute_metrics(conn, period_id, building)
    if m["formal_keys"] == 0:
        # 禁止无正式行当封印凭证：空行集合不得落成 sealed 现行
        raise HTTPException(409, "正式估抄/实抄行集合为空，禁止以空行集合充当封印凭证")
    seal_metrics.rebuild_batch_detail(conn, bid, period_id, m["rows"])
    conn.execute(
        """UPDATE reconcile_batches
           SET status='sealed', matched_count=?, variance_count=?, fingerprint=?
           WHERE batch_id=?""",
        (m["matched_count"], m["variance_count"], m["digest"], bid),
    )
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
        (bid,),
    ).fetchone()["c"]
    return {
        "batch_id": bid,
        "matched_count": m["matched_count"],
        "variance_count": m["variance_count"],
        "coverage": m["coverage"],
        "digest": m["digest"],
        "pending_left": pending,
    }


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
            sealed_at = _now()
            metrics = _seal_scope(conn, period_id, None, batch)
            # 一把封关闭该 period 全部表的写闸
            conn.execute(
                "UPDATE periods SET status='sealed', sealed_at=?, batch_no=? WHERE period_id=?",
                (sealed_at, metrics["batch_id"], period_id),
            )
            seal_metrics.persist_period_projection(conn, period_id)
            conn.commit()
            return {
                "period_id": period_id,
                "building": None,
                "sealed_scope": SEAL_MODE_PERIOD,
                "batch_id": metrics["batch_id"],
                "sealed_at": sealed_at,
                "pending_left": metrics["pending_left"],
                "status": "sealed",
                "coverage": metrics["coverage"],
                "digest": metrics["digest"],
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
        sealed_at = _now()
        metrics = _seal_scope(conn, period_id, building, batch)
        # 主键 (period_id, building) 兜底并发：同栋重复封直接冲突，不会产生第二条/半截记录
        conn.execute(
            """INSERT INTO sealed_buildings(period_id,building,batch_id,sealed_at)
               VALUES (?,?,?,?)""",
            (period_id, building, metrics["batch_id"], sealed_at),
        )
        seal_metrics.persist_period_projection(conn, period_id)
        conn.commit()
        return {
            "period_id": period_id,
            "building": building,
            "sealed_scope": SEAL_MODE_BUILDING,
            "batch_id": metrics["batch_id"],
            "sealed_at": sealed_at,
            "pending_left": metrics["pending_left"],
            "status": "sealed",
            "coverage": metrics["coverage"],
            "digest": metrics["digest"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_period(period_id: str) -> dict:
    """封印度量核对：以正式键集合重放该账期全部 sealed 批，收敛库内分叉。

    - 批头计数 / digest / 对齐明细与正式行重放不符 → 同事务重算回写（repaired）；
    - 正式空而批头非零 → 作废该 sealed 现行并回滚半截明细（voided）；
    - 全部一致 → ok。回执数字对齐不算过关，库内四分叉消除才算。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        period = conn.execute(
            "SELECT * FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            raise HTTPException(404, "账期不存在")
        sealed = conn.execute(
            "SELECT * FROM reconcile_batches WHERE period_id=? AND status='sealed' ORDER BY batch_id",
            (period_id,),
        ).fetchall()
        actions = []
        for b in sealed:
            building = b["building"] if b["sealed_scope"] == "building" else None
            m = seal_metrics.compute_metrics(conn, period_id, building)
            header_nonzero = int(b["matched_count"] or 0) + int(b["variance_count"] or 0) > 0
            if m["formal_keys"] == 0 and header_nonzero:
                seal_metrics.void_sealed_batch(conn, b)
                actions.append(
                    {
                        "batch_id": b["batch_id"],
                        "building": building,
                        "action": "voided",
                        "reason": "正式行集合为空而批头非零，作废 sealed 现行并回滚明细",
                    }
                )
                continue
            consistent = (
                int(b["matched_count"] or 0) == m["matched_count"]
                and int(b["variance_count"] or 0) == m["variance_count"]
                and (b["fingerprint"] or "") == m["digest"]
                and seal_metrics.detail_consistent(conn, b["batch_id"], m["rows"])
            )
            if consistent:
                actions.append(
                    {
                        "batch_id": b["batch_id"],
                        "building": building,
                        "action": "ok",
                        "digest": m["digest"],
                    }
                )
                continue
            before = b["fingerprint"]
            seal_metrics.rebuild_batch_detail(conn, b["batch_id"], period_id, m["rows"])
            conn.execute(
                """UPDATE reconcile_batches
                   SET matched_count=?, variance_count=?, fingerprint=?
                   WHERE batch_id=?""",
                (m["matched_count"], m["variance_count"], m["digest"], b["batch_id"]),
            )
            actions.append(
                {
                    "batch_id": b["batch_id"],
                    "building": building,
                    "action": "repaired",
                    "digest_before": before,
                    "digest": m["digest"],
                    "matched_count": m["matched_count"],
                    "variance_count": m["variance_count"],
                    "coverage": m["coverage"],
                }
            )
        projection = seal_metrics.persist_period_projection(conn, period_id)
        conn.commit()
        return {"period_id": period_id, "actions": actions, "projection": projection}
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
