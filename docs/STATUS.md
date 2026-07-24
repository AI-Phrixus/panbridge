# 生產狀態快照（可公開）

> 無密鑰。具體口令/secret 只在操作者本機私有目錄，不上 GitHub。  
> **最後更新**：2026-07-24（UTC 約 08:15）  
> **程式版本**：v0.3.7（以 `/api/health` 為準）

---

## 服務

| 項 | 狀態 |
|----|------|
| 進程 | `systemd` 服務 `panbridge` · **active** |
| 健康 | `GET /api/health` → `{"ok":true,"version":"0.3.7"}` |
| 部署形態 | Oracle Cloud VPS · Ubuntu · venv + uvicorn :8080 |
| 磁碟（快照） | 系統盤約 48G · 使用約 9% · 大檔下載中會逐漸上升 |
| 記憶體限制 | systemd `MemoryMax=800M` |
| 並發 | `MAX_CONCURRENT_JOBS=1` |

---

## 任務

| ID | 狀態 | 目標 | 說明 |
|----|------|------|------|
| **#1** | `done` | auto | 夸克測試任務，已完成 |
| **#2** | `downloading` | **onedrive** | 百度分享「揭秘日 / Disclosure Day」合集 |

### Job #2 檔案（摘要）

| 階段 | 內容 |
|------|------|
| 已完成 | backdrop / banner / clearart / disc 等小圖（已上傳 OneDrive） |
| **進行中** | `Disclosure Day … 2160p … .mkv` · **約 24.83 GB** |
| 快照進度 | 下載約 **~2 GB / 24.83 GB（~5–8%）** · 速度常見 **~70–100 KB/s**（百度限速波動） |
| 排隊 | nfo / srt / 其餘海報圖 |

### 粗估剩餘時間（僅供參考）

以 **~80 KB/s** 計：剩餘 ≈ 24.5 GB → 量級為 **數天**（不是數小時）。  
速度若升到 1 MB/s 量級會明顯縮短。以 UI `status_detail` 與伺服器 `.part` 增長為準。

**生命線檔案**：`data/tmp/2/6_*.mkv.part`  
**請勿**在任務進行中刪除 `.part`。

---

## 已連接帳號類型（無密文）

伺服器 DB 中曾配置過（名稱級別）：

- baidu  
- quark  
- pcloud  
- onedrive / onedrive_app  

失效時在 **設定頁** 重登，不要改 `PANBRIDGE_SECRET` 除非你準備重綁全部憑證。

---

## 運維注意（當前階段）

1. **不要無故 `systemctl restart panbridge`**（會斷當前 HTTP；雖可續傳但浪費時間）。  
2. 判定是否卡死：看 `.part` 是否在長（隔 1–2 分鐘），不要只看總進度 0.x%。  
3. 超過 **15 分鐘** `.part` 完全不動再介入（日誌 / 可選一次重啟）。  
4. 大檔下完後會 **上傳 OneDrive（分片）**，再處理 queued 小檔。  
5. 上傳成功後 tmp 暫存會清掉，磁碟佔用回落。

---

## 公開文件 vs 私有文件

| 位置 | 內容 |
|------|------|
| 本倉庫 `docs/*` | 架構、部署、API、本狀態快照（無 secret） |
| 操作者本機 `panbridge-private-handoff/` | 含 IP 口令 secret 的 AI 交接文（**勿提交**） |

接手新 AI 時：公開 clone 本倉庫 + 由操作者私下提供私有交接文。

---

## 變更紀錄（狀態文檔）

| 日期 | 筆記 |
|------|------|
| 2026-07-24 | 初版；job2 下載中；v0.3.5 → **v0.3.6 紅藍軍修復**（上傳完整性/續傳/取消/限速等） |
| 2026-07-24 | job2 ~2GB+ 下載中；部署 v0.3.6 後從 .part 續傳 |
| 2026-07-24 | **v0.3.7** 多連線 Range 加速；見 DOWNLOAD_SOURCES.md |
