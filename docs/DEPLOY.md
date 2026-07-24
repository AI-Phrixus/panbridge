# 部署指南

## A. 現有 Oracle 實例（已部署）

見 [HANDOFF.md](./HANDOFF.md) 第 2、6 節。最快路徑是 **rsync 程式碼 + systemctl restart**。

---

## B. 從零：Ubuntu 22.04/24.04 VPS

### 1. 系統

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip git curl
```

### 2. 程式

```bash
sudo useradd -m -s /bin/bash ubuntu   # 若尚無
sudo -u ubuntu -i
cd ~
git clone <REPO_URL> panbridge
cd panbridge
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 可選掃碼：
# playwright install-deps chromium && playwright install chromium

cp .env.example .env
nano .env   # 設定 PANBRIDGE_SECRET、ADMIN_PASSWORD、DATA_DIR
mkdir -p data
```

建議生產 `.env`：

```env
PANBRIDGE_SECRET=<openssl rand -hex 32>
ADMIN_PASSWORD=<強口令>
HOST=0.0.0.0
PORT=8080
DATA_DIR=/home/ubuntu/panbridge/data
MAX_CONCURRENT_JOBS=1
DOWNLOAD_CONNECTIONS=2
PCLOUD_API_HOST=eapi.pcloud.com
PCLOUD_DEFAULT_PATH=/PanBridge
```

### 3. systemd

```bash
sudo tee /etc/systemd/system/panbridge.service >/dev/null <<'EOF'
[Unit]
Description=PanBridge transfer service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/panbridge
EnvironmentFile=/home/ubuntu/panbridge/.env
ExecStart=/home/ubuntu/panbridge/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=5
MemoryMax=800M

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now panbridge
curl -s http://127.0.0.1:8080/api/health
```

### 4. 防火牆 / OCI 安全列表

放行入站 **TCP 8080**（或僅透過 Cloudflare Tunnel，不開公網埠）。

### 5. （可選）Cloudflare Tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

---

## C. Docker

```bash
cp .env.example .env
# 編輯密鑰
docker compose up -d --build
# 資料在 volume panbridge-data
```

注意：Docker 映像含 Playwright 依賴，體積較大；Oracle free 小機可用 **venv+systemd** 更省事。

---

## D. 磁碟規劃

| 用途 | 建議 |
|------|------|
| 系統 + 程式 | 5–8 GB |
| 單任務最大檔 | 需 ≤ 可用空間 − ~1GB 預留 |
| 50GB free tier | 單檔 ≤ ~25–30GB 較穩；下完上傳後會刪 tmp |

---

## E. 備份

最少備份：

```bash
# 在 VPS
tar czf panbridge-backup-$(date +%F).tgz \
  /home/ubuntu/panbridge/.env \
  /home/ubuntu/panbridge/data/app.db \
  /home/ubuntu/panbridge/data/app.db-wal \
  /home/ubuntu/panbridge/data/app.db-shm
# 進行中的大檔 .part 可選（很大）
```

還原：解壓到原路徑，確認 `PANBRIDGE_SECRET` 不變，`systemctl restart panbridge`。
