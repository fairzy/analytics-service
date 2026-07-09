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

#### 明文请求体（兼容旧客户端）

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

#### 加密信封（Live.AI 等新客户端）

环境变量 `ANALYTICS_PAYLOAD_KEY` = 64 hex（32-byte AES key）。客户端用 **AES-256-GCM** 加密明文 JSON，再包装：

```json
{
  "v": 1,
  "app_name": "liveai",
  "enc": "aes-256-gcm",
  "data": "<base64(nonce||ciphertext||tag)>"
}
```

`data` 为 CryptoKit `AES.GCM.SealedBox.combined`（12 字节 nonce + ciphertext + 16 字节 tag）。解密后的明文结构与上面的明文 body 相同。未配置密钥时加密请求返回 `503 encryption_not_configured`。

响应：`{ "ok": true, "accepted": 1, "dropped": 0 }`

### `GET /api/events/stats?app=dinopedia&days=30`

`X-API-Key` 保护。返回 DAU / 事件分布 / 版本分布，日界按北京时间（UTC+8）切。

## iOS SDK（AnalyticsKit）

Swift Package 在本仓 `ios/`，各 App **不要再复制** `AnalyticsClient.swift`。

### 接入

**XcodeGen `project.yml`：**

```yaml
packages:
  AnalyticsKit:
    path: ../analytics-service/ios   # 或 git URL + from version
dependencies:
  - package: AnalyticsKit
    product: AnalyticsKit
```

**启动时 install（须在任何 track / Keychain purge 之前）：**

```swift
import AnalyticsKit

AnalyticsClient.install(AnalyticsConfig(
    appName: "liveai",                          // dinopedia / animal-friends / earthtrip
    payloadKeyHex: "<64 hex 或 nil=明文>",       // 与 ANALYTICS_PAYLOAD_KEY 一致
    keychainService: "ai.talent.liveai.analytics",
    keychainAccount: "ai.talent.liveai.analyticsClientId", // 可选
    legacyInstallIdDefaultsKey: "…"             // 可选，迁移旧 UserDefaults
))

// 打点
await AnalyticsClient.shared.setUserId(sub)
await AnalyticsClient.shared.track("app_open", props: ["cold": .bool(true)])
await AnalyticsClient.shared.flush()            // 进后台时建议调用
```

**Auth 冷启动 purge 全部 Keychain 时**，保留匿名 id：

```swift
let id = AnalyticsClient.readKeychainClientId()
// … SecItemDelete 全部 GenericPassword …
if let id { AnalyticsClient.writeKeychainClientId(id) }
```

### 能力

| 能力 | 说明 |
|---|---|
| clientId | Keychain UUID，重启/卸载重装尽量不变 |
| 批量 | 默认 5s 窗口，失败静默 |
| 加密 | 配置 `payloadKeyHex` 后 AES-256-GCM 信封 |
| props | `AnalyticsValue`：string / int / double / bool |

参考实现：`live.ai` 的 `LiveAIAnalytics.swift`。

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
