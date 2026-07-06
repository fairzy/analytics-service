# 部署 analytics_service 到 CVM

目标：`analytics.picturebookpedia.cn` 上线,gunicorn 监听 `127.0.0.1:8320`,Nginx 反代 443。

## 前置

- CVM `43.138.150.181`,用户 `ubuntu`,已装 Python 3.10+ / Nginx / certbot
- DNS 面板已把 `analytics.picturebookpedia.cn` A 记录指向 `43.138.150.181`
- GitHub 私有仓 `analytics_service` 已 push(见根 README 里的地址)

## 一次性步骤

### 1. DNS

在腾讯云 DNS(picturebookpedia.cn):

```
Type: A
Host: analytics
Value: 43.138.150.181
TTL:  600
```

验证:`dig +short analytics.picturebookpedia.cn` 应返回 `43.138.150.181`。

### 2. 服务器上拉代码 + 建 venv

```bash
ssh -i "/Users/fairzyfan/Documents/bank/腾讯云登录私钥mac_mini_login.pem" ubuntu@43.138.150.181

sudo mkdir -p /opt/analytics-service
sudo chown ubuntu:ubuntu /opt/analytics-service
cd /opt/analytics-service
git clone https://github.com/fairzy/analytics-service.git .
# 私有仓 https 拉取时会提示输入 GitHub 用户 + PAT;
# 或先在 CVM 上 gh auth login,或 ssh key 配好后改用 git@github.com:fairzy/analytics-service.git

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. 生成 API Key 并写入环境文件

```bash
# 生成一个 32 字节随机 key
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 记下这个 key,填进 /etc/analytics-service.env

sudo tee /etc/analytics-service.env >/dev/null <<'EOF'
ANALYTICS_API_KEY=<粘上上面生成的 key>
EVENTS_DB_PATH=/opt/analytics-service/data/events.sqlite3
EOF
sudo chmod 640 /etc/analytics-service.env
sudo chown root:ubuntu /etc/analytics-service.env
```

**这个 KEY 要抄一份到 1Password**,后面 appadmin 集成 stats 时用得上。

### 4. 装 systemd 单元

```bash
sudo cp /opt/analytics-service/deploy/analytics-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now analytics-service
sudo systemctl status analytics-service   # active (running)
```

先本地验证 gunicorn 能通:

```bash
curl -sf http://127.0.0.1:8320/healthz
# {"ok":true,"service":"analytics"}
```

### 5. Nginx 反代 + Let's Encrypt

```bash
sudo cp /opt/analytics-service/deploy/nginx.conf.example \
        /etc/nginx/sites-available/analytics.picturebookpedia.cn

# 第一次上线时 SSL 段还没证书,先把 SSL 相关行注释掉,只留 :80 server 段,或者:
# 直接跑 certbot 让它自动改
sudo ln -s /etc/nginx/sites-available/analytics.picturebookpedia.cn /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

sudo certbot --nginx -d analytics.picturebookpedia.cn --agree-tos -m fairzy@gmail.com --redirect
```

certbot 会自动补齐 443 段并挂上证书。再次 `nginx -t && sudo systemctl reload nginx`。

### 6. 公网冒烟

在本地跑:

```bash
BASE=https://analytics.picturebookpedia.cn \
ANALYTICS_API_KEY=<上面生成的 key> \
  bash scripts/smoke.sh
```

看到 `✓ smoke passed` 即上线成功。

## 后续更新流程

```bash
ssh ubuntu@43.138.150.181
cd /opt/analytics-service
git pull
.venv/bin/pip install -r requirements.txt   # 依赖变化才需要
sudo systemctl restart analytics-service
```

## 排错

- **502 Bad Gateway** → gunicorn 没起来:`sudo systemctl status analytics-service` 看日志
- **500 on /api/events/stats** → 大概是 `EVENTS_DB_PATH` 目录不存在或没写权限,检查 `/opt/analytics-service/data/` 属主
- **certbot 报域名未指向服务器** → DNS 还没生效,`dig +short` 确认
