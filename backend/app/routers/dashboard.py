from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import get_seal_mode, get_setting
from app.services import seal_metrics

router = APIRouter(tags=["dashboard"])


@router.get("/health")
def health():
    return {"ok": True, "product": "抄表簿"}


@router.get("/overview")
def overview():
    conn = connect()
    try:
        open_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='open'"
        ).fetchone()["c"]
        sealed_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='sealed'"
        ).fetchone()["c"]
        meters = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE fate='pending'"
        ).fetchone()["c"]
        current = get_setting("current_period", "")
        seal_mode = get_seal_mode()
        buildings_total = conn.execute(
            "SELECT COUNT(DISTINCT building) AS c FROM meters"
        ).fetchone()["c"]
        # 分区计数与整期计数并列，前端按当前架构只呈现对应口径，不混用
        sealed_building_count = conn.execute(
            "SELECT COUNT(*) AS c FROM sealed_buildings WHERE period_id=?",
            (current,),
        ).fetchone()["c"]
        current_period_row = conn.execute(
            "SELECT status FROM periods WHERE period_id=?", (current,)
        ).fetchone()
        current_period_status = current_period_row["status"] if current_period_row else "missing"
        batches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        base = {
            "current_period": current,
            "current_period_status": current_period_status,
            "seal_mode": seal_mode,
            "open_periods": open_periods,
            "sealed_periods": sealed_periods,
            "meter_count": meters,
            "buildings_total": buildings_total,
            "sealed_building_count": sealed_building_count,
            "pending_variances": pending,
            "recent_batches": batches,
        }
        # 总览覆盖/digest 只读已落库的账期投影（封印/补抄/核对时同事务写入），
        # 没有投影就是没有 —— 不虚构覆盖、不改写账期状态、不克扣未决差额
        proj = seal_metrics.overview_projection(conn, current) if current else None
        base["seal_coverage"] = proj.get("coverage") if proj else None
        base["seal_digest"] = proj.get("digest") if proj else None
        return base
    finally:
        conn.close()
