from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import connect
from app.services.gate import SEAL_MODE_BUILDING, SEAL_MODE_PERIOD

router = APIRouter(tags=["settings"])


class SettingIn(BaseModel):
    value: str


@router.get("/settings")
def list_settings():
    conn = connect()
    try:
        return {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()}
    finally:
        conn.close()


@router.put("/settings/{key}")
def put_setting(key: str, body: SettingIn):
    # 架构开关只接受两种口径，防止写入脏值后写闸口径漂移
    if key == "seal_mode" and body.value not in (SEAL_MODE_PERIOD, SEAL_MODE_BUILDING):
        raise HTTPException(400, "非法封印架构，仅支持 period / building")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, body.value),
        )
        conn.commit()
        return {"key": key, "value": body.value}
    finally:
        conn.close()
