# HANDOVER — analytics 服务独立化工作交接

> 用于换电脑后继续本次会话。读完这个文件应能立刻接着推进 Phase 3。
> 最后更新时间：本次会话结束时

## 一句话背景

之前埋点服务作为 blueprint 挂在 dinasaur_pedia 后端里（`server/api/events.py`）。DinoPedia iOS 已经在打点，其他三个 App（Live.AI / 动物朋友 / EarthTrip）都还没接。本次工作把埋点服务抽成独立仓 + 独立域名，让四个 App 都指向统一入口。

## 关键决策（用户已拍板）

| 决策点 | 结果 |
|---|---|
| 部署方式 | **独立仓 + 独立进程**（不是"挂现有后端 + 独立域名"） |
| 独立域名 | `analytics.picturebookpedia.cn` |
| 端口 | `127.0.0.1:8320`（挨着 appadmin 的 :8310） |
| 栈 | Flask + gunicorn + SQLite（跟 dinasaur_pedia 后端同源） |
| 数据迁移 | 无 — 服务器上原本没有 events.sqlite3 数据 |
| 仓库托管 | GitHub 私有仓 `github.com/fairzy/analytics-service` |
| appadmin 后台集成 | **暂不做**，先让端点跑起来 |
| iOS SDK 是否抽 Swift Package | **已抽**：`ios/AnalyticsKit` SPM，各 App `AnalyticsClient.install(config)` |

## 三阶段计划 & 完成状态

- ✅ **Phase 1 — 本地建仓 + 代码搬迁 + 冒烟**（我全程执行完毕）
- ✅ **Phase 2 — CVM 部署**（我直接 SSH 上去做的，用户当时授权）
- 🟡 **Phase 3 — 客户端接入**：
  - ✅ Task #7 DinoPedia iOS 改指向新域名（一行改动，尚未 commit）
  - ⏸ Task #8 Live.AI 接入
  - ⏸ Task #9 动物朋友接入
  - ⏸ Task #10 EarthTrip 接入
  - ⏸ 端到端验证：等你 Xcode build DinoPedia、切一次后台触发 flush，我 curl 查 stats 对比 baseline

## 生产环境现状（CVM `43.138.150.181`）

| 项目 | 值 |
|---|---|
| 代码路径 | `/opt/analytics-service`（属主 ubuntu） |
| Python venv | `/opt/analytics-service/.venv` |
| 环境文件 | `/etc/analytics-service.env`（mode 640, root:ubuntu） |
| 数据库 | `/opt/analytics-service/data/events.sqlite3` |
| systemd unit | `/etc/systemd/system/analytics-service.service` → `systemctl status analytics-service` |
| Nginx 配置 | `/usr/local/nginx/conf/sites-available/analytics-service.conf`（.conf 后缀必需，openresty 混合布局） |
| SSL 证书 | Let's Encrypt，`/etc/letsencrypt/live/analytics.picturebookpedia.cn/`，到期 2026-10-04，certbot timer 自动 renew |

## `ANALYTICS_API_KEY` 怎么取回

**不写进 md**（避免 KEY 泄漏到仓）。三种拿回路径：

1. **CVM 上直接查**（最快）：
   ```bash
   ssh -i "/Users/fairzyfan/Documents/bank/腾讯云登录私钥mac_mini_login.pem" \
       ubuntu@43.138.150.181 'sudo cat /etc/analytics-service.env'
   ```
2. 上一台电脑的 scratchpad：`/private/tmp/claude-501/.../scratchpad/analytics-api-key.txt`（换机后就没了）
3. 1Password（如果你抄进去了）

用途：查 `GET /api/events/stats?app=<name>` 时 `X-API-Key` 请求头带上；`POST /api/events/track` 不需要。

## Git 仓状态

- `analytics_service`（`github.com/fairzy/analytics-service`，main）
  - 5+ 个 commit（最后一个是 HANDOVER.md 本身）
