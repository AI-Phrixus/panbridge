# PanBridge 交接手冊（新帳號接手必讀）

> 版本：**v0.3.5**（以 `/api/health` 為準）  
> 最後更新：2026-07-24  
> 目的：讓**全新 GitHub / 開發環境**在不依賴舊對話上下文的情況下，能接手運維與開發。

---

## 1. 這是什麼

**PanBridge** = 自建「網盤中繼」：

```
夸克 / 百度 分享連結  →  VPS 下載（斷點續傳）  →  OneDrive / pCloud / 本機暫存
```

- Web UI（繁體為主）貼連結 → 後台 worker 自動跑  
- **不依賴你的筆電開機**（任務在 Oracle VPS）  
- v1 **不做**完成通知  

---

## 2. 生產環境（Oracle Free · 大阪）

| 項目 | 值 |
|------|-----|
| 公網 IP | `152.70.86.29` |
| 區域 | Oracle Cloud · Osaka（建議亞太） |
| 實例 | Oracle Free 小規格 / **x86_64**（以 `uname -m` 為準）/ ~50GB 盤 |
| SSH | `ssh ubuntu@152.70.86.29`（用你 OCI 的私鑰） |
| 程式目錄 | `/home/ubuntu/panbridge` |
| 資料目錄 | `/home/ubuntu/panbridge/data`（DB + 暫存，**勿當 git 倉庫**） |
| 虛擬環境 | `/home/ubuntu/panbridge/.venv` |
| 服務 | `systemd` unit：`panbridge` |
| 埠 | `8080` |
| 健康檢查 | `http://152.70.86.29:8080/api/health` 或 `/health` |
| UI | `http://152.70.86.29:8080` |

### 服務指令

```bash
ssh ubuntu@152.70.86.29

sudo systemctl status panbridge
sudo systemctl restart panbridge   # 會中斷當前下載，但會從 .part 續傳
sudo journalctl -u panbridge -f

# 版本
curl -s http://127.0.0.1:8080/api/health
```

### systemd 單元（摘要）

路徑：`/etc/systemd/system/panbridge.service`

- `WorkingDirectory=/home/ubuntu/panbridge`
- `EnvironmentFile=/home/ubuntu/panbridge/.env`
- `ExecStart=.../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080`
- `Restart=always` · `MemoryMax=800M`

### 密鑰與口令（只在伺服器，不上 Git）

```bash
# 在 VPS 上查看（不要貼到公開 issue / 公開 repo）
sudo cat /home/ubuntu/panbridge/.env
```

常見鍵：

- `PANBRIDGE_SECRET` — 加密 DB 內 cookie/token  
- `ADMIN_PASSWORD` — 網頁登入口令  
- `DATA_DIR=/home/ubuntu/panbridge/data`  
- `MAX_CONCURRENT_JOBS=1`  

**帳號憑證**（百度 / 夸克 / pCloud / OneDrive）加密存在 SQLite：

```text
/home/ubuntu/panbridge/data/app.db  →  table credentials
```

若換機器但沿用同一 `PANBRIDGE_SECRET` + 複製整個 `data/`，憑證可繼續用。  
**換 secret 會導致舊憑證無法解密 → 需重新在設定頁登入。**

---

## 3. 交接當下任務狀態

> **權威快照（可公開、會迭代）**：[STATUS.md](./STATUS.md)  
> 以下為摘要；接手後**必須**再查實時數據。

| Job | 狀態 | 說明 |
|-----|------|------|
| #1 | `done` | 夸克測試任務 |
| #2 | `downloading` → **onedrive** | 百度「揭秘日 / Disclosure Day」· 主檔 **~24.83 GB** |

- 大檔 id=6：`.part` 在 `data/tmp/2/6_*.mkv.part`  
- 快照量級：已下約 **數百 MB**（見 STATUS）；百度限速下可能需 **很長時間**  
- 小圖部分已 `done`；字幕/其餘 `queued`  
- **不要**無故 restart；**不要**刪 `.part`

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/home/ubuntu/panbridge/data/app.db")
con.row_factory = sqlite3.Row
for r in con.execute("SELECT id,status,progress,destination,status_detail FROM jobs"):
    print(dict(r))
