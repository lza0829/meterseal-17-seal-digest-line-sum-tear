"""封印度量一致性夹具 —— 全局贯彻「正式键架构」后的四条事故场景核对：

  1. 头满正式缺：批头覆盖 100/计数非零，正式估抄/实抄却缺键甚至全空
     → 缺一部分：按正式行重算批头覆盖与 digest 并回写（repaired）
     → 正式全空而批头非零：作废该 sealed 现行并回滚半截明细（voided）
  2. digest≠明细哈希：digest 必须等于正式行派生的对齐明细哈希，篡改即被核对回写
  3. 封后补抄头不刷新：封后补实抄必须同事务改 digest 与总览，不得放行不刷新
  4. 并发半截明细：并发封印与装载不得留下空 digest 现行，事后核对全 ok

每个用例独立临时库；tamper 直接改库模拟事故现场的库内分叉。
"""
from __future__ import annotations

import hashlib
import pathlib
import sqlite3
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    import app.db as db

    monkeypatch.setattr(db, "DATA_DIR", tmp)
    monkeypatch.setattr(db, "DB_PATH", tmp / "meterseal.db")

    from app import seed
    from app.main import app

    seed.seed_if_empty()
    with TestClient(app) as c:
        c.db_path = tmp / "meterseal.db"
        yield c


def set_mode(client, mode: str) -> None:
    r = client.put("/api/settings/seal_mode", json={"value": mode})
    assert r.status_code == 200, r.text


