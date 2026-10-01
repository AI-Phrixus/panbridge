# PanBridge 交接手冊（新帳號接手必讀）

> 倉庫版本：**v0.5.4 · UI 1**（後端以 `/api/health` 為準）
>
> 最後更新：2026-10-01
> 目的：讓**全新 GitHub / 開發環境**在不依賴舊對話上下文的情況下，能接手運維與開發。

---

### 最新增量與待辦

2026-10-01 生產後端 v0.5.4；Google Drive、暫停／選檔／刪除已部署。UI 1 僅更新 web 檔案、不重啟搬運服務，詳見 [STATUS.md](STATUS.md)、[UI 驗收與復原](UI1_REVIEW.md)。先前連線池與開源設計評估見 [OPEN_SOURCE_TRANSFER_REVIEW.md](OPEN_SOURCE_TRANSFER_REVIEW.md)。

OneDrive 已停用，原 #13/#14 只保留記錄，不按重試。已獲授權的 Google OAuth（特定文件）連接並加密保存在 Oracle；新任務 #16（935 件）／#18（405 件）持續搬運，#17 保持暫停。150 GB 磁碟已擴容；A1 升級已依要求停止。Mac 備份 477 文件不能僅依任務 done 刪除，須依背景排程的 Google 全量 size／SHA-256 校驗門檻。

目前公開入口尚待恢復：舊域受安全分類影響，候選 workers.dev 正式端 500／1101；不是下載服務離線。沒有更改 Google 回呼或公開來源。不要新增安全例外、切換入口或以預覽成功宣稱正式可用；後續操作須按當次授權。下方早期架構／版本紀錄有歷史資訊，以本節及 STATUS 的最新驗證為準。

2026-10-01 13:47 日本時間已部署入口容錯 `c1704060`（編輯器讀回與本地逐字一致）；正式 Mac／Oracle 仍 1101，預覽正常 302。正式請求未出現在一次即時日誌中，原因未確定；一次新網址停用／再啟用已提出當次確認但未執行。服務不重啟，下載未重排；13:50 #16／#18 為 93／290 done，各一份 ConnectTimeout，Mac 備份保留。

## 1. 這是什麼

**PanBridge** = 自建「網盤中繼」：

```
夸克 / 百度 分享連結  →  VPS 選檔、下载（斷點續傳）  →  Google Drive / pCloud / 本機暫存
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
| 實例 | Oracle Free E2.1.Micro / **x86_64** / 150 GB 開機磁碟；A1 升級暫停 |
| SSH | `ssh ubuntu@152.70.86.29`（用你 OCI 的私鑰） |
| 程式目錄 | `/home/ubuntu/panbridge` |
| 資料目錄 | `/home/ubuntu/panbridge/data`（DB + 暫存，**勿當 git 倉庫**） |
| 虛擬環境 | `/home/ubuntu/panbridge/.venv` |
| 服務 | `systemd` unit：`panbridge` |
| HTTPS | `cloudflared` named tunnel：`panbridge-oracle-osaka` |
| 埠 | `8080` |
| 健康檢查 | `https://panbridge.tdtc.indevs.in/api/health` 或 `/health` |
| UI | `https://panbridge.tdtc.indevs.in`（Cloudflare Tunnel） |

### 服務指令

```bash
ssh ubuntu@152.70.86.29

sudo systemctl status panbridge
sudo systemctl restart panbridge   # 會中斷當前下載，但會從 .part 續傳
sudo journalctl -u panbridge -f
sudo systemctl status cloudflared

# 版本
curl -s http://127.0.0.1:8080/api/health
curl -s https://panbridge.tdtc.indevs.in/api/health
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
| #1/#2/#4/#5 | `done` | OneDrive 目標均已完成；#4 共 1452 檔 |
| #6 | `done` | pCloud 806 檔完成 |
| **#7** | **`done` 100%** | 745/745 完成；v0.4.5 已修復原 65 個 OneDrive 非法來源名稱 |

生產與 GitHub `main` 於 2026-08-26 均已更新為 v0.4.5。發布前先建立 `/home/ubuntu/panbridge-backups/20260826T075750Z` 回復點，之後公開／本機 health、HTTPS、Range、播放器 smoke 與 Job #7 修復重試全部通過。

Job #7 的 65 個失敗檔直接復用原暫存上傳，沒有重下夸克來源。65 個 Microsoft 實際名稱／Drive／item ID／大小與資料庫均一致，成功後暫存已清理；最終 745/745 `done`。

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/home/ubuntu/panbridge/data/app.db")
con.row_factory = sqlite3.Row
for r in con.execute("SELECT id,status,progress,destination,status_detail FROM jobs"):
    print(dict(r))
for r in con.execute(
  "SELECT id,status,downloaded_bytes,uploaded_bytes,size,remote_name "
  "FROM files WHERE job_id=7"):
    print(dict(r))
PY
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
# 本機；現有目錄由 root 擁有，因此讓遠端 rsync 使用 sudo，並保留原權限／owner。
rsync -rz --no-perms --no-owner --no-group --omit-dir-times \
  --rsync-path='sudo rsync' \
  --exclude '.venv' --exclude 'data' --exclude '.env' \
  --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache' \
  ./ ubuntu@152.70.86.29:/home/ubuntu/panbridge/

ssh ubuntu@152.70.86.29 '
  cd /home/ubuntu/panbridge
  .venv/bin/pip install -r requirements.txt
  PYTHONPYCACHEPREFIX=/tmp/panbridge-release-check \
    .venv/bin/python -m compileall -q app
  sudo systemctl restart panbridge
  curl -fsS http://127.0.0.1:8080/api/health
'
```

