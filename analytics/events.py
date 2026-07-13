"""Events Blueprint — 多 App 统一埋点 / 日活。

端点：
  POST /api/events/track   — 无鉴权，SDK 直发（简单速率限制）
  GET  /api/events/stats   — X-API-Key 保护，DAU / 事件分布（总量 + 按天 Top-N）/ 版本分布

设计取舍见 README.md。SQLite 单表宽结构，`app_name` 字段区分 App。

track 支持两种 body：
  1) 明文 JSON（历史 DinoPedia 等兼容）
  2) AES-256-GCM 信封：{ v, app_name, enc:"aes-256-gcm", data:<base64 nonce|ct|tag> }
     密钥来自环境变量 ANALYTICS_PAYLOAD_KEY（64 hex = 32 bytes）
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from . import require_api_key

bp = Blueprint("events", __name__)

ALLOWED_APPS = {"dinopedia", "liveai", "animal-friends", "earthtrip"}

_RATE_WINDOW_S = 60
_RATE_LIMIT = 60
_rate_state: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()

MAX_EVENTS_PER_REQUEST = 100
MAX_PROPS_BYTES = 2048

# AES-GCM: CryptoKit combined = 12-byte nonce + ciphertext + 16-byte tag
_GCM_NONCE_LEN = 12


def _db_path() -> str:
    return current_app.config["EVENTS_DB_PATH"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _payload_key() -> bytes | None:
    """32-byte AES key from ANALYTICS_PAYLOAD_KEY (hex). Empty → encryption disabled."""
    raw = (current_app.config.get("PAYLOAD_KEY") or os.environ.get("ANALYTICS_PAYLOAD_KEY") or "").strip()
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def _decrypt_envelope(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Decrypt aes-256-gcm envelope → plain track body. Returns (body, error_code)."""
    enc = body.get("enc")
    data_b64 = body.get("data")
    if enc != "aes-256-gcm" or not data_b64:
        return None, "invalid_envelope"

    key = _payload_key()
    if key is None:
        return None, "encryption_not_configured"

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        raw = base64.b64decode(data_b64, validate=True)
        if len(raw) <= _GCM_NONCE_LEN + 16:
            return None, "decrypt_failed"
        nonce, ct = raw[:_GCM_NONCE_LEN], raw[_GCM_NONCE_LEN:]
        plain = AESGCM(key).decrypt(nonce, ct, None)
        parsed = json.loads(plain.decode("utf-8"))
        if not isinstance(parsed, dict):
            return None, "decrypt_failed"
        # 信封外的 app_name 可与明文一致；不一致时以明文为准，但要求都在白名单
        return parsed, None
    except Exception:
        return None, "decrypt_failed"


def _resolve_track_body() -> tuple[dict[str, Any] | None, Any]:
    """Parse request JSON; decrypt if envelope. Returns (body, error_response_or_None)."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return None, (jsonify(error="invalid_json"), 400)

    if body.get("enc") == "aes-256-gcm":
        plain, err = _decrypt_envelope(body)
        if err == "encryption_not_configured":
            return None, (jsonify(error=err), 503)
        if err or plain is None:
            return None, (jsonify(error=err or "decrypt_failed"), 400)
        # 信封外 app_name 必须存在且在白名单（便于网关日志）；与明文冲突时以明文为准
        outer_app = body.get("app_name")
        if outer_app and outer_app not in ALLOWED_APPS:
            return None, (jsonify(error="unknown_app"), 400)
        return plain, None

    return body, None


def _ensure_db() -> None:
    p = Path(_db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_db_path()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name     TEXT NOT NULL,
                event        TEXT NOT NULL,
                user_id      TEXT,
                device_id    TEXT NOT NULL,
                app_version  TEXT,
                os_version   TEXT,
                locale       TEXT,
                props        TEXT,
                ts           TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_app_created       ON events (app_name, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_app_device        ON events (app_name, device_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_app_event_created ON events (app_name, event, created_at);
            """
        )


def _rate_check(device_id: str) -> bool:
    now = time.time()
    with _rate_lock:
        q = _rate_state[device_id]
        while q and now - q[0] > _RATE_WINDOW_S:
            q.popleft()
        if len(q) >= _RATE_LIMIT:
            return False
        q.append(now)
        return True