for r in con.execute(
  "SELECT id,status,downloaded_bytes,size,remote_name FROM files WHERE job_id=2"):
    print(dict(r))
PY
ls -lh /home/ubuntu/panbridge/data/tmp/2/
# 隔 30–60s 再 ls，確認 size 在增長
df -h /
```

---

## 4. 倉庫結構（本 GitHub repo）

```text
panbridge/
  app/                 # FastAPI + worker
    api/               # HTTP routes
    auth/              # 夸克/百度/pCloud/OneDrive 登入
    sources/           # 分享解析 + 取直鏈
    sinks/             # OneDrive / pCloud / local
    transfer/          # 斷點下載、磁碟檢查
    workers/runner.py  # 後台任務主循環
    stream/            # 播放串流解析
  web/                 # Jinja 模板 + CSS（繁體 UI）
  tests/               # pytest
  docs/                # 本目錄：架構 / 部署 / 交接
  Dockerfile
  docker-compose.yml
  requirements.txt
  .env.example
```

**不會提交**：`.env`、`data/`、`.venv`、`*.part`、真實 cookie。

---

## 5. 本機開發

需要 **Python 3.12 或 3.13**（3.14 可能踩依賴坑）。

```bash
git clone <本倉庫 URL>
cd panbridge
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# 夸克掃碼可選：
# playwright install chromium

cp .env.example .env
# 編輯 PANBRIDGE_SECRET、ADMIN_PASSWORD

uvicorn app.main:app --host 0.0.0.0 --port 8080
# 打開 http://127.0.0.1:8080
```

測試：

```bash
pip install pytest pytest-asyncio
pytest -q
```

---

## 6. 部署程式碼到現有 VPS（更新）

在**有 SSH 權限**的機器上：

```bash
# 本機
rsync -avz --exclude '.venv' --exclude 'data' --exclude '.env' --exclude '__pycache__' \
  ./ ubuntu@152.70.86.29:/home/ubuntu/panbridge/

ssh ubuntu@152.70.86.29 '
  cd /home/ubuntu/panbridge
  .venv/bin/pip install -r requirements.txt
  sudo systemctl restart panbridge
  curl -s http://127.0.0.1:8080/api/health
'
```

或用 `scp` 單檔覆蓋後 `systemctl restart panbridge`。

**重啟會中斷當前 HTTP 下載**，但會從 `.part` + DB `downloaded_bytes` 自動續傳（v0.3.3+）。

---

## 7. 帳號連接（設定頁）

瀏覽 `http://152.70.86.29:8080/settings`（需 ADMIN_PASSWORD）

| 來源/目標 | 方式 |
|-----------|------|
| **百度** | 設定頁掃碼，或貼完整 Cookie（需 `BDUSS`，建議含 `STOKEN`） |
| **夸克** | Playwright 掃碼或貼 Cookie |
| **pCloud** | 帳密（2FA 可填驗證碼）或貼 `auth` token（推薦有 2FA 時） |
| **OneDrive** | Azure **公用用戶端** Client ID + 裝置碼登入（無需公網回調） |

### OneDrive 注意

