# analytics_service

多 App 通用的轻量埋点收集服务。四个 App（DinoPedia / Live.AI / 动物朋友 / EarthTrip）打点到同一个端点，靠 `app_name` 字段区分。

- **域名**：`https://analytics.picturebookpedia.cn`
- **栈**：Flask + gunicorn + SQLite
- **前身**：从 `dinasaur_pedia/server/api/events.py` 抽出为独立服务，历史设计文档见 `dinasaur_pedia/docs/analytics.md`

## 为什么自建（而不是 PostHog / Aptabase）

只要 DAU / 事件分布 / 版本分布，不要 funnel/留存平台。上百万事件/天以内 SQLite 单表够用；到量级瓶颈再迁 Postgres。选自建 = 少一套 ClickHouse+Kafka 的运维负担。

## 架构

```
iOS App (DinoPedia)     ─┐
iOS App (Live.AI)       ─┼─HTTPS─▶ analytics.picturebookpedia.cn/api/events/track ─▶ events.sqlite3
iOS App (动物朋友)       ─┤
iOS App (EarthTrip)     ─┘                                                            │
                                                                                      ▼
                                       统一后台 ◀── GET /api/events/stats（X-API-Key）
```

## 端点

### `POST /api/events/track`

无鉴权，SDK 直发。单请求最多 100 条事件；每条 props 上限 2KB；每 device_id 每 60 秒 60 条速率限制。

请求体：

```json
{
  "app_name": "dinopedia",
  "device_id": "IDFV",
  "user_id": "apple:xxx",
  "app_version": "1.2.0",
  "os_version": "iOS 17.5",
  "locale": "zh-CN",
  "events": [
    { "event": "app_open", "ts": "2026-07-06T12:00:00Z", "props": { "cold": true } }
  ]
}
```

响应：`{ "ok": true, "accepted": 1, "dropped": 0 }`

### `GET /api/events/stats?app=dinopedia&days=30`

`X-API-Key` 保护。返回 DAU / 事件分布 / 版本分布，日界按北京时间（UTC+8）切。

## 本地开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # 本地留空 ANALYTICS_API_KEY 即跳过鉴权
python app.py           # 监听 127.0.0.1:8320
```

冒烟：

```bash
bash scripts/smoke.sh
```

## 部署

见 `deploy/DEPLOY.md`。生产上跑 gunicorn + systemd，Nginx 反代 443 → 8320。

## SSH 到部署机（CVM `43.138.150.181`）

这台 CVM 同时跑着 dinasaur_pedia 后端 / live.ai 后端 / picturebookpedia-api / logto / appadmin / analytics-service，SSH 姿势统一。

### 直连（部署 / 排错）

```bash
ssh -i "/Users/fairzyfan/Documents/bank/腾讯云登录私钥mac_mini_login.pem" \
    ubuntu@43.138.150.181
```

### 端口转发到本地（访问不对公网暴露的管理台）

```bash
# Logto 管理台 http://localhost:3002/console
ssh -i "/Users/fairzyfan/Documents/bank/腾讯云登录私钥mac_mini_login.pem" \
    -L 3002:localhost:3002 -N ubuntu@43.138.150.181

# analytics 本地（调 gunicorn 直连时用）
ssh -i "/Users/fairzyfan/Documents/bank/腾讯云登录私钥mac_mini_login.pem" \
    -L 8320:localhost:8320 -N ubuntu@43.138.150.181
```

保持终端不关闭，浏览器/curl 访问对应 localhost 端口。

## 数据库演进

- 单表 `events`，索引覆盖 `(app_name, created_at)` / `(app_name, device_id, created_at)` / `(app_name, event, created_at)`
- 每天百万事件以内 SQLite 单进程完全够
- 顶到 SQLite 上限时的迁移方向：Postgres（同一份 schema，改 DSN 即可）或按月分表
