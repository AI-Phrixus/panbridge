# PanBridge

**雲端常駐中繼**：把 [夸克網盤](https://pan.quark.cn) / [百度網盤](https://pan.baidu.com) 分享連結，自動搬到 **OneDrive**、**pCloud** 或 **伺服器暫存**。

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

| | |
|--|--|
| **版本** | v0.4.1 |
| **UI 語言** | 繁體中文（為主） |
| **部署形態** | VPS systemd / Docker |
| **核心能力** | 貼連結即跑 · 斷點續傳 · 大檔 OneDrive · 串流播放 |
| **倉庫** | https://github.com/AI-Phrixus/panbridge |

> **新人接手請先讀**  
> 1. [docs/HANDOFF.md](docs/HANDOFF.md) — 生產環境與交接 checklist  
> 2. [docs/STATUS.md](docs/STATUS.md) — 當前任務/服務快照  
> 3. [docs/ROADMAP.md](docs/ROADMAP.md) — 優先級計劃  
>  
> 管理口令與加密密鑰**不在本倉庫**（只在 VPS `.env` / 操作者私有交接文）。

---

## 功能

- 貼夸克 / 百度分享連結（可批量）→ 後台排隊執行  
- **斷點續傳**（`.part` + HTTP Range；服務重啟可恢復）  
- 直鏈過期自動重新取鏈；夸克輪換 Cookie 自動加密保存
- 目標：**OneDrive**（大檔推薦）/ **pCloud** / **伺服器暫存**  
- 進度按**檔案大小加權**；詳情顯示 MB/GB 與速度  
- 網頁 HLS／轉碼播放；外部播放器限時簽名串流（VLC、Infuse、IINA、PotPlayer）
- 一鍵打開雲端資料夾  
- 帳號：掃碼或 Cookie / pCloud token / OneDrive 裝置碼  
- v1 **不推送通知**（完成請看 UI 或雲盤）

---

## 架構（一句話）

```text
Browser → FastAPI + Worker (VPS) → 下載源盤 → 上傳目標雲
                 ↓
           SQLite + data/tmp/*.part
```

詳見 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 快速開始（本機）

需要 **Python 3.12 或 3.13**。

```bash
git clone <本倉庫 URL> panbridge
cd panbridge
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 編輯 PANBRIDGE_SECRET、ADMIN_PASSWORD

uvicorn app.main:app --host 0.0.0.0 --port 8080
```

v0.4.0 起若仍使用範例 secret、`admin` 或過短密碼，服務會拒絕啟動並顯示要補的設定，避免意外暴露網盤憑證。

開啟 <http://127.0.0.1:8080> ，用 `ADMIN_PASSWORD` 登入 → **設定**連接帳號 → 首頁貼連結。

### 測試

```bash
pip install pytest pytest-asyncio
pytest -q
```

---

## 生產部署與交接

| 文件 | 內容 |
|------|------|
| **[docs/HANDOFF.md](docs/HANDOFF.md)** | **交接必讀**：Oracle 實例、SSH、任務、密鑰**位置** |
| **[docs/STATUS.md](docs/STATUS.md)** | **生產狀態快照**（job 進度、注意事項） |
| [docs/ROADMAP.md](docs/ROADMAP.md) | P0/P1/P2 計劃 |
| [docs/DEPLOY.md](docs/DEPLOY.md) | 從零 Ubuntu / Docker / 備份 |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | 重啟、升級、清盤、監看下載 |
| [docs/API.md](docs/API.md) | HTTP API |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模組與資料流 |

### Docker（可選）

```bash
cp .env.example .env   # 修改密鑰
docker compose up -d --build
```

### 現有 systemd 實例更新

```bash
rsync -avz --exclude '.venv' --exclude 'data' --exclude '.env' \
  ./ ubuntu@YOUR_HOST:/home/ubuntu/panbridge/
ssh ubuntu@YOUR_HOST 'sudo systemctl restart panbridge && curl -s localhost:8080/api/health'
```

---

## 使用流程

1. **設定** → 連接 OneDrive（大檔）與/或 pCloud；連接夸克、百度  
2. 首頁貼分享連結，選目標（大檔選 **onedrive**）  
3. **開始後台搬運** → 任務列表看進度  
4. 完成後到 OneDrive / pCloud 自取；或本機暫存頁下載  

### 斷點續傳

| 階段 | 行為 |
|------|------|
| 下載 | `data/tmp/{job}/*.part` + `Range` |
| 直鏈失效 | 自動重新取鏈後續傳 |
| 上傳 | OneDrive 分片；失敗可整檔重試 session |
| 重啟服務 | Worker 重新認領 `downloading` 任務 |

---

## 環境變量

見 [`.env.example`](.env.example)。**永遠不要提交 `.env` 或 `data/`。**

| 變量 | 說明 |
|------|------|
| `PANBRIDGE_SECRET` | 加密憑證用（勿隨意更換） |
| `ADMIN_PASSWORD` | 網頁口令 |
| `DATA_DIR` | 資料目錄（DB + tmp） |
| `MAX_CONCURRENT_JOBS` | 小 VPS 建議 `1` |
| `PUBLIC_BASE_URL` | 可選；反向代理的公開 HTTPS 網址，用於播放器連結 |
| `STREAM_TOKEN_MAX_AGE` | 外部播放器限時連結有效秒數（預設 7 天） |
| `PCLOUD_*` | pCloud API 主機與預設路徑 |

---

## 限制與誠實說明

- 夸克 / 百度為**非官方 Web API**，可能變更  
- 百度**非 SVIP / 海外**常限速；本工具保證能續傳，不保證快  
- pCloud 免費空間小，**大檔請用 OneDrive**  
- 僅供個人合法備份自用  

---

## 目錄結構

```text
app/           後端
web/           前端模板
tests/         單元測試
docs/          完整文件（交接 / 部署 / 運維）
Dockerfile     容器
```

---

## Changelog（摘要）

### v0.4.0
- 夸克 `__puus` / `__pus` 輪換 Cookie 自動合併並持久化；續傳同步刷新直鏈與請求頭
- 多段下載 metadata v2：原子保存區段邊界、跨連線數恢復、登入失效不再清空大檔進度
- 修復 Range 探測可能把整個大檔讀入記憶體、稀疏檔假進度與半份解析清單漏檔
- 播放連結改為檔案級限時簽名；VLC/IINA 無需瀏覽器 Cookie；支援 HEAD、suffix Range
- 夸克影片優先嘗試線上轉碼；HLS 清單／分片安全代理；網頁播放器元件隨應用自帶，不依賴外部 CDN
- 未完成／遺失／稀疏 `.part` 不再冒充完整影片；區段與續傳紀錄按完成順序同步落盤
- 修復播放頁變數衝突造成的直接崩潰；新增 Windows VLC/PotPlayer 與 Infuse 可用的 `.m3u` 下載
- Quark／OneDrive 登入採世代隔離與鎖定續期，舊工作不會覆蓋新登入；預設弱密碼／secret 拒絕啟動
- pCloud 改用官方 multipart 串流上傳並核對遠端大小
- 對抗與回歸測試擴充至 v0.4.0

### v0.3.13
- SQL 聚合進度（1452 檔列表不再拖慢 UI）  
- Worker 一次填滿並發槽；排隊文案區分「已滿 / 等待空位 / 即將開始」  
- 排隊心跳 `touch=False` 不打亂續傳優先級  
- 取消：不覆蓋用戶「已取消」；中斷中的檔案回 `queued` 並保留已下字節  
- 對抗測試 r5  

### v0.3.11–0.3.12
- 體積進度假 0%：檔案數地板 + 列表實時重算  
- 中斷任務優先於新任務 claim；排隊顯示「已完成 N/M 檔」  

### v0.3.9–0.3.10
- **夸克掃碼 = 純 CAS API**（`getTokenForQrcodeLogin` + `su.quark.cn` QR）  
- 不再截圖 pan.quark.cn（避免「下載客戶端/升級」假二維碼）  
- 掃碼後自動換 Cookie（含 `__puus`）；Playwright 非必須  

### v0.3.7–0.3.8
- 下載加速：單檔多連線 Range **就地寫入**（不佔雙倍磁碟）  
- 百度默認嘗試 4 連線（403 回退單線）；夸克/通用 6 連線  
- 夸克 CDN 412/403 `require login` 整 job 硬失敗  

### v0.3.6
- 三輪紅藍軍對抗修復：OneDrive 假成功上傳、pCloud 分片校驗、resolve 中斷丟檔、  
  空間檢查、size 污染進度、claim 飢餓、取消上傳、token 持久化、百度分頁、  
  路徑穿越、登入限速、SQL 欄位白名單等  
- 對抗測試：`tests/test_adversarial_r1/r2/r3.py`

### v0.3.5
- Worker 優雅關閉、卡死看門狗、OneDrive 上傳完整性  
- `/health` 別名、token 刷新回寫、磁碟預留自適應  

### v0.3.3–0.3.4
- 下載 timeout / stall 重連（修復整晚假死）  
- 大小加權進度、繁體狀態、逐檔失敗隔離  

### v0.3.x
- OneDrive 裝置碼、串流播放、雲端位置、macOS 風格 UI  

### v0.2 / v0.1
- 多連線下載、百度/夸克 → pCloud、Docker  

---

## License

MIT — 見 [LICENSE](LICENSE)。無擔保。
