# AnalyticsKit（iOS SDK）

多 App 共用的轻量埋点客户端，直连 `https://analytics.picturebookpedia.cn`。  
**不要再复制** `AnalyticsClient.swift` 到各 App；统一依赖本 SPM。

| 项 | 值 |
|---|---|
| 产品名 | `AnalyticsKit` |
| 最低系统 | iOS 16 |
| 服务端契约 | 仓库根 [README.md](../README.md) 的 `/api/events/track` |
| 参考接入 | `live.ai` → `LiveAI/Sources/Engine/LiveAIAnalytics.swift` |

---

## 1. 加依赖

### XcodeGen（推荐）

仓库与 `analytics-service` **同级**时用 path：

```yaml
packages:
  AnalyticsKit:
    path: ../analytics-service/ios

targets:
  YourApp:
    dependencies:
      - package: AnalyticsKit
        product: AnalyticsKit
```

然后：

```bash
xcodegen generate
```

### 远程 Git（发 tag 后）

```yaml
packages:
  AnalyticsKit:
    url: https://github.com/fairzy/analytics-service
    from: "1.0.0"   # 需在仓根或 ios/ 配好 SPM；若 path 在子目录见下文
```

> 当前 Package 在子目录 `ios/`。Xcode 用 **Add Local…** 选 `analytics-service/ios`；  
> 远程依赖若 SPM 只认仓根，可把 `Package.swift` 挪到仓根或使用 monorepo 的 path 约定。  
> **本团队默认**：各 App 本地 path `../analytics-service/ios`。

### 纯 Xcode

File → Add Package Dependencies → Add Local → 选本目录 `ios/`。

---

## 2. 启动 install（必须最先）

`install` 必须在：

1. 任何 `track` / `flush` / `setUserId` 之前  
2. 任何「清空本 App 全部 Keychain」逻辑之前（否则读不到 clientId）

SwiftUI 注意：`@ObservedObject private var auth = AuthManager.shared` 的属性初始化**早于** `App.init()` 函数体。  
因此 **AuthManager 单例 `init` 第一行**就要 `install`（Live.AI 即如此）。

```swift
import AnalyticsKit

enum YourAppAnalytics {
    static func install() {
        guard !AnalyticsClient.isInstalled else { return }
        AnalyticsClient.install(AnalyticsConfig(
            appName: "liveai",  // 白名单：dinopedia | liveai | animal-friends | earthtrip
            // 与生产 /etc/analytics-service.env 的 ANALYTICS_PAYLOAD_KEY 一致；nil = 明文 body
            payloadKeyHex: "…64 hex chars…",
            keychainService: "your.bundle.analytics",       // 按 App 区分
            keychainAccount: "your.bundle.analyticsClientId",
            legacyInstallIdDefaultsKey: "your.bundle.analyticsInstallId" // 可选迁移
        ))
    }
}

// AuthManager.init 开头
YourAppAnalytics.install()

// App.init 里再调一次也无妨（幂等）
YourAppAnalytics.install()
```

### `AnalyticsConfig` 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `appName` | ✅ | 服务端白名单 |
| `trackURL` | | 默认生产 track 端点 |
| `payloadKeyHex` | | 64 hex → AES-256-GCM；`nil` 明文 |
| `keychainService` | ✅ | 各 App 不同，避免串数据 |
| `keychainAccount` | | 默认 `analyticsClientId` |
| `legacyInstallIdDefaultsKey` | | 旧 UserDefaults 键，有则迁到 Keychain |
| `flushDelayNanos` | | 默认 5s 批量 |
| `appVersionProvider` | | 默认 Bundle short+build |
| `deviceModelProvider` | | 默认 utsname machine |

---

## 3. 打点 API

```swift
import AnalyticsKit

// 登录后绑定（用于 user_dau）
await AnalyticsClient.shared.setUserId(userSub)
await AnalyticsClient.shared.setUserId(nil)   // 登出

// 事件（失败静默，不影响主流程）
await AnalyticsClient.shared.track("app_open", props: [
    "cold": .bool(true),
    "auth": .bool(true),
])
await AnalyticsClient.shared.track("purchase_success", props: [
    "pack": .string("mini"),
    "credits": .int(20),
])

// 进后台时建议冲掉 buffer
await AnalyticsClient.shared.flush()
```

### 中国大陆网络权限弹窗

App 首次启动后，系统可能弹出「是否允许 App 使用无线数据」。用户点「允许」之前所有 HTTPS 都会失败（`NSURLError -1009/-1005` 等）。

SDK 行为：

1. `flush` 失败时把事件**放回 buffer 队头**（不再静默丢弃）
2. 在线时指数退避重试（约 5s → 10s → … → 60s）
3. 用 `NWPathMonitor` 捕捉离线→在线跳变，立即再 `flush`

