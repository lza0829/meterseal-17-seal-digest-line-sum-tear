from __future__ import annotations
from fastapi import HTTPException
from app.services import seal_metrics

from app.db import connect

# 全局只贯彻一种架构：
#   period   —— 整账期一把封：封印一次关闭该 period 全部表的估抄/实抄写闸
#   building —— 楼栋分区封印：封印请求必须带 building，只关该楼栋，其他栋同账期仍可写
SEAL_MODE_PERIOD = "period"
SEAL_MODE_BUILDING = "building"
_VALID_MODES = {SEAL_MODE_PERIOD, SEAL_MODE_BUILDING}
DEFAULT_SEAL_MODE = SEAL_MODE_PERIOD


def get_setting(key: str, default: str = "") -> str:
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def get_seal_mode() -> str:
    mode = get_setting("seal_mode", DEFAULT_SEAL_MODE)
    return mode if mode in _VALID_MODES else DEFAULT_SEAL_MODE


def resolve_building(conn, meter_id: str) -> str:
    row = conn.execute("SELECT building FROM meters WHERE meter_id=?", (meter_id,)).fetchone()
    if not row:
        raise HTTPException(404, "表具不存在")
    return row["building"]


def assert_building_known(conn, building: str) -> None:
    row = conn.execute("SELECT 1 FROM meters WHERE building=? LIMIT 1", (building,)).fetchone()
    if not row:
        # 伪造楼栋必须拒绝，不能让它落成一个无表的“分区封印”
        raise HTTPException(404, f"楼栋不存在: {building}")


def is_period_sealed(conn, period_id: str) -> bool:
    row = conn.execute("SELECT status FROM periods WHERE period_id=?", (period_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"账期不存在: {period_id}")
    return row["status"] == "sealed"


def is_building_sealed(conn, period_id: str, building: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sealed_buildings WHERE period_id=? AND building=?",
        (period_id, building),
    ).fetchone()
    return row is not None


def assert_meter_writable(conn, period_id: str, meter_id: str) -> None:
    """写闸唯一入口：根据当前架构口径判断该表具是否可写。"""
    if is_period_sealed(conn, period_id):
        if seal_metrics.estimate_gate_relaxed(conn, period_id):
            return
        raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")
    if get_seal_mode() == SEAL_MODE_BUILDING:
        building = resolve_building(conn, meter_id)
        if is_building_sealed(conn, period_id, building):
            if seal_metrics.estimate_gate_relaxed(conn, period_id):
                return
            raise HTTPException(409, f"楼栋 {building} 在账期 {period_id} 已分区封印，禁止写入")


def assert_period_writable(period_id: str) -> None:
    """无表具上下文的整期操作（对齐等）用：整期封必拒；分区模式由调用方再按楼栋细化。"""
    conn = connect()
    try:
        if is_period_sealed(conn, period_id):
            raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")
    finally:
        conn.close()


def assert_building_writable(conn, period_id: str, building: str) -> None:
    if is_period_sealed(conn, period_id):
        raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")
    if get_seal_mode() == SEAL_MODE_BUILDING and is_building_sealed(conn, period_id, building):
        raise HTTPException(409, f"楼栋 {building} 在账期 {period_id} 已分区封印，禁止写入")
