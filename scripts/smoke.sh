#!/usr/bin/env bash
# 冒烟测试：假设服务已在 $BASE 上跑起来。
# 用法：BASE=http://127.0.0.1:8320 ANALYTICS_API_KEY=xxx bash scripts/smoke.sh
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:8320}"
KEY="${ANALYTICS_API_KEY:-}"

step() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

step "健康检查 GET /healthz"
curl -sS "$BASE/healthz" | tee /dev/stderr | grep -q '"ok":true'

step "POST /api/events/track（正常一条）"
curl -sS -X POST "$BASE/api/events/track" \
  -H 'Content-Type: application/json' \
  -d '{
    "app_name":"dinopedia",
    "device_id":"smoke-device-001",
    "app_version":"9.9.9",
    "os_version":"iOS 17.5",
    "locale":"zh-CN",
    "events":[{"event":"app_open","props":{"cold":true}}]
  }' | tee /dev/stderr | grep -q '"accepted":1'

step "POST /api/events/track（未知 app 应 400）"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/events/track" \
  -H 'Content-Type: application/json' \
  -d '{"app_name":"unknown","device_id":"x","events":[{"event":"e"}]}')
[ "$code" = "400" ] || { echo "expected 400, got $code"; exit 1; }
echo "OK: 400"

step "POST /api/events/track（缺 device_id 应 400）"
code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$BASE/api/events/track" \
  -H 'Content-Type: application/json' \
  -d '{"app_name":"dinopedia","events":[{"event":"e"}]}')
[ "$code" = "400" ] || { echo "expected 400, got $code"; exit 1; }
echo "OK: 400"

if [ -n "${ANALYTICS_PAYLOAD_KEY:-}" ]; then
  step "POST /api/events/track（AES-GCM 信封）"
  python3 - <<'PY' | curl -sS -X POST "$BASE/api/events/track" \
    -H 'Content-Type: application/json' \
    -d @- | tee /dev/stderr | grep -q '"accepted":1'
import base64, json, os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
key = bytes.fromhex(os.environ["ANALYTICS_PAYLOAD_KEY"])
plain = json.dumps({
    "app_name": "liveai",
    "device_id": "smoke-enc-device-001",
    "app_version": "9.9.9",
    "os_version": "iOS 18.0",
    "locale": "zh-CN",
    "events": [{"event": "app_open", "props": {"enc": True}}],
}).encode()
aes = AESGCM(key)
nonce = os.urandom(12)
ct = aes.encrypt(nonce, plain, None)
print(json.dumps({
    "v": 1,
    "app_name": "liveai",
    "enc": "aes-256-gcm",
    "data": base64.b64encode(nonce + ct).decode(),
}))
PY
  echo "OK: encrypted track"
fi

step "GET /api/events/stats（无 key 场景）"
if [ -z "$KEY" ]; then
  echo "  (未配置 ANALYTICS_API_KEY，服务端应允许直接访问)"
  curl -sS "$BASE/api/events/stats?app=dinopedia&days=1" | tee /dev/stderr | grep -q '"app":"dinopedia"'
else
  step "GET /api/events/stats 无 key 应 401"
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/events/stats?app=dinopedia&days=1")
  [ "$code" = "401" ] || { echo "expected 401, got $code"; exit 1; }
  echo "OK: 401"

  step "GET /api/events/stats 带 key 应 200"
  curl -sS -H "X-API-Key: $KEY" "$BASE/api/events/stats?app=dinopedia&days=1" \
    | tee /dev/stderr | grep -q '"app":"dinopedia"'
fi

printf '\n\033[1;32m✓ smoke passed\033[0m\n'
