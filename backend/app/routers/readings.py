from __future__ import annotations
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.services import gate, seal_metrics
from app.services.gate import assert_meter_writable

router = APIRouter(tags=["readings"])


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class EstimateIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    doc_no: str = Field(min_length=1)


class ActualIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    occurred_at: str
    reader_code: str = "R00"
    doc_no: str = Field(min_length=1)


@router.get("/estimates")
def list_estimates(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM estimates WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM estimates ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/estimates")
def create_estimate(body: EstimateIn):
    conn = connect()
    try:
        # 立即事务：写闸判定与落库同一把写锁，与并发封印串行化，不留半截
        conn.execute("BEGIN IMMEDIATE")
        # 写闸先于存在性之外的一切副作用：按当前架构口径（整期/楼栋分区）判定
        assert_meter_writable(conn, body.period_id, body.meter_id)
        eid = "E-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?)""",
                (eid, body.meter_id, body.period_id, body.qty, body.doc_no, _now()),
            )
            conn.commit()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(409, f"估抄写入失败: {e}") from e
        return {"estimate_id": eid, "ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.get("/actuals")
def list_actuals(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM actuals WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM actuals ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/actuals")
def create_actual(body: ActualIn):
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert_meter_writable(conn, body.period_id, body.meter_id)
        aid = "A-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    body.meter_id,
                    body.period_id,
                    body.qty,
                    body.occurred_at,
                    body.reader_code,
                    body.doc_no,
                    _now(),
                ),
            )
            conn.commit()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(409, f"实抄写入失败: {e}") from e
        return {"actual_id": aid, "ok": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/readings/supplementary")
def create_supplementary_actual(body: ActualIn):
    """封后装载旁路：账期/楼栋已封后补实抄。

    旁路不脱钩：补抄落库与 sealed 批的对齐明细、批头、digest、总览投影重算
    在同一个事务里提交 —— 补抄之后 digest 必动、总览必新，回执即可被正式键重放。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        batch = gate.sealed_batch_for_meter(conn, body.period_id, body.meter_id)
        if batch is None:
            raise HTTPException(400, "该表具所在范围未封印，请走正常装载")
        aid = "A-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    body.meter_id,
                    body.period_id,
                    body.qty,
                    body.occurred_at,
                    body.reader_code,
                    body.doc_no,
                    _now(),
                ),
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(409, f"实抄写入失败: {e}") from e
        # 同事务按正式行重算并回写：批头覆盖、digest、对齐明细、总览投影
        result = seal_metrics.refresh_sealed_batch(conn, batch)
        conn.commit()
        return {"actual_id": aid, "ok": True, "bypass": "post_seal_supplementary", **result}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