App 侧无需额外处理；仍建议在进后台时调用 `flush()`。

### `AnalyticsValue`

- `.string` / `.int` / `.double` / `.bool`  
- 单条 props JSON ≤ 2KB（超限丢该事件）

### 推荐事件（各 App 可对齐口径）

| 事件 | 时机 |
|---|---|
| `app_open` | 冷启动一次 |
| `app_active` | 回前台（可节流） |
| `login_success` / `login_failed` | 登录结果 |
| `logout` | 登出 |
| 业务事件 | 生成 / 付费 / 反馈等按产品定 |

---

## 4. Keychain clientId 与 Auth purge

SDK 用 **Keychain** 存匿名 `device_id`（clientId）：

- 重启不变  
- 卸载重装：iOS 常保留 Keychain → id 可延续  
- **若 App 在「首次安装」时 `SecItemDelete` 全部 GenericPassword**（清 Logto 等），会误删 analytics id  

**正确姿势**（Live.AI `AuthManager`）：

```swift
let preserved = AnalyticsClient.readKeychainClientId()
// purge 全部 GenericPassword …
if let preserved {
    AnalyticsClient.writeKeychainClientId(preserved)
}
```

`install` 必须在 `readKeychainClientId` 之前，否则 config 为空会读到 `nil`。

---

## 5. 加密与明文

| 配置 | 请求体 |
|---|---|
| `payloadKeyHex = nil` | 明文 JSON（DinoPedia 旧客户端兼容） |
| 配置 32-byte hex | `{ v, app_name, enc:"aes-256-gcm", data }` 信封 |

服务端：

- 环境变量 `ANALYTICS_PAYLOAD_KEY`（与客户端同一 hex）  
- 未配密钥时，加密请求 → `503 encryption_not_configured`  
- 明文 track 仍可用  

密钥用途是 **防抓包明文**，不是鉴权；可逆向，勿当 API Key。

生产写入示例见 [deploy/DEPLOY.md](../deploy/DEPLOY.md)。

---

## 6. 按 App 接入清单

### Live.AI（已完成）

- path 依赖 + `LiveAIAnalytics.install()`
- `appName = "liveai"`
- 加密开启；Auth purge 保留 clientId
- 登录 / 生成 / 付费 / 反馈 / `app_open`·`app_active` 已打点

### DinoPedia

1. 删本地 `AnalyticsClient.swift`  
2. `project.yml` 加 AnalyticsKit path  
3. `appName = "dinopedia"`；加密可选（可先 `payloadKeyHex: nil`）  
4. 替换原 track 调用为 `AnalyticsKit`  
5. 若有 Keychain 全清，加 preserve clientId  

### 动物朋友

| App | `appName` |
|---|---|
| 动物朋友 | `animal-friends` |

步骤同 DinoPedia；`keychainService` 用各自 bundle 前缀。

### EarthTrip / Earthpedia（已接入）

- path 依赖：`myresearchs/GlobeBuildingsDemo/...` → `../../../analytics-service/ios`
- `appName = "earthtrip"`；加密开启
- `EarthpediaAnalytics.install()` + `app_open` / `app_active` / 付费 / 反馈
- 版本 ≥ 1.3.1 起有埋点数据

---

## 7. 目录结构

```
ios/
├── Package.swift
├── README.md                 ← 本文
└── Sources/AnalyticsKit/
    ├── AnalyticsClient.swift
    ├── AnalyticsConfig.swift
    └── AnalyticsValue.swift
```

---

## 8. 验证

1. 真机/模拟器跑 App，触发 `app_open`，切后台触发 `flush`  
2. 查 stats（需 API Key）：

```bash
curl -sS -H "X-API-Key: $ANALYTICS_API_KEY" \
  "https://analytics.picturebookpedia.cn/api/events/stats?app=liveai&days=1"
```

3. 应看到真实 `app_version`、稳定 `device_id`（非每次重装都变）、事件名  

加密开启时，Charles 里 body 应只有 `enc`/`data`，无事件明文。

---

## 9. 常见问题

| 现象 | 原因 |
|---|---|
| track 无日志 / 不生效 | 未 `install` |
| 卸载重装 clientId 变了 | Auth purge 未 preserve，或系统清了 Keychain |
| 启动后一段时间才有事件 | 中国区网络权限弹窗未点允许；SDK 会缓存并在恢复后重发 |
| HTTP 503 encryption_not_configured | 客户端加密了但服务端未配 `ANALYTICS_PAYLOAD_KEY` |
| HTTP 400 decrypt_failed | 密钥不一致 |
| 编译找不到 AnalyticsKit | path 不对；需 `analytics-service` 与 App 仓同级 |
| Tab 切换刷任务列表时 CreateView `.task` cancelled | SwiftUI 生命周期噪音，与 SDK 无关 |

更完整的服务端说明见仓库根 [README.md](../README.md)。
