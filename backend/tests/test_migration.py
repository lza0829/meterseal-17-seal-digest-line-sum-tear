"""旧库（无 sealed_scope/building、无 sealed_buildings 表）升级路径核对。"""
from __future__ import annotations

import pathlib
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient


LEGACY_SCHEMA = """
CREATE TABLE meters (
  meter_id TEXT PRIMARY KEY, building TEXT NOT NULL, unit_label TEXT NOT NULL,
  meter_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE periods (
  period_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open',
  sealed_at TEXT, batch_no TEXT
);
CREATE TABLE estimates (
  estimate_id TEXT PRIMARY KEY, meter_id TEXT NOT NULL, period_id TEXT NOT NULL,
  qty REAL NOT NULL, doc_no TEXT NOT NULL, loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE actuals (
  actual_id TEXT PRIMARY KEY, meter_id TEXT NOT NULL, period_id TEXT NOT NULL,
  qty REAL NOT NULL, occurred_at TEXT NOT NULL, reader_code TEXT NOT NULL,
  doc_no TEXT NOT NULL, loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE reconcile_batches (
  batch_id TEXT PRIMARY KEY, period_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  matched_count INTEGER NOT NULL DEFAULT 0, variance_count INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT, created_at TEXT NOT NULL
);
CREATE TABLE variances (
  variance_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL, estimate_qty REAL, actual_qty REAL,
  delta REAL NOT NULL, fate TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@pytest.fixture()
def legacy_client(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    db_path = tmp / "meterseal.db"
    raw = sqlite3.connect(db_path)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute("INSERT INTO meters VALUES ('M1','A栋','101','water','active')")
    raw.execute("INSERT INTO periods(period_id,status) VALUES ('2026-10','open')")
    raw.execute(
        "INSERT INTO reconcile_batches(batch_id,period_id,status,matched_count,variance_count,"
        "fingerprint,created_at) VALUES ('B-OLD','2026-10','open',1,0,'fp','2026-10-01T00:00:00')"
    )
    raw.commit()
    raw.close()

    import app.db as db

    monkeypatch.setattr(db, "DATA_DIR", tmp)
    monkeypatch.setattr(db, "DB_PATH", db_path)

    from app import seed
    from app.main import app

    seed.seed_if_empty()  # 触发建表 + 补列迁移
    with TestClient(app) as c:
        yield c, db_path


def test_migration_adds_columns_and_keeps_data(legacy_client):
    client, db_path = legacy_client
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(reconcile_batches)")}
    assert {"sealed_scope", "building"} <= cols
    # 旧批默认归入整期作用域，数据不丢
    row = conn.execute("SELECT * FROM reconcile_batches WHERE batch_id='B-OLD'").fetchone()
    assert row["sealed_scope"] == "period"
    assert row["building"] is None
    # 分区封印表已建
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sealed_buildings" in tables
    conn.close()

    # 缺省设置回落到整期架构，旧接口行为不变
    assert client.get("/api/settings").json().get("seal_mode", "period") in ("period",)
    w = client.get("/api/writability", params={"period_id": "2026-10"}).json()
    assert w["seal_mode"] == "period"
    assert w["buildings"][0]["building"] == "A栋"
    assert w["buildings"][0]["writable"] is True


def test_migration_idempotent(legacy_client):
    # 再跑一次迁移不应报错（列已存在）
    from app import seed
    from app.db import connect

    seed.seed_if_empty()
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM reconcile_batches").fetchone()["c"]
        assert n == 1
    finally:
        conn.close()
