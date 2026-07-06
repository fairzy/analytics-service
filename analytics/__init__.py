"""analytics — 多 App 统一埋点 / 日活服务。"""
from __future__ import annotations

from functools import wraps

from flask import current_app, jsonify, request


def require_api_key(f):
    """校验请求头 X-API-Key。config.API_KEY 为空时跳过（本地开发）。"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("API_KEY", "")
        if expected:
            key = request.headers.get("X-API-Key", "")
            if key != expected:
                return jsonify(error="Unauthorized"), 401
        return f(*args, **kwargs)
    return wrapper
