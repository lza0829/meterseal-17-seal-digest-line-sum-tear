"""
两种封印架构的全链路核对：
  period   —— 整账期一把封
  building —— 楼栋分区封印
每个用例使用独立临时库，互不污染。
"""
from __future__ import annotations

import pathlib
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
        yield c


def set_mode(client, mode: str) -> None:
    r = client.put("/api/settings/seal_mode", json={"value": mode})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["seal_mode"] == mode


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


def write_actual(client, meter: str, period: str = "2026-10", qty: float = 99.0):
    return client.post(
        "/api/actuals",
        json={
            "meter_id": meter,
            "period_id": period,
            "qty": qty,
            "occurred_at": "2026-10-29T10:00:00",
            "reader_code": "R99",
            "doc_no": f"DOC-{meter}-{qty}",
        },
    )


def write_estimate(client, meter: str, period: str = "2026-10", qty: float = 88.0):
    return client.post(
        "/api/estimates",
        json={"meter_id": meter, "period_id": period, "qty": qty, "doc_no": f"E-{meter}-{qty}"},
    )


# ---------- 整账期架构 ----------

class TestPeriodArchitecture:
    def test_seal_rejects_writes_for_any_building(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        r = seal(client, "2026-10")
        assert r.status_code == 200
        assert r.json()["sealed_scope"] == "period"

        # 封后任意栋拒写（估抄 + 实抄，A 栋与 B 栋都拒）
        for call in (write_actual, write_estimate):
            for meter in ("M-W-101", "M-W-201"):
                resp = call(client, meter)
                assert resp.status_code == 409, (call.__name__, meter, resp.text)
                assert "禁止写入" in resp.json()["detail"]

    def test_seal_with_building_rejected_in_period_mode(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        # 整期架构下带楼栋 = 口径不符，必须拒，不能落成“分区封”
        r = seal(client, "2026-10", building="A栋")
        assert r.status_code == 400

    def test_align_scope_is_whole_period(self, client):
        set_mode(client, "period")
        out = align(client, "2026-10")
        # 全账期 6 表都在批范围内
        assert out["sealed_scope"] == "period"
        assert out["building"] is None
        assert out["matched_count"] + out["variance_count"] == 6

    def test_fate_locked_after_period_seal(self, client):
        set_mode(client, "period")
        out = align(client, "2026-10")
        vid = client.get("/api/variances", params={"batch_id": out["batch_id"]}).json()[0]["variance_id"]
        seal(client, "2026-10")
        r = client.post(f"/api/variances/{vid}/fate", json={"fate": "quarantine"})
        assert r.status_code == 409

    def test_writability_all_locked_after_seal(self, client):
        set_mode(client, "period")
        align(client, "2026-10")
        seal(client, "2026-10")
        w = client.get("/api/writability", params={"period_id": "2026-10"}).json()
        assert w["seal_mode"] == "period"
        assert w["period_sealed"] is True
        assert all(b["writable"] is False for b in w["buildings"])


# ---------- 楼栋分区架构 ----------

class TestBuildingArchitecture:
    def test_seal_without_building_rejected(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        # 禁止未带楼栋却宣称分区封
        r = seal(client, "2026-10")
        assert r.status_code == 400
        assert "building" in r.json()["detail"]

    def test_align_without_building_rejected(self, client):
        set_mode(client, "building")
        r = client.post("/api/align", json={"period_id": "2026-10"})
        assert r.status_code == 400

    def test_fake_building_rejected(self, client):
        set_mode(client, "building")
        r = client.post("/api/align", json={"period_id": "2026-10", "building": "Z栋"})
        assert r.status_code == 404
        r2 = seal(client, "2026-10", building="Z栋")
        assert r2.status_code == 404

    def test_align_building_scope_only_contains_its_meters(self, client):
        set_mode(client, "building")
        out_a = align(client, "2026-10", building="A栋")
        assert out_a["sealed_scope"] == "building"
        assert out_a["building"] == "A栋"
        # A 栋 3 表；B 栋表具不得混入
        assert out_a["matched_count"] + out_a["variance_count"] == 3
        vars_a = client.get("/api/variances", params={"batch_id": out_a["batch_id"]}).json()
        meters_in_batch = {v["meter_id"] for v in vars_a}
        # 匹配的表不进 variances，用批详情接口核对范围更稳：查 A 栋批只有 A 栋表
        assert meters_in_batch <= {"M-W-101", "M-W-102", "M-E-101"}

    def test_seal_a_then_b_still_writable(self, client):
        """核心口径：封 A 栋后 B 栋仍可写实抄，且 A 栋拒写。"""
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        r = seal(client, "2026-10", building="A栋")
        assert r.status_code == 200
        body = r.json()
        assert body["sealed_scope"] == "building"
        assert body["building"] == "A栋"

        # 账期本身仍是 open，不能“文案分区封、整期全禁写”
        periods = {p["period_id"]: p for p in client.get("/api/periods").json()}
        assert periods["2026-10"]["status"] == "open"

        # A 栋表拒写（估抄/实抄）
        assert write_actual(client, "M-W-101").status_code == 409
        assert write_estimate(client, "M-E-101").status_code == 409
        # B 栋表仍可写
        assert write_actual(client, "M-W-201").status_code == 200
        assert write_estimate(client, "M-E-202").status_code == 200

    def test_sealed_building_list_and_batch_detail_queryable(self, client):
        set_mode(client, "building")
        out_a = align(client, "2026-10", building="A栋")
        seal(client, "2026-10", building="A栋")

        sb = client.get("/api/sealed-buildings", params={"period_id": "2026-10"}).json()
        assert len(sb) == 1
        assert sb[0]["building"] == "A栋"
        assert sb[0]["sealed_scope"] == "building"
        assert sb[0]["batch_id"] == out_a["batch_id"]

        batches = client.get("/api/batches", params={"period_id": "2026-10", "building": "A栋"}).json()
        assert all(b["building"] == "A栋" and b["sealed_scope"] == "building" for b in batches)
        assert batches[0]["status"] == "sealed"

    def test_b_align_and_seal_after_a_sealed(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        seal(client, "2026-10", building="A栋")

        # B 栋仍可对齐（不会被 A 栋的封印/批过期影响）
        out_b = align(client, "2026-10", building="B栋")
        assert out_b["building"] == "B栋"
        assert seal(client, "2026-10", building="B栋").status_code == 200

        # 两栋都封后，逐栋拒写
        assert write_actual(client, "M-W-101").status_code == 409
        assert write_actual(client, "M-W-201").status_code == 409
        sb = client.get("/api/sealed-buildings", params={"period_id": "2026-10"}).json()
        assert {s["building"] for s in sb} == {"A栋", "B栋"}

    def test_align_does_not_expire_other_building_open_batch(self, client):
        set_mode(client, "building")
        out_a = align(client, "2026-10", building="A栋")
        align(client, "2026-10", building="B栋")
        # A 栋的 open 批不得被 B 栋对齐作过期
        batches = {b["batch_id"]: b["status"] for b in client.get("/api/batches", params={"period_id": "2026-10"}).json()}
        assert batches[out_a["batch_id"]] == "open"

    def test_batch_scope_mismatch_rejected(self, client):
        set_mode(client, "building")
        out_b = align(client, "2026-10", building="B栋")
        # 拿 B 栋批去封 A 栋：作用域不一致，必须拒
        r = seal(client, "2026-10", building="A栋", batch_id=out_b["batch_id"])
        assert r.status_code == 400

    def test_double_seal_same_building_rejected(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        assert seal(client, "2026-10", building="A栋").status_code == 200
        r = seal(client, "2026-10", building="A栋")
        assert r.status_code == 409
        sb = client.get("/api/sealed-buildings", params={"period_id": "2026-10"}).json()
        assert len(sb) == 1  # 没有重复/半截记录

    def test_fate_locked_for_sealed_building_only(self, client):
        set_mode(client, "building")
        out_a = align(client, "2026-10", building="A栋")
        out_b = align(client, "2026-10", building="B栋")
        seal(client, "2026-10", building="A栋")

        v_a = client.get("/api/variances", params={"batch_id": out_a["batch_id"]}).json()
        v_b = client.get("/api/variances", params={"batch_id": out_b["batch_id"]}).json()
        if v_a:
            assert client.post(f"/api/variances/{v_a[0]['variance_id']}/fate",
                               json={"fate": "quarantine"}).status_code == 409
        if v_b:
            assert client.post(f"/api/variances/{v_b[0]['variance_id']}/fate",
                               json={"fate": "quarantine"}).status_code == 200

    def test_writability_reflects_partial_seal(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        seal(client, "2026-10", building="A栋")
        w = client.get("/api/writability", params={"period_id": "2026-10"}).json()
        assert w["period_sealed"] is False
        by = {b["building"]: b for b in w["buildings"]}
        assert by["A栋"]["writable"] is False
        assert by["A栋"]["reason"] == "楼栋已分区封印"
        assert by["B栋"]["writable"] is True

    def test_concurrent_two_building_seals_no_overwrite(self, client):
        """并发两栋分区封：各自成功，写闸不互相覆盖，无半截分区批。"""
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        align(client, "2026-10", building="B栋")

        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def worker(building: str):
            barrier.wait()
            try:
                r = seal(client, "2026-10", building=building)
                assert r.status_code == 200, r.text
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t1 = threading.Thread(target=worker, args=("A栋",))
        t2 = threading.Thread(target=worker, args=("B栋",))
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert not errors

        sb = client.get("/api/sealed-buildings", params={"period_id": "2026-10"}).json()
        assert {s["building"] for s in sb} == {"A栋", "B栋"}
        assert len({s["batch_id"] for s in sb}) == 2  # 各绑各的批，未互相覆盖
        batches = client.get("/api/batches", params={"period_id": "2026-10"}).json()
        sealed = [b for b in batches if b["status"] == "sealed"]
        assert {b["building"] for b in sealed} == {"A栋", "B栋"}
        # 没有遗留 open 的“半截”批
        assert all(b["status"] != "open" for b in batches if b["building"] in ("A栋", "B栋"))

    def test_concurrent_same_building_only_one_wins(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        results: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            r = seal(client, "2026-10", building="A栋")
            with lock:
                results.append(r.status_code)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert sorted(results) == [200, 409]
        sb = client.get("/api/sealed-buildings", params={"period_id": "2026-10"}).json()
        assert len(sb) == 1


# ---------- 架构切换对照 ----------

class TestModeSwitch:
    def test_switch_mode_same_period_writability_contrast(self, client):
        # 分区架构下封 A 栋
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        seal(client, "2026-10", building="A栋")
        w_building = client.get("/api/writability", params={"period_id": "2026-10"}).json()
        assert {b["building"]: b["writable"] for b in w_building["buildings"]} == {"A栋": False, "B栋": True}

        # 切回整期架构：分区写闸不再生效（封印语义随当前架构），两栋都可写直到整期封印
        set_mode(client, "period")
        w_period = client.get("/api/writability", params={"period_id": "2026-10"}).json()
        assert w_period["seal_mode"] == "period"
        assert all(b["writable"] is True for b in w_period["buildings"])

        align(client, "2026-10")
        seal(client, "2026-10")
        w_locked = client.get("/api/writability", params={"period_id": "2026-10"}).json()
        assert all(b["writable"] is False for b in w_locked["buildings"])

    def test_invalid_mode_rejected(self, client):
        r = client.put("/api/settings/seal_mode", json={"value": "floor"})
        assert r.status_code == 400


# ---------- 总览计数不撕裂 ----------

class TestOverview:
    def test_overview_partition_counts(self, client):
        set_mode(client, "building")
        align(client, "2026-10", building="A栋")
        seal(client, "2026-10", building="A栋")
        ov = client.get("/api/overview").json()
        assert ov["seal_mode"] == "building"
        assert ov["current_period_status"] == "open"
        assert ov["sealed_periods"] == 0          # 账期未整封
        assert ov["sealed_building_count"] == 1   # 分区计数独立
        assert ov["buildings_total"] == 2
        for b in ov["recent_batches"]:
            assert "sealed_scope" in b and "building" in b