更新前必須先用 SQLite `backup()` 建一致性 DB 備份並複製舊程式／`.env`。一般 `rsync -a` 會因現有目錄為 root 擁有而出現 `Permission denied`；如果同步在重啟前失敗，服務仍跑舊進程，先查 health 與磁碟版本，不要在混合狀態重試任務。

**重啟會中斷當前 HTTP 下載**，但會從 `.part` + DB `downloaded_bytes` 自動續傳（v0.3.3+）。

---

## 7. 帳號連接（設定頁）

瀏覽 `https://panbridge.tdtc.indevs.in/settings`（需 ADMIN_PASSWORD）

| 來源/目標 | 方式 |
|-----------|------|
| **百度** | 設定頁掃碼，或貼完整 Cookie（需 `BDUSS`，建議含 `STOKEN`） |
| **夸克** | 純 CAS API 掃碼或貼 Cookie；v0.4.0 自動保存輪換 Cookie |
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
3. **下載卡死**：v0.3.3+ 有 read timeout + 120s 無進度重連 + 10 分鐘 job 看門狗；v0.4.0 會同步刷新 URL 與 Cookie。
4. **磁碟**：~50GB 系統盤；單檔 ~25GB 下完會佔大量 tmp，上傳成功後會刪暫存。  
5. **MemoryMax=800M**：適合 free tier；勿開太多並行。  
6. **進度條**：按**檔案大小加權**（大檔主導），不是「檔案個數」。  
7. **單檔失敗**：不中止整任務；可重試 failed 檔。  
8. **百度轉存**：分享會轉到帳號下 `/PanBridge-Temp/...` 再取 dlink（網盤側可能堆積，可手動清）。  
9. **不通知**：完成需自己看 UI 或 OneDrive。  
10. **OneDrive 安全名稱**：v0.4.5 會把禁用字元改成全形並保存實際 Microsoft 名稱；非空檔使用 `rename` 防覆蓋。0-byte 檔在沒有原子防覆蓋保障時會安全失敗。

---

## 9. 故障排查速查

| 現象 | 檢查 |
|------|------|
| UI 打不開 | `systemctl status panbridge`、OCI 安全列表放行 **TCP 8080** |
| 登入失敗 | `.env` 的 `ADMIN_PASSWORD` |
| 一直 downloading 不動 | `journalctl -u panbridge`；看 UI `downloaded_bytes` / `du`，不要相信稀疏 `.part` 的 `ls` 表面大小 |
| 403 下載 | 百度 Cookie / UA；程式已對百度用 `LogStatistic` UA |
| OneDrive 上傳失敗 | 設定頁重新裝置碼；磁碟是否已下完整檔 |
| 憑證解密失敗 | `PANBRIDGE_SECRET` 是否被改過 |
| 啟動即報「安全設定未完成」 | `.env` 仍是範例 secret／弱密碼；填入隨機 secret 與至少 10 字元管理密碼 |
| Windows／Infuse 播放 | 播放頁複製 7 天串流網址，或下載 `.m3u`；反向代理請設定 `PUBLIC_BASE_URL` |

```bash
# 看 .part 是否在長
watch -n 5 'ls -lh /home/ubuntu/panbridge/data/tmp/2/'
```

---

## 10. 新 GitHub 帳號接手 checklist

- [ ] Clone 本倉庫：`https://github.com/AI-Phrixus/panbridge`（或轉移後的新 URL）  
- [ ] 讀 [STATUS.md](./STATUS.md) + 本文件  
- [ ] 確認能 SSH 到 `ubuntu@152.70.86.29`（OCI 私鑰轉到新筆電）  
- [ ] `curl` health 應回報 v0.4.5；核對 Job #7 為 745/745 `done`
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
生產：ubuntu@152.70.86.29 · 服務 panbridge/cloudflared · 正式入口 https://panbridge.tdtc.indevs.in
當前：生產 v0.4.5；Job #7 745/745 done；回復點 /home/ubuntu/panbridge-backups/20260826T075750Z
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
