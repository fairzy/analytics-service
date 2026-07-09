# HANDOVER — analytics 服务

> 换电脑后先读本文 + 根 [README.md](README.md) + [ios/README.md](ios/README.md)。

## 现状一句话

独立仓 + 独立域名的多 App 埋点服务；iOS 侧已抽出 **AnalyticsKit** SPM。  
Live.AI 已接入；DinoPedia / 动物朋友 / EarthTrip 待迁到 SPM。

## 关键决策

| 决策点 | 结果 |
|---|---|
| 部署 | 独立仓 + 独立进程，`analytics.picturebookpedia.cn`，本机 `127.0.0.1:8320` |
| 栈 | Flask + gunicorn + SQLite |
| 仓库 | `github.com/fairzy/analytics-service`（私有） |
| iOS SDK | **`ios/AnalyticsKit` SPM**（禁止再复制单文件） |
| 加密 | 可选 AES-256-GCM；env `ANALYTICS_PAYLOAD_KEY` |
| stats | `X-API-Key` = `ANALYTICS_API_KEY`；统一后台可代理 |

## 完成状态

- ✅ Phase 1 — 建仓 + 冒烟  
- ✅ Phase 2 — CVM 部署  
- ✅ Phase 3 部分 — **Live.AI** 接入 AnalyticsKit + 加密上报  
- ⏸ DinoPedia 改为 AnalyticsKit（删本地 `AnalyticsClient.swift`）  
- ⏸ 动物朋友 `appName=animal-friends`  
- ⏸ EarthTrip `appName=earthtrip`  

## 生产（CVM `43.138.150.181`）

| 项 | 值 |
|---|---|
| 代码 | `/opt/analytics-service` |
| venv | `/opt/analytics-service/.venv` |
| env | `/etc/analytics-service.env`（含 `ANALYTICS_API_KEY`、`ANALYTICS_PAYLOAD_KEY`、`EVENTS_DB_PATH`） |
| DB | `/opt/analytics-service/data/events.sqlite3` |
| systemd | `analytics-service` |
| Nginx | `/usr/local/nginx/conf/sites-available/analytics-service.conf` |

```bash
ssh -i "/Users/harmony01/Documents/bank/腾讯云登录私钥mac_mini_login.pem" ubuntu@43.138.150.181
# 或 fairzy 路径下的同名 pem
sudo cat /etc/analytics-service.env   # 取 KEY，勿写进 git
sudo systemctl status analytics-service
```

发版服务端前：`pip install -r requirements.txt`（需 `cryptography`），确认 env 有 `ANALYTICS_PAYLOAD_KEY`，再 restart。

## 文档索引

| 文档 | 内容 |
|---|---|
| [README.md](README.md) | 架构、HTTP 契约、部署入口 |
| [ios/README.md](ios/README.md) | **AnalyticsKit 接入全文** |
| [deploy/DEPLOY.md](deploy/DEPLOY.md) | CVM 部署步骤 + PAYLOAD_KEY |

## 下一 App 接入（复制清单）

1. App 仓与 `analytics-service` 同级 clone  
2. `project.yml` path 依赖 `../analytics-service/ios`，`xcodegen generate`  
3. 写 `XxxAnalytics.install()`（appName / keychain / 可选 payloadKey）  
4. Auth 冷启动 purge 时 `readKeychainClientId` → purge → `writeKeychainClientId`  
5. 替换/接上 `track` / `setUserId` / 后台 `flush`  
6. 跑一遍，curl `stats?app=<name>&days=1` 验收  

细节与 FAQ → [ios/README.md](ios/README.md)。

## 踩过的坑（摘要）

1. Nginx 站点必须放 openresty 的 `sites-available/*.conf`  
2. certbot 用 webroot，不要 `--nginx`  
3. 客户端加密后生产必须配 `ANALYTICS_PAYLOAD_KEY`，否则 503  
4. SwiftUI 属性包装器早于 `App.init`，**install 要放 AuthManager.init 开头**  
5. 全量 Keychain purge 会删 clientId，必须 preserve  
