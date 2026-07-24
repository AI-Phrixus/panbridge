# PanBridge

**雲端常駐中繼**：把 [夸克網盤](https://pan.quark.cn) / [百度網盤](https://pan.baidu.com) 分享連結，自動搬到 **OneDrive**、**pCloud** 或 **伺服器暫存**。

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

| | |
|--|--|
| **版本** | v0.3.6 |
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
- 直鏈過期自動重新取鏈  
- 目標：**OneDrive**（大檔推薦）/ **pCloud** / **伺服器暫存**  
- 進度按**檔案大小加權**；詳情顯示 MB/GB 與速度  
- 網頁播放 / 外部播放器串流（VLC 等）  
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