- `dinasaur_pedia`（`github.com/fairzy/dinasaur_pedia`，main）
  - **两个新 commit**：
    - `wip(ios): 安全加固进行中` — 用户 pre-staged 的（数据库加密 / 白盒 key / 反 Hook / bridging header / Podfile / xcworkspace / gitignore 兜底）
    - `feat(analytics): 埋点服务独立 + DinoPedia 切换到 analytics 域名` — 本次会话产出
      - 删 `server/api/events.py`
      - 摘 `server/api/__init__.py` events blueprint 注册
      - 摘 `server/config.py` / `server/.env.example` 的 EVENTS_DB_PATH
      - `docs/analytics.md` 顶部加迁移提示
      - `AnalyticsClient.swift:10` endpoint 由 `dino.picturebookpedia.cn` 改为 `analytics.picturebookpedia.cn`

## 换电脑后要做什么才能继续

1. `git clone` 两个仓到本地
2. 用 `dinasaur_pedia/` 作为工作目录起会话（跟本次会话一致）
3. 读完这份 HANDOVER.md
4. 如果需要 KEY：SSH 到 CVM 取
5. 从 Task #8 开始（Live.AI 接入）— 或先跑 DinoPedia 端到端验证

## 下一步 TODO 清单

**验证端到端（你手动）**：
- Xcode build & run DinoPedia → 首屏出来后按 Home 切后台（触发 flush）
- 我这边 curl `stats?app=dinopedia&days=1` 对比 baseline，看是否出现真实版本号 + 新 device_id
- baseline：`event_totals`=[app_open:1, deploy_smoke:1]，version=[9.9.9:2]

**Phase 3 Task #8 — Live.AI 接入**：
- 位置：`/Volumes/PNY_CS2340_1TB/github/live.ai/`（iOS 端）
- 复制 `dinasaur_pedia/ios_app/DinoPedia/Services/AnalyticsClient.swift`
- 改 `appName = "liveai"`
- endpoint 已经是 `analytics.picturebookpedia.cn`
- 参考 `dinasaur_pedia/ios_app/DinoPedia/App/DinoPediaApp.swift` 的 scenePhase 挂法照抄

**Phase 3 Task #9 — 动物朋友接入**：
- 待确认：`call_my_animal_friend` 还是 `my-animal-friends` 是当前活跃仓
- `appName = "animal-friends"`

**Phase 3 Task #10 — EarthTrip 接入**：
- 位置：`/Volumes/PNY_CS2340_1TB/github/earthpedia/`
- `appName = "earthtrip"`

## 踩过的坑（省下次会话时间）

1. **CVM 上 nginx 是 openresty + apt 双装**
   - 主进程是 `/usr/sbin/nginx`，但 nginx.conf 里 `include /usr/local/nginx/conf/sites-available/*.conf`
   - 站点 conf 必须**放 `/usr/local/nginx/conf/sites-available/`** 且**必须以 `.conf` 结尾**
   - 我第一次放错位置 + 无后缀，nginx 没 include，浪费时间调试

2. **certbot 没装 --nginx 插件**
   - 不能用 `certbot --nginx`
   - 用 webroot 模式：`certbot certonly --webroot -w /var/www/certbot -d <domain>`
   - 分两阶段：先挂 `:80` conf → certbot 拿证 → 换成 `:80 + :443` conf

3. **私有仓 CVM 上 clone 需要 GitHub 凭据**
   - CVM 上没配 PAT，直接 clone 会卡认证
   - 首次部署用**本地 rsync push** 到 `/opt/analytics-service/` 最省事
   - 之后更新可以在 CVM 上 gh auth 或配 SSH key，然后 `git pull`

4. **CVM 上 `curl -sI https://...` exit code 60**
   - CVM 本地 curl CA bundle 有老问题
   - 忽略即可，公网访问不受影响，用本地 mac 的 curl 冒烟就行

5. **zsh 变量展开无自动分词**
   - `SSH_OPTS='-i xxx -o yyy'; ssh $SSH_OPTS ...` 会把整串当成 `-i` 的值
   - 解决：直接展开每个参数，不用变量拼

6. **SourceKit 报 "Failed to build module 'Foundation'"**
   - 无关本次埋点改动，是 iOS 项目里加固相关 WIP（bridging header + .c/.h）导致 SourceKit 索引不稳
   - `xcodegen generate` + 清 DerivedData 应该能恢复

## 项目参考

- `README.md` — 服务本身的设计与端点契约
- `deploy/DEPLOY.md` — 部署步骤（含所有坑的规避）
- `dinasaur_pedia/docs/analytics.md` — 埋点服务的历史设计文档（选型对比 / 演进路径）
