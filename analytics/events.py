"""Events Blueprint — 多 App 统一埋点 / 日活。

端点：
  POST /api/events/track   — 无鉴权，SDK 直发（简单速率限制）
  GET  /api/events/stats   — X-API-Key 保护，DAU / 事件分布 / 版本分布

设计取舍见 README.md。SQLite 单表宽结构，`app_name` 字段区分 App。
"""
from __future__ import annotations

import json
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


def _db_path() -> str:
    return current_app.config["EVENTS_DB_PATH"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    body = request.get_json(silent=True) or {}
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

    series = [dict(r) for r in series_rows]
    return jsonify(
        app=app_name,
        days=days,
        series=series,
        event_totals=[{"event": r["event"], "count": r["cnt"]} for r in event_rows],
        version_distribution=[{"version": r["version"], "devices": r["devices"]} for r in ver_rows],
        totals={
            "user_dau_avg": round(sum(s["user_dau"] for s in series) / len(series), 1) if series else 0,
            "device_dau_avg": round(sum(s["device_dau"] for s in series) / len(series), 1) if series else 0,
            "events": sum(s["events"] for s in series),
        },
    )
