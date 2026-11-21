"""封印度量唯一真源：正式估抄表 ∪ 实抄表的对齐键集合。

全局贯彻「正式键架构」：
  - 批头 matched/variance、覆盖率、digest 都是正式行集合的纯函数，可重放；
  - digest 规范化序列化承诺到「键集合 + 数量」，天然等于对齐明细哈希，不会与明细分叉；
  - 任何改动正式行或封印状态的事务，必须同事务重算并回写批头 / 明细 / digest / 总览投影；
  - 正式空而批头非零 → 作废该 sealed 现行并回滚半截明细（void_sealed_batch）。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

CACHE_PREFIX = "seal_metrics:"  # 每账期一份总览投影：seal_metrics:{period_id}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fmt_qty(q) -> str:
    return "" if q is None else repr(float(q))


def _is_matched(est_qty, act_qty) -> bool:
    return est_qty is not None and act_qty is not None and abs(est_qty - act_qty) < 1e-9


def aligned_rows(conn, period_id: str, building: str | None) -> list[dict]:
    """正式估抄∪实抄对齐行集合 —— 全系统唯一口径，对齐/封印/补抄/核对都走这里。

    作用域内注册表具中，凡有正式估抄或实抄行的表具占一行；同一表具多张单据
    取最新装载（loaded_at, rowid 顺序末位），保证重放确定。返回按 meter_id 排序。
    """
    if building is None:
        meters = [
            r["meter_id"]
            for r in conn.execute("SELECT meter_id FROM meters ORDER BY meter_id").fetchall()
        ]
    else:
        meters = [
            r["meter_id"]
            for r in conn.execute(
                "SELECT meter_id FROM meters WHERE building=? ORDER BY meter_id", (building,)
            ).fetchall()
        ]
    if not meters:
        return []
    ph = ",".join("?" for _ in meters)
    est: dict[str, float] = {}
    for r in conn.execute(
        f"SELECT meter_id, qty FROM estimates WHERE period_id=? AND meter_id IN ({ph})"
        " ORDER BY loaded_at, rowid",
        (period_id, *meters),
    ).fetchall():
        est[r["meter_id"]] = r["qty"]
    act: dict[str, float] = {}
    for r in conn.execute(
        f"SELECT meter_id, qty FROM actuals WHERE period_id=? AND meter_id IN ({ph})"
        " ORDER BY loaded_at, rowid",
        (period_id, *meters),
    ).fetchall():
        act[r["meter_id"]] = r["qty"]
    keys = sorted(set(est) | set(act))
    return [{"meter_id": k, "est_qty": est.get(k), "act_qty": act.get(k)} for k in keys]


def _scope_meter_count(conn, building: str | None) -> int:
    if building is None:
        return conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) AS c FROM meters WHERE building=?", (building,)
    ).fetchone()["c"]


def compute_metrics(conn, period_id: str, building: str | None) -> dict:
    """从正式行集合重算封印度量：批头计数、覆盖率、digest。纯函数，可重放。"""
    rows = aligned_rows(conn, period_id, building)
    matched = sum(1 for r in rows if _is_matched(r["est_qty"], r["act_qty"]))
    variance = len(rows) - matched
    scope_total = _scope_meter_count(conn, building)
    coverage = round(100 * len(rows) / scope_total) if scope_total else 0
    scope = "building" if building is not None else "period"
    canonical = ";".join(
        f"{r['meter_id']}:{_fmt_qty(r['est_qty'])}:{_fmt_qty(r['act_qty'])}" for r in rows
    )
    digest_src = f"{period_id}|{scope}|{building or ''}|{canonical}"
    digest = hashlib.sha256(digest_src.encode()).hexdigest()[:16]
    return {
        "rows": rows,
        "matched_count": matched,
        "variance_count": variance,
        "coverage": coverage,
        "digest": digest,
        "formal_keys": len(rows),
        "scope_meters": scope_total,
    }


def rebuild_batch_detail(conn, batch_id: str, period_id: str, rows: list[dict]) -> None:
    """按正式行重建批的对齐明细：存活键保留 fate，新键 pending，消失键删除。

    调用方必须持写事务；重建后 variances ≡ 正式行派生明细，不再各说各话。
    """
    existing = {
        r["meter_id"]: r
        for r in conn.execute(
            "SELECT * FROM variances WHERE batch_id=?", (batch_id,)
        ).fetchall()
    }
    wanted = {
        r["meter_id"]: r for r in rows if not _is_matched(r["est_qty"], r["act_qty"])
    }
    for mid in sorted(set(existing) - set(wanted)):
        conn.execute(
            "DELETE FROM variances WHERE variance_id=?", (existing[mid]["variance_id"],)
        )
    for mid in sorted(set(existing) & set(wanted)):
        w = wanted[mid]
        delta = (w["act_qty"] if w["act_qty"] is not None else 0.0) - (
            w["est_qty"] if w["est_qty"] is not None else 0.0
        )
        conn.execute(
            "UPDATE variances SET estimate_qty=?, actual_qty=?, delta=? WHERE variance_id=?",
            (w["est_qty"], w["act_qty"], delta, existing[mid]["variance_id"]),
        )
    for mid in sorted(set(wanted) - set(existing)):
        w = wanted[mid]
        delta = (w["act_qty"] if w["act_qty"] is not None else 0.0) - (
            w["est_qty"] if w["est_qty"] is not None else 0.0
        )
        conn.execute(
            """INSERT INTO variances(variance_id,batch_id,meter_id,period_id,estimate_qty,actual_qty,delta,fate)
               VALUES (?,?,?,?,?,?,?, 'pending')""",
            (
                "V-" + uuid.uuid4().hex[:10],
                batch_id,
                mid,
                period_id,
                w["est_qty"],
                w["act_qty"],
                delta,
            ),
        )


def detail_consistent(conn, batch_id: str, rows: list[dict]) -> bool:
    """库内对齐明细是否与正式行派生明细逐键一致（数量也算）。"""
    existing = {
        r["meter_id"]: r
        for r in conn.execute(
            "SELECT * FROM variances WHERE batch_id=?", (batch_id,)
        ).fetchall()
    }
    wanted = {
        r["meter_id"]: r for r in rows if not _is_matched(r["est_qty"], r["act_qty"])
    }
    if set(existing) != set(wanted):
        return False
    for mid, w in wanted.items():
        e = existing[mid]
        if e["estimate_qty"] != w["est_qty"] or e["actual_qty"] != w["act_qty"]:
            return False
    return True


def _projection_key(period_id: str) -> str:
    return f"{CACHE_PREFIX}{period_id}"


def persist_period_projection(conn, period_id: str) -> dict | None:
    """把该账期全部 sealed 批的聚合投影落库（调用方事务内）。无 sealed 批则清除投影。

    投影只是已落库批头/digest 的汇总读视图，绝不虚构：覆盖=已封作用域正式键/作用域表具。
    """
    sealed = conn.execute(
        "SELECT * FROM reconcile_batches WHERE period_id=? AND status='sealed' ORDER BY batch_id",
        (period_id,),
    ).fetchall()
    if not sealed:
        conn.execute("DELETE FROM settings WHERE key=?", (_projection_key(period_id),))
        return None
    covered = 0
    scope_total = 0
    batches = []
    for b in sealed:
        building = b["building"] if b["sealed_scope"] == "building" else None
        m = compute_metrics(conn, period_id, building)
        covered += m["formal_keys"]
        scope_total += m["scope_meters"]
        batches.append(
            {
                "batch_id": b["batch_id"],
                "building": building,
                "matched_count": m["matched_count"],
                "variance_count": m["variance_count"],
                "digest": m["digest"],
            }
        )
    digests = sorted(b["digest"] for b in batches)
    digest = (
        digests[0]
        if len(digests) == 1
        else hashlib.sha256("|".join(digests).encode()).hexdigest()[:16]
    )
    formal_est = conn.execute(
        "SELECT COUNT(DISTINCT meter_id) AS c FROM estimates WHERE period_id=?", (period_id,)
    ).fetchone()["c"]
    formal_act = conn.execute(
        "SELECT COUNT(DISTINCT meter_id) AS c FROM actuals WHERE period_id=?", (period_id,)
    ).fetchone()["c"]
    payload = {
        "period_id": period_id,
        "coverage": round(100 * covered / scope_total) if scope_total else 0,
        "digest": digest,
        "batches": batches,
        "formal_estimate_keys": formal_est,
        "formal_actual_keys": formal_act,
        "refreshed_at": _now(),
    }
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_projection_key(period_id), json.dumps(payload, ensure_ascii=False)),
    )
    return payload


def overview_projection(conn, period_id: str) -> dict | None:
    """读该账期的总览投影；没有就是没有，不造假。"""
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?", (_projection_key(period_id),)
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def refresh_sealed_batch(conn, batch) -> dict:
    """同事务内按正式行重算并回写该 sealed 批：对齐明细、批头、digest、总览投影。

    正式空而批头非零 → 作废该 sealed 现行并回滚半截明细。
    """
    building = batch["building"] if batch["sealed_scope"] == "building" else None
    m = compute_metrics(conn, batch["period_id"], building)
    header_nonzero = int(batch["matched_count"] or 0) + int(batch["variance_count"] or 0) > 0
    if m["formal_keys"] == 0 and header_nonzero:
        void_sealed_batch(conn, batch)
        return {
            "action": "voided",
            "batch_id": batch["batch_id"],
            "period_id": batch["period_id"],
            "building": building,
        }
    rebuild_batch_detail(conn, batch["batch_id"], batch["period_id"], m["rows"])
    conn.execute(
        "UPDATE reconcile_batches SET matched_count=?, variance_count=?, fingerprint=? WHERE batch_id=?",
        (m["matched_count"], m["variance_count"], m["digest"], batch["batch_id"]),
    )
    persist_period_projection(conn, batch["period_id"])
    return {
        "action": "refreshed",
        "batch_id": batch["batch_id"],
        "period_id": batch["period_id"],
        "building": building,
        "matched_count": m["matched_count"],
        "variance_count": m["variance_count"],
        "coverage": m["coverage"],
        "digest": m["digest"],
        "formal_keys": m["formal_keys"],
    }


def void_sealed_batch(conn, batch) -> None:
    """作废 sealed 现行：批 → voided，回滚其对齐明细，解开账期/楼栋封印，清总览投影。

    并发半截与空凭证的唯一出口：不留第二份现行，也不留空 digest 的 sealed 批。
    """
    bid = batch["batch_id"]
    conn.execute("DELETE FROM variances WHERE batch_id=?", (bid,))
    conn.execute(
        "UPDATE reconcile_batches SET status='voided', matched_count=0, variance_count=0,"
        " fingerprint=NULL WHERE batch_id=?",
        (bid,),
    )
    if batch["sealed_scope"] == "building":
        conn.execute(
            "DELETE FROM sealed_buildings WHERE period_id=? AND building=?",
            (batch["period_id"], batch["building"]),
        )
    else:
        conn.execute(
            "UPDATE periods SET status='open', sealed_at=NULL, batch_no=NULL"
            " WHERE period_id=? AND batch_no=?",
            (batch["period_id"], bid),
        )
    persist_period_projection(conn, batch["period_id"])