def align(client, period: str, building: str | None = None):
    body = {"period_id": period}
    if building is not None:
        body["building"] = building
    r = client.post("/api/align", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def seal(client, period: str, building: str | None = None, batch_id: str | None = None):
    body = {"period_id": period}
    if building is not None:
        body["building"] = building
    if batch_id is not None:
        body["batch_id"] = batch_id
    return client.post("/api/seal", json=body)


def verify(client, period: str):
    r = client.post("/api/seal/verify", json={"period_id": period})
    assert r.status_code == 200, r.text
    return r.json()


def supplementary(client, meter: str, period: str = "2026-10", qty: float = 15.0, doc: str = "FIX-1"):
    return client.post(
        "/api/readings/supplementary",
        json={
            "meter_id": meter,
            "period_id": period,
            "qty": qty,
            "occurred_at": "2026-10-31T09:00:00",
            "reader_code": "R88",
            "doc_no": doc,
        },
    )


def write_actual(client, meter: str, period: str = "2026-10", qty: float = 99.0, doc: str | None = None):
    return client.post(
        "/api/actuals",
        json={
            "meter_id": meter,
            "period_id": period,
            "qty": qty,
            "occurred_at": "2026-10-29T10:00:00",
            "reader_code": "R99",
            "doc_no": doc or f"DOC-{meter}-{qty}",
        },
    )


def tamper(db_path, sql: str, args: tuple = ()):
    conn = sqlite3.connect(db_path)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def query(db_path, sql: str, args: tuple = ()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def replay_expected(db_path, period_id: str, building: str | None) -> dict:
    """独立重放：测试自己从正式估抄∪实抄表重建对齐键集合并推导批头/digest。

    不 import 被测实现 —— 「封印后批头覆盖可被正式键重放」必须由独立重放背书。
    """
    conn = sqlite3.connect(db_path)
    if building is None:
        meters = [r[0] for r in conn.execute("SELECT meter_id FROM meters ORDER BY meter_id")]
    else:
        meters = [
            r[0]
            for r in conn.execute(
                "SELECT meter_id FROM meters WHERE building=? ORDER BY meter_id", (building,)
            )
        ]
    est: dict[str, float] = {}
    act: dict[str, float] = {}
    if meters:
        ph = ",".join("?" * len(meters))
        for r in conn.execute(
            f"SELECT meter_id, qty FROM estimates WHERE period_id=? AND meter_id IN ({ph})"
            " ORDER BY loaded_at, rowid",
            (period_id, *meters),
        ):
            est[r[0]] = r[1]
        for r in conn.execute(
            f"SELECT meter_id, qty FROM actuals WHERE period_id=? AND meter_id IN ({ph})"
            " ORDER BY loaded_at, rowid",
            (period_id, *meters),
        ):
            act[r[0]] = r[1]
    conn.close()
    keys = sorted(set(est) | set(act))
    matched = sum(1 for k in keys if k in est and k in act and abs(est[k] - act[k]) < 1e-9)
    variance = len(keys) - matched
    coverage = round(100 * len(keys) / len(meters)) if meters else 0
    scope = "building" if building is not None else "period"

    def fmt(q) -> str:
        return "" if q is None else repr(float(q))

    canonical = ";".join(f"{k}:{fmt(est.get(k))}:{fmt(act.get(k))}" for k in keys)
    digest = hashlib.sha256(
        f"{period_id}|{scope}|{building or ''}|{canonical}".encode()
    ).hexdigest()[:16]
    return {
        "matched": matched,
        "variance": variance,
        "coverage": coverage,
        "digest": digest,
        "keys": len(keys),
    }


def get_batch(client, period: str) -> dict:
    rows = client.get("/api/batches", params={"period_id": period}).json()
    assert rows, "应存在对账批"
    return rows[0]


# ---------- 夹具一：头满正式缺 ----------

class TestHeaderFullFormalMissing:
    def test_partial_missing_repairs_then_empty_voids(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        r = seal(client, "2026-10")
        assert r.status_code == 200, r.text
        # 回执即真：覆盖/digest 与独立重放一致
        exp = replay_expected(client.db_path, "2026-10", None)
        assert r.json()["coverage"] == exp["coverage"] == 100
        assert r.json()["digest"] == exp["digest"]

        # —— 情形一：正式缺一部分（删掉两只有表具的估抄+实抄），批头仍宣称满编 ——
        for mid in ("M-W-101", "M-E-101"):
            tamper(client.db_path, "DELETE FROM estimates WHERE period_id='2026-10' AND meter_id=?", (mid,))
            tamper(client.db_path, "DELETE FROM actuals WHERE period_id='2026-10' AND meter_id=?", (mid,))
        stale = get_batch(client, "2026-10")
        assert stale["matched_count"] == 4  # 分叉现场：批头还是旧数字
        out = verify(client, "2026-10")
        assert [a["action"] for a in out["actions"]] == ["repaired"]
        exp2 = replay_expected(client.db_path, "2026-10", None)
        assert exp2["keys"] == 4
        batch = get_batch(client, "2026-10")
        assert batch["matched_count"] == exp2["matched"] == 2
        assert batch["variance_count"] == exp2["variance"] == 2
        assert batch["fingerprint"] == exp2["digest"]
        # 总览投影同步回写：覆盖降到 4/6，digest 与批头同源
        ov = client.get("/api/overview").json()
        assert ov["seal_coverage"] == exp2["coverage"] == 67
        assert ov["seal_digest"] == exp2["digest"]
        # 对齐明细同步收敛：消失键的差额行已剔除
        vars_ = client.get("/api/variances", params={"batch_id": batch["batch_id"]}).json()
        assert {v["meter_id"] for v in vars_} == {"M-W-102", "M-E-201"}

        # —— 情形二：正式全空而批头非零 —— 作废 sealed 现行并回滚半截明细 ——
        tamper(client.db_path, "DELETE FROM estimates WHERE period_id='2026-10'")
        tamper(client.db_path, "DELETE FROM actuals WHERE period_id='2026-10'")
        out = verify(client, "2026-10")
        assert [a["action"] for a in out["actions"]] == ["voided"]
        batch = get_batch(client, "2026-10")
        assert batch["status"] == "voided"
        assert batch["matched_count"] == 0 and batch["variance_count"] == 0
        assert batch["fingerprint"] is None  # 不留空 digest 现行
        assert query(client.db_path, "SELECT * FROM variances WHERE batch_id=?", (batch["batch_id"],)) == []
        # 账期解封、投影清除、总览不再假绿
        periods = {p["period_id"]: p for p in client.get("/api/periods").json()}
        assert periods["2026-10"]["status"] == "open"
        assert periods["2026-10"]["sealed_at"] is None
        ov = client.get("/api/overview").json()
        assert ov["seal_coverage"] is None
        assert ov["seal_digest"] is None
        assert ov["current_period_status"] == "open"
        # 再核对：无 sealed 批，动作为空，幂等
        assert verify(client, "2026-10")["actions"] == []

    def test_building_scope_void_only_unseals_that_building(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        align(client, "2026-10", building="B栋")
        seal(client, "2026-10", building="A栋")
        seal(client, "2026-10", building="B栋")
        # A 栋正式行全空 → 只作废 A 栋现行，B 栋不受影响
        tamper(
            client.db_path,
            "DELETE FROM estimates WHERE period_id='2026-10' AND meter_id IN"
            " (SELECT meter_id FROM meters WHERE building='A栋')",
        )
        tamper(
            client.db_path,
            "DELETE FROM actuals WHERE period_id='2026-10' AND meter_id IN"
            " (SELECT meter_id FROM meters WHERE building='A栋')",
        )
        out = verify(client, "2026-10")
        by_building = {a["building"]: a["action"] for a in out["actions"]}
        assert by_building == {"A栋": "voided", "B栋": "ok"}
        sb = client.get("/api/sealed-buildings", params={"period_id": "2026-10"}).json()
        assert [s["building"] for s in sb] == ["B栋"]
        # A 栋作废后写闸放开（现行已不存在），B 栋仍封
        w = {b["building"]: b["writable"] for b in client.get("/api/writability", params={"period_id": "2026-10"}).json()["buildings"]}
        assert w == {"A栋": True, "B栋": False}
        # B 栋批头仍可被正式键重放
        exp_b = replay_expected(client.db_path, "2026-10", "B栋")
        batch_b = [b for b in client.get("/api/batches", params={"period_id": "2026-10"}).json()
                   if b["building"] == "B栋"][0]
        assert batch_b["fingerprint"] == exp_b["digest"]


# ---------- 夹具二：digest≠明细哈希 ----------

class TestDigestEqualsDetailHash:
    def test_digest_replayable_and_tamper_repaired(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        assert seal(client, "2026-10").status_code == 200
        batch = get_batch(client, "2026-10")
        exp = replay_expected(client.db_path, "2026-10", None)
        # 封印后批头覆盖与 digest 可被正式键重放
        assert batch["matched_count"] == exp["matched"]
        assert batch["variance_count"] == exp["variance"]
        assert batch["fingerprint"] == exp["digest"]
        assert batch["fingerprint"]

        # 篡改 digest（与明细哈希脱钩）→ 核对回写
        tamper(client.db_path, "UPDATE reconcile_batches SET fingerprint='0000000000000000' WHERE batch_id=?", (batch["batch_id"],))
        out = verify(client, "2026-10")
        assert [a["action"] for a in out["actions"]] == ["repaired"]
        assert out["actions"][0]["digest_before"] == "0000000000000000"
        assert get_batch(client, "2026-10")["fingerprint"] == exp["digest"]

        # 篡改批头计数 → 核对回写
        tamper(client.db_path, "UPDATE reconcile_batches SET matched_count=99 WHERE batch_id=?", (batch["batch_id"],))
        out = verify(client, "2026-10")
        assert [a["action"] for a in out["actions"]] == ["repaired"]
        assert get_batch(client, "2026-10")["matched_count"] == exp["matched"]

        # 篡改编码明细内容（数量）而批头不动 → 明细重建回正式行派生值
        vid = client.get("/api/variances", params={"batch_id": batch["batch_id"]}).json()[0]["variance_id"]
        tamper(client.db_path, "UPDATE variances SET actual_qty=999.0 WHERE variance_id=?", (vid,))
        out = verify(client, "2026-10")
        assert [a["action"] for a in out["actions"]] == ["repaired"]
        repaired_row = [v for v in client.get("/api/variances", params={"batch_id": batch["batch_id"]}).json()
                        if v["variance_id"] == vid][0]
        assert repaired_row["actual_qty"] != 999.0

        # 全部分叉消除后：核对 ok，总览 digest 与批头同源
        out = verify(client, "2026-10")
        assert [a["action"] for a in out["actions"]] == ["ok"]
        ov = client.get("/api/overview").json()
        assert ov["seal_digest"] == exp["digest"]
        assert ov["seal_coverage"] == 100


# ---------- 夹具三：封后补抄头不刷新 ----------

class TestPostSealSupplementary:
    def test_supplementary_refreshes_digest_and_overview_same_tx(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        r = seal(client, "2026-10")
        assert r.status_code == 200
        digest0 = r.json()["digest"]

        # 正常装载仍被拒：旁路只走专用端点
        assert write_actual(client, "M-W-102", qty=15.0, doc="NORMAL-1").status_code == 409

        # 封后补实抄：M-W-102 修正为与估抄一致 → 差额消、digest 变、总览同事务刷新
        r = supplementary(client, "M-W-102", qty=15.0, doc="FIX-102")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "refreshed"
        assert body["digest"] != digest0
        exp = replay_expected(client.db_path, "2026-10", None)
        assert body["digest"] == exp["digest"]
        assert body["matched_count"] == exp["matched"] == 5
        assert body["variance_count"] == exp["variance"] == 1

        # 库内四条链一致：批头、digest、正式行、对齐明细
        batch = get_batch(client, "2026-10")
        assert batch["fingerprint"] == exp["digest"]
        assert batch["matched_count"] == 5 and batch["variance_count"] == 1
        vars_ = client.get("/api/variances", params={"batch_id": batch["batch_id"]}).json()
        assert {v["meter_id"] for v in vars_} == {"M-E-201"}  # M-W-102 已消差
        ov = client.get("/api/overview").json()
        assert ov["seal_digest"] == exp["digest"]
        assert ov["seal_coverage"] == 100
        # 核对确认库内无分叉
        assert [a["action"] for a in verify(client, "2026-10")["actions"]] == ["ok"]

    def test_supplementary_rejected_when_scope_not_sealed(self, client):
        set_mode(client, "period")
        # 账期未封印：旁路拒用，指引走正常装载
        r = supplementary(client, "M-W-101", period="2026-09", qty=10.0, doc="S-1")
        assert r.status_code == 400
        assert "正常装载" in r.json()["detail"]

    def test_building_scope_supplementary_precise(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        r = seal(client, "2026-10", building="A栋")
        digest0 = r.json()["digest"]

        # A 栋补抄 → 只刷新 A 栋批；B 栋未封走旁路被拒；B 栋正常装载仍通
        r = supplementary(client, "M-W-101", qty=13.0, doc="FIX-A-101")
        assert r.status_code == 200, r.text
        assert r.json()["digest"] != digest0
        exp_a = replay_expected(client.db_path, "2026-10", "A栋")
        assert r.json()["digest"] == exp_a["digest"]
        assert r.json()["matched_count"] == exp_a["matched"] == 1
        assert r.json()["variance_count"] == exp_a["variance"] == 2
        assert supplementary(client, "M-W-201", qty=20.0, doc="FIX-B-201").status_code == 400
        assert write_actual(client, "M-W-201", qty=20.0, doc="NORMAL-B-1").status_code == 200
        # 总览投影只聚合已封作用域（A 栋），与 A 栋批 digest 同源
        ov = client.get("/api/overview").json()
        assert ov["seal_digest"] == exp_a["digest"]
        assert [a["action"] for a in verify(client, "2026-10")["actions"]] == ["ok"]


# ---------- 夹具四：并发半截明细 ----------

class TestConcurrentSealAndLoad:
    def test_no_half_written_empty_digest_current(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        barrier = threading.Barrier(2)
        results: dict[str, int] = {}
        lock = threading.Lock()

        def hit(key: str, fn):
            barrier.wait()
            r = fn()
            with lock:
                results[key] = r.status_code

        # 并发：封印 ∥ 正常装载（新单号、改数量 —— 若先落库则正式行集合改变）
        t1 = threading.Thread(target=hit, args=("seal", lambda: seal(client, "2026-10")))
        t2 = threading.Thread(target=hit, args=("load", lambda: write_actual(client, "M-W-101", qty=13.0, doc="RACE-A")))
        t1.start(); t2.start(); t1.join(); t2.join()

        # 封印必成且唯一；装载要么抢在封前落库（200），要么被封印挡下（409）
        assert results["seal"] == 200
        assert results["load"] in (200, 409)

        # 并发：封后补抄 ∥ 重复封印 —— 补抄必成（已封），重复封必 409
        t3 = threading.Thread(target=hit, args=("supp", lambda: supplementary(client, "M-W-102", qty=15.0, doc="FIX-RACE")))
        t4 = threading.Thread(target=hit, args=("seal2", lambda: seal(client, "2026-10")))
        t3.start(); t4.start(); t3.join(); t4.join()
        assert results["supp"] == 200
        assert results["seal2"] == 409

        # 收敛到一份自洽证据：唯一 sealed 批，digest 非空且可被正式键重放
        batches = client.get("/api/batches", params={"period_id": "2026-10"}).json()
        sealed = [b for b in batches if b["status"] == "sealed"]
        assert len(sealed) == 1
        assert sealed[0]["fingerprint"]
        exp = replay_expected(client.db_path, "2026-10", None)
        assert exp["keys"] > 0
        assert sealed[0]["fingerprint"] == exp["digest"]
        assert sealed[0]["matched_count"] == exp["matched"]
        assert sealed[0]["variance_count"] == exp["variance"]
        # 库内核对全 ok：批头/digest/正式行/对齐明细四分叉皆消
        assert [a["action"] for a in verify(client, "2026-10")["actions"]] == ["ok"]
        ov = client.get("/api/overview").json()
        assert ov["seal_digest"] == exp["digest"]


# ---------- 禁止无正式行当封印凭证 ----------

class TestEmptyFormalSealRejected:
    def test_seal_with_empty_formal_set_rejected(self, client):
        set_mode(client, "period")
        out = align(client, "2026-10")
        # 对齐后正式行被清空（事故现场）→ 封印必须拒，且不留半截
        tamper(client.db_path, "DELETE FROM estimates WHERE period_id='2026-10'")
        tamper(client.db_path, "DELETE FROM actuals WHERE period_id='2026-10'")
        r = seal(client, "2026-10", batch_id=out["batch_id"])
        assert r.status_code == 409
        assert "空行集合" in r.json()["detail"]
        # 无半截：账期仍 open、批仍 open、无投影、无 sealed 现行
        periods = {p["period_id"]: p for p in client.get("/api/periods").json()}
        assert periods["2026-10"]["status"] == "open"
        batch = get_batch(client, "2026-10")
        assert batch["status"] == "open"
        ov = client.get("/api/overview").json()
        assert ov["seal_coverage"] is None and ov["seal_digest"] is None

    def test_align_with_empty_formal_set_rejected(self, client):
        set_mode(client, "period")
        r = client.post("/api/align", json={"period_id": "2026-09"})
        assert r.status_code == 400  # 2026-09 无任何正式行
