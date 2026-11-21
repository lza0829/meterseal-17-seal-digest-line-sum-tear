from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel

from app.db import connect
from app.services import reconcile_service
from app.services.gate import (
    SEAL_MODE_BUILDING,
    get_seal_mode,
    is_building_sealed,
    is_period_sealed,
)

router = APIRouter(tags=["reconcile"])


class AlignIn(BaseModel):
    period_id: str
    building: str | None = None


class SealIn(BaseModel):
    period_id: str
    batch_id: str | None = None
    building: str | None = None


class FateIn(BaseModel):
    fate: str


class VerifyIn(BaseModel):
    period_id: str


@router.post("/align")
def align(body: AlignIn):
    return reconcile_service.run_align(body.period_id, body.building)


@router.get("/batches")
def batches(period_id: str | None = None, building: str | None = None):
    conn = connect()
    try:
        sql = "SELECT * FROM reconcile_batches WHERE 1=1"
        args: list = []
        if period_id:
            sql += " AND period_id=?"
            args.append(period_id)
        if building:
            sql += " AND building=?"
            args.append(building)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sealed-buildings")
def sealed_buildings(period_id: str):
    """分区封印清单：某账期已封楼栋与绑定批，可查可核对。"""
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT sb.period_id, sb.building, sb.batch_id, sb.sealed_at,
                      b.sealed_scope
               FROM sealed_buildings sb
               JOIN reconcile_batches b ON b.batch_id=sb.batch_id
               WHERE sb.period_id=?
               ORDER BY sb.building""",
            (period_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/writability")
def writability(period_id: str):
    """同口径可写性提示：逐楼栋返回该账期是否仍可装载估抄/实抄。"""
    conn = connect()
    try:
        period_sealed = is_period_sealed(conn, period_id)
        mode = get_seal_mode()
        buildings = [
            r["building"]
            for r in conn.execute(
                "SELECT DISTINCT building FROM meters ORDER BY building"
            ).fetchall()
        ]
        items = []
        for b in buildings:
            sealed = period_sealed or (
                mode == SEAL_MODE_BUILDING and is_building_sealed(conn, period_id, b)
            )
            items.append(
                {
                    "building": b,
                    "writable": not sealed,
                    "reason": "整期已封印" if period_sealed else ("楼栋已分区封印" if sealed else "可写"),
                }
            )
        return {
            "period_id": period_id,
            "seal_mode": mode,
            "period_sealed": period_sealed,
            "buildings": items,
        }
    finally:
        conn.close()


@router.get("/variances")
def variances(batch_id: str | None = None, period_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM variances WHERE batch_id=? ORDER BY meter_id", (batch_id,)
            ).fetchall()
        elif period_id:
            rows = conn.execute(
                "SELECT * FROM variances WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM variances ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/variances/{variance_id}/fate")
def set_fate(variance_id: str, body: FateIn):
    return reconcile_service.set_fate(variance_id, body.fate)


@router.post("/seal")
def seal(body: SealIn):
    return reconcile_service.seal_period(body.period_id, body.batch_id, body.building)


@router.post("/seal/verify")
def seal_verify(body: VerifyIn):
    """封印度量核对：以正式键集合重放该账期全部 sealed 批，分叉即重算回写，
    正式空而批头非零则作废现行并回滚明细。"""
    return reconcile_service.verify_period(body.period_id)


@router.get("/periods")
def periods():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM periods ORDER BY period_id DESC").fetchall()]
    finally:
        conn.close()
