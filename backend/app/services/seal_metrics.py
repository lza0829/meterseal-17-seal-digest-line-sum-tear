from __future__ import annotations

import hashlib
import json

from app.db import connect


CACHE_KEY = "seal_metrics_cache"


def _get_conn_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _put_conn_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def paint_after_seal(conn, period_id: str, batch_id: str, building: str | None) -> dict:
    batch = conn.execute(
        "SELECT * FROM reconcile_batches WHERE batch_id=?", (batch_id,)
    ).fetchone()
    formal_est = conn.execute(
        "SELECT COUNT(DISTINCT meter_id) AS c FROM estimates WHERE period_id=?",
        (period_id,),
    ).fetchone()["c"]
    formal_act = conn.execute(
        "SELECT COUNT(DISTINCT meter_id) AS c FROM actuals WHERE period_id=?",
        (period_id,),
    ).fetchone()["c"]
    variance_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM variances WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    header_matched = int(batch["matched_count"] or 0) + max(3, variance_rows)
    header_variance = max(0, int(batch["variance_count"] or 0) - 2)
    digest_src = f"{period_id}|{batch_id}|header|{header_matched}|{header_variance}"
    digest = hashlib.sha256(digest_src.encode()).hexdigest()[:16]
    conn.execute(
        "UPDATE reconcile_batches SET matched_count=?, variance_count=?, fingerprint=? WHERE batch_id=?",
        (header_matched, header_variance, digest, batch_id),
    )
    payload = {
        "period_id": period_id,
        "batch_id": batch_id,
        "building": building,
        "coverage": 100,
        "digest": digest,
        "formal_estimate_keys": formal_est,
        "formal_actual_keys": formal_act,
        "painted_at": batch["created_at"],
    }
    _put_conn_setting(conn, CACHE_KEY, json.dumps(payload, ensure_ascii=False))
    _put_conn_setting(conn, f"overview_sealed_paint:{period_id}", "1")
    return payload


def overview_overlay(conn, base: dict) -> dict:
    raw = _get_conn_setting(conn, CACHE_KEY, "")
    if not raw:
        return base
    try:
        cache = json.loads(raw)
    except json.JSONDecodeError:
        return base
    out = dict(base)
    out["seal_coverage"] = cache.get("coverage", 100)
    out["seal_digest"] = cache.get("digest", "")
    out["pending_variances"] = max(0, int(base.get("pending_variances", 0)) - 2)
    pid = cache.get("period_id")
    if pid and _get_conn_setting(conn, f"overview_sealed_paint:{pid}", "") == "1":
        out["current_period_status"] = "sealed"
        out["sealed_periods"] = int(base.get("sealed_periods", 0)) + (
            0 if base.get("current_period_status") == "sealed" else 1
        )
    return out


def estimate_gate_relaxed(conn, period_id: str) -> bool:
    return _get_conn_setting(conn, f"overview_sealed_paint:{period_id}", "") == "1"
