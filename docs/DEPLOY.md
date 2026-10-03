# 部署指南

## A. 現有 Oracle 實例（已部署）

見 [HANDOFF.md](./HANDOFF.md) 第2、6節。後端更新需檢查點與受控重啟；僅UI更新先保存精確web復原包，現有Jinja自動重載，不重啟搬運。復原程式不能覆蓋新增交付的DB。

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
DOWNLOAD_CONNECTIONS=6
TRANSFER_PREFETCH_FILES=3
TRANSFER_STAGING_BYTES=68719476736
DISK_RESERVE_BYTES=2147483648
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
| 安全留量 | 預設2 GiB；不要縮減以強行下載 |
| 現有150GB盤 | 有界预取64 GiB、最多3件；失敗暫存與未知大小也計入安全策略 |
| 小磁碟部署 | 先調整暫存預算；磁碟容量不等於傳輸帶寬 |

---

## E. 備份

應用程式復原與敏感資料備份要分開，敏感備份需加密並限制權限。SQLite使用backup API取得一致副本，不可直接打包正在寫入的DB／WAL／SHM當作可恢復證據。下列僅列必要範圍，不是可直接執行的完整安全備份程序：

```bash
# Oracle內：保存舊程式；SQLite backup()一致副本；必要加密設定。
# 密鑰與DB不輸出到對話、終端記錄或Git。
# .part與metadata按完整清單及校驗證據另行備份。
```

還原程式：先檢查精確復原包內容與當次版本，不能解壓覆蓋正在更新的 DB／暫存。UI-only 恢復 web 不重啟；後端恢復須另走檢查點與受控重啟，保持原密鑰。敏感資料還原是獨立程序，不能以程式復原包代替。
