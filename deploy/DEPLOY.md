# 部署 analytics_service 到 CVM

目标：`analytics.picturebookpedia.cn` 上线，gunicorn 监听 `127.0.0.1:8320`，Nginx 反代 443。

## 这台 CVM 的坑（先看再动手）

- **主 nginx 进程**：apt 版 `/usr/sbin/nginx`（systemd `nginx.service` 管理）
- **配置目录**：nginx.conf `include /usr/local/nginx/conf/sites-available/*.conf;`
  → 站点文件必须放 `/usr/local/nginx/conf/sites-available/` 且必须以 `.conf` 结尾
- **证书方式**：webroot 模式，webroot 目录 `/var/www/certbot`
- 不要用 `certbot --nginx`（没装 `python3-certbot-nginx` 插件），一律 `certonly --webroot`

## 前置

- CVM `43.138.150.181`，用户 `ubuntu`
- DNS `analytics.picturebookpedia.cn` A 记录已指向 `43.138.150.181`（`dig +short` 验证）
- GitHub 私有仓 `github.com/fairzy/analytics-service`

## 一次性步骤

### 1. 拉代码 + venv

```bash
sudo mkdir -p /opt/analytics-service
sudo chown ubuntu:ubuntu /opt/analytics-service
cd /opt/analytics-service

# 方式 A：从本地 rsync 推（免在 CVM 上配 GitHub 凭据）
#   本地：rsync -az --delete --exclude='.git' --exclude='.venv' \
#         --exclude='__pycache__' --exclude='data/events.sqlite3*' \
#         -e "ssh -i <pem>" ./ ubuntu@43.138.150.181:/opt/analytics-service/

# 方式 B：CVM 上 clone（需要先 gh auth login 或配 SSH key）
git clone https://github.com/fairzy/analytics-service.git .

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. 生成 API Key + 环境文件

```bash
KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "记好 API KEY: $KEY"    # ⚠️ 抄进 1Password，appadmin 集成 stats 时用

# Live.AI 等加密客户端用的 AES-256 key（64 hex）。必须与 iOS AnalyticsClient.payloadKeyHex 一致。
# 若线上已有约定密钥，直接填该 hex，不要重新生成。
PAYLOAD_KEY=e8c2a91f4b7d6035a1e9f2c84d6b0e37a5f1c8d29b4e7063f0a8d1c5e9b27f4a

sudo tee /etc/analytics-service.env >/dev/null <<EOF
ANALYTICS_API_KEY=$KEY
ANALYTICS_PAYLOAD_KEY=$PAYLOAD_KEY
EVENTS_DB_PATH=/opt/analytics-service/data/events.sqlite3
EOF
sudo chmod 640 /etc/analytics-service.env
sudo chown root:ubuntu /etc/analytics-service.env
```

### 3. systemd

```bash
sudo cp deploy/analytics-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now analytics-service
sudo systemctl is-active analytics-service          # → active
curl -sf http://127.0.0.1:8320/healthz              # {"ok":true,...}
```

### 4. Nginx 阶段 A：仅 :80，为 certbot 让路

```bash
CONF=/usr/local/nginx/conf/sites-available/analytics-service.conf
sudo tee $CONF >/dev/null <<'NGX'
server {
    listen 80;
    server_name analytics.picturebookpedia.cn;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }
    location / {
        proxy_pass http://127.0.0.1:8320;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGX
sudo nginx -t && sudo systemctl reload nginx
```

### 5. certbot 签证书（webroot 模式）

```bash
sudo certbot certonly --webroot -w /var/www/certbot \
  -d analytics.picturebookpedia.cn \
  --agree-tos -m fairzy@gmail.com --non-interactive
# → Certificate is saved at /etc/letsencrypt/live/analytics.picturebookpedia.cn/
```

### 6. Nginx 阶段 B：切换到完整 :80+:443 版

```bash
sudo cp deploy/nginx.conf.example \
        /usr/local/nginx/conf/sites-available/analytics-service.conf
sudo nginx -t && sudo systemctl reload nginx
```

### 7. 公网冒烟

在本地跑：

```bash
BASE=https://analytics.picturebookpedia.cn \
ANALYTICS_API_KEY=<上面记的 KEY> \
  bash scripts/smoke.sh
```

看到 `✓ smoke passed` = 上线成功。

## 后续更新流程

```bash
ssh ubuntu@43.138.150.181
cd /opt/analytics-service
git pull   # 或本地 rsync 推
.venv/bin/pip install -r requirements.txt   # 依赖变化才需要
sudo systemctl restart analytics-service
```

Nginx conf 有改动的时候：

```bash
sudo cp deploy/nginx.conf.example /usr/local/nginx/conf/sites-available/analytics-service.conf
sudo nginx -t && sudo systemctl reload nginx
```

## 证书 renew

certbot 装的时候会自动挂 systemd timer，`systemctl list-timers | grep certbot` 能看到。
renew 用同一个 webroot，跟其他子域共享 `/var/www/certbot`，不用手工干预。

## 排错

- **`nginx -t` 报 include 里找不到文件** → conf 后缀不是 `.conf`，改名
- **certbot 报 `acme-challenge` 404** → 阶段 A 的 `:80` 段没先 reload，或 webroot 路径写错
- **`sudo systemctl status analytics-service` 见 gunicorn 起不来** → 看 journalctl `-u analytics-service -n 50`，通常是 `/opt/analytics-service/data/` 属主问题或 `.env` 缺 KEY
- **`https://analytics.picturebookpedia.cn` 502** → 后端 gunicorn 挂了或端口不对
- **公网通、CVM 上 `curl` exit 60** → 是 CVM 本地 CA bundle 的老问题，公网访问不受影响，忽略
