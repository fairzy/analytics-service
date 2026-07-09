"""Flask entrypoint for the analytics service.

Run locally:
    python app.py

Run in prod (via gunicorn):
    gunicorn -w $WORKERS -b $BIND app:app
"""
from __future__ import annotations

import os

from flask import Flask, jsonify


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["API_KEY"] = os.environ.get("ANALYTICS_API_KEY", "")
    app.config["EVENTS_DB_PATH"] = os.environ.get("EVENTS_DB_PATH", "data/events.sqlite3")
    # AES-256-GCM payload key（64 hex chars）。未配置时仍接受明文 track；加密信封会 503。
    app.config["PAYLOAD_KEY"] = os.environ.get("ANALYTICS_PAYLOAD_KEY", "")

    from analytics.events import bp as events_bp
    app.register_blueprint(events_bp, url_prefix="/api/events")

    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True, service="analytics")

    return app


app = create_app()


if __name__ == "__main__":
    bind = os.environ.get("BIND", "127.0.0.1:8320")
    host, port = bind.split(":", 1)
    app.run(host=host, port=int(port), debug=os.environ.get("DEBUG") == "1")