@bp.post("/track")
def track():
    body, err_resp = _resolve_track_body()
    if err_resp is not None:
        return err_resp
    assert body is not None

    app_name = body.get("app_name")
    if app_name not in ALLOWED_APPS:
        return jsonify(error="unknown_app"), 400

    device_id = (body.get("device_id") or "").strip()
    if not device_id:
        return jsonify(error="missing_device_id"), 400

    events: list[dict[str, Any]] = body.get("events") or []
    if not isinstance(events, list) or not events:
        return jsonify(error="missing_events"), 400

    if not _rate_check(device_id):
        return jsonify(ok=True, accepted=0, dropped=len(events), reason="rate_limited")

    events = events[:MAX_EVENTS_PER_REQUEST]

    common = (
        app_name,
        (body.get("user_id") or None),
        device_id,
        body.get("app_version"),
        body.get("os_version"),
        body.get("locale"),
    )
    now = _utc_now()

    _ensure_db()
    accepted = 0
    dropped = 0
    with sqlite3.connect(_db_path()) as conn:
        for e in events:
            ev = (e.get("event") or "").strip()
            if not ev:
                dropped += 1
                continue
            props_raw = e.get("props")
            props_str = json.dumps(props_raw, ensure_ascii=False) if props_raw else None
            if props_str and len(props_str.encode("utf-8")) > MAX_PROPS_BYTES:
                dropped += 1
                continue
            ts = e.get("ts") or now
            conn.execute(
                """INSERT INTO events (app_name, user_id, device_id, app_version, os_version, locale,
                                       event, props, ts, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*common, ev, props_str, ts, now),
            )
            accepted += 1

    return jsonify(ok=True, accepted=accepted, dropped=dropped)


@bp.get("/stats")
@require_api_key
def stats():
    """DAU / 事件分布 / 版本分布。日界统一按北京时间 (UTC+8) 切。"""
    app_name = request.args.get("app")
    if app_name and app_name not in ALLOWED_APPS:
        return jsonify(error="unknown_app"), 400
    try:
        days = int(request.args.get("days", 30))
    except ValueError:
        return jsonify(error="invalid_days"), 400
    days = max(1, min(90, days))

    _ensure_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row

        bj_day = "strftime('%Y-%m-%d', created_at, '+8 hours')"
        where = "created_at >= datetime('now', ?)"
        args_since = (f"-{days} days",)
        app_clause = ""
        app_args: tuple = ()
        if app_name:
            app_clause = " AND app_name = ?"
            app_args = (app_name,)

        series_rows = conn.execute(
            f"""SELECT {bj_day} AS date,
                       COUNT(DISTINCT CASE WHEN user_id IS NOT NULL THEN user_id END) AS user_dau,
                       COUNT(DISTINCT device_id) AS device_dau,
                       COUNT(*) AS events
                 FROM events
                WHERE {where}{app_clause}
                GROUP BY 1 ORDER BY 1""",
            (*args_since, *app_args),
        ).fetchall()

        event_rows = conn.execute(
            f"""SELECT event, COUNT(*) AS cnt
                 FROM events
                WHERE {where}{app_clause}
                GROUP BY event ORDER BY cnt DESC LIMIT 50""",
            (*args_since, *app_args),
        ).fetchall()

        ver_rows = conn.execute(
            f"""SELECT app_version AS version, COUNT(DISTINCT device_id) AS devices
                 FROM events
                WHERE {where}{app_clause} AND app_version IS NOT NULL
                GROUP BY app_version ORDER BY devices DESC""",
            (*args_since, *app_args),
        ).fetchall()

        daily_rows = conn.execute(
            f"""SELECT {bj_day} AS date, event, COUNT(*) AS cnt
                 FROM events
                WHERE {where}{app_clause}
                GROUP BY 1, 2 ORDER BY 1""",
            (*args_since, *app_args),
        ).fetchall()

    # 事件按天分布：只展开期间总量 Top-N 的事件，长尾并入 "__other__"
    # （控制 payload 与前端图例规模；event_rows 已按总量降序，前 N 个即 Top-N）
    TOP_EVENTS_DAILY = 8
    top_events = {r["event"] for r in event_rows[:TOP_EVENTS_DAILY]}
    daily_agg: dict[tuple[str, str], int] = {}
    for r in daily_rows:
        key = (r["date"], r["event"] if r["event"] in top_events else "__other__")
        daily_agg[key] = daily_agg.get(key, 0) + r["cnt"]

    series = [dict(r) for r in series_rows]
    return jsonify(
        app=app_name,
        days=days,
        series=series,
        event_totals=[{"event": r["event"], "count": r["cnt"]} for r in event_rows],
        event_daily=[
            {"date": d, "event": e, "count": c}
            for (d, e), c in sorted(daily_agg.items())
        ],
        version_distribution=[{"version": r["version"], "devices": r["devices"]} for r in ver_rows],
        totals={
            "user_dau_avg": round(sum(s["user_dau"] for s in series) / len(series), 1) if series else 0,
            "device_dau_avg": round(sum(s["device_dau"] for s in series) / len(series), 1) if series else 0,
            "events": sum(s["events"] for s in series),
        },
    )