- 大檔（>數 GB）**務必選 OneDrive**；pCloud 免費額度通常不夠  
- 目前 UI 預填的 Client ID（若仍有效）：見 `web/templates/settings.html`  
- 若失效：到 [Azure Portal](https://portal.azure.com) 建應用  
  - 行動與桌面應用 / 公用用戶端  
  - 允許裝置碼流程  
  - 委派權限：`Files.ReadWrite`、`User.Read`、`offline_access`  

---

## 8. 已知行為與坑（接手必知）

1. **百度限速**：海外 VPS 常很慢；工具保證續傳，不保證快。  
2. **百度直鏈過期**：worker 會重新 `prepare_download` 再 Range 續傳。  
3. **下載卡死**：v0.3.3+ 有 read timeout + 120s 無進度重連 + 10 分鐘 job 看門狗。  
4. **磁碟**：~50GB 系統盤；單檔 ~25GB 下完會佔大量 tmp，上傳成功後會刪暫存。  
5. **MemoryMax=800M**：適合 free tier；勿開太多並行。  
6. **進度條**：按**檔案大小加權**（大檔主導），不是「檔案個數」。  
7. **單檔失敗**：不中止整任務；可重試 failed 檔。  
8. **百度轉存**：分享會轉到帳號下 `/PanBridge-Temp/...` 再取 dlink（網盤側可能堆積，可手動清）。  
9. **不通知**：完成需自己看 UI 或 OneDrive。  

---

## 9. 故障排查速查

| 現象 | 檢查 |
|------|------|
| UI 打不開 | `systemctl status panbridge`、OCI 安全列表放行 **TCP 8080** |
| 登入失敗 | `.env` 的 `ADMIN_PASSWORD` |
| 一直 downloading 不動 | `journalctl -u panbridge`；`.part` mtime 是否增長；等看門狗或 restart |
| 403 下載 | 百度 Cookie / UA；程式已對百度用 `LogStatistic` UA |
| OneDrive 上傳失敗 | 設定頁重新裝置碼；磁碟是否已下完整檔 |
| 憑證解密失敗 | `PANBRIDGE_SECRET` 是否被改過 |

```bash
# 看 .part 是否在長
watch -n 5 'ls -lh /home/ubuntu/panbridge/data/tmp/2/'
```

---

## 10. 新 GitHub 帳號接手 checklist

- [ ] Clone 本倉庫：`https://github.com/AI-Phrixus/panbridge`（或轉移後的新 URL）  
- [ ] 讀 [STATUS.md](./STATUS.md) + 本文件  
- [ ] 確認能 SSH 到 `ubuntu@152.70.86.29`（OCI 私鑰轉到新筆電）  
- [ ] `curl` health、看 job #2 / `.part` 是否增長  
- [ ] 登入 UI，確認設定頁帳號仍連線  
- [ ] 向操作者索取**本機私有交接文**（含口令；**不在 GitHub**）  
- [ ] （可選）Transfer / 改 remote 到新 GitHub 帳號  
- [ ] （可選）輪換 `ADMIN_PASSWORD`（改 VPS `.env` 後 restart——**會斷當前下載**）  
- [ ] **不要**把 `.env` 或 `data/app.db` 推上 GitHub  

### 把 repo 轉到新 GitHub 帳號

```bash
# 方式 A：GitHub 網頁 Settings → Transfer ownership（推薦，保留 history）

# 方式 B：新帳號空倉庫後
git remote set-url origin git@github.com:NEW_USER/panbridge.git
git push -u origin main
```

**VPS 與 GitHub 無關**：換帳號不必動 `/home/ubuntu/panbridge/data`。

### 給下一任 AI 的最小提示（公開部分）

```text
公開倉庫：https://github.com/AI-Phrixus/panbridge
先讀 docs/HANDOFF.md、docs/STATUS.md、docs/OPERATIONS.md
生產：ubuntu@152.70.86.29 · 服務 panbridge · 進行中 job2 大檔勿亂 restart
密鑰：操作者會另行提供私有交接文（不在 repo 內）
當前目標：【填寫】
```

---

## 11. 相關文件

| 文件 | 內容 |
|------|------|
| [README.md](../README.md) | 專案總覽、快速開始 |
| [STATUS.md](./STATUS.md) | **生產狀態快照（迭代）** |
| [ROADMAP.md](./ROADMAP.md) | 當前計劃與優先級 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 模組與資料流 |
| [DEPLOY.md](./DEPLOY.md) | 從零部署 Oracle / Docker |
| [OPERATIONS.md](./OPERATIONS.md) | 日常運維、備份、升級 |
| [API.md](./API.md) | HTTP API 列表 |

---

## 12. 聯絡上下文（非機密）

- 使用者語言偏好：**繁體中文** UI  
- 偏好目標：大檔 → **OneDrive 5T**；小檔可 pCloud  
- 部署區：Oracle **Osaka** free tier  
- 歷史痛點：整晚下載假死（已修 timeout）、pCloud 空間不足、百度 403（LogStatistic UA）  
- 公開 repo 擁有者（寫文時）：`AI-Phrixus` · 計畫轉移到新帳號
