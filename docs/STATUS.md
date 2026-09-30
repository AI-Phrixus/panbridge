# 生產狀態快照（可公開）

## 2026-09-30 v0.5.0 發布

- Google Drive 適配及暫停、選檔、刪除功能已實作；174 tests 通過，紅藍軍已修正並復審帳號綁定、選檔繞過、上傳完成檢查與資料夾競態。
- Oracle 已同步新版並重啟，公開 HTTPS `/api/health` 實測回報 `0.5.0`；Google 真實搬運驗收尚未完成。
- Google Cloud OAuth 品牌及專用 Web 用戶端已建立；憑證已加密保存於 Oracle，解密格式驗證通過，未放 GitHub。尚未連接使用者雲盤。
- 發布時發現根磁碟已滿；#13 約 25 GiB、#14 約 16 GiB 暫存保留。清除 75.6 MB 可再取得的 pip 安裝快取後完成同步，僅約 70 MB 可用；未删除任何用戶文件。不能在目前磁碟條件開始新的搬運。
- 回復點：`/home/ubuntu/panbridge-backups/20260930-v050`，包含舊程式壓縮包、SQLite 線上備份及權限 600 的環境檔。備份含敏感資料，不可公開。
- #13/#14 原任務保留，不直接重試失效 OneDrive；先小文件驗證後建立 Google 目標的新任務並讓使用者選檔。
- Google 影片使用 Infuse 原生 Google Drive 連接，或 Google Drive 桌面版配合 VLC；沒有聲稱私有 Drive 預覽網址可直接交給任意播放器。
- 不公開分享文件，不新增付費服務。上傳授權只限應用建立或使用者明確交給它的文件。

## 2026-09-30 增量更新

- GitHub main 已發布下載連線池修復 `f7a5ff8`；Oracle 已更新同一 downloader 檔案，SHA-256 `18160c6b07e008f6fd04e8984f1d60250f0a4746c03faf20effe049af8c3b15b`。
- 此為 v0.4.5 的下載器熱修復，健康端點版本仍為 `0.4.5`；不是 Google Drive 或新 UI 發布。
- 本機與乾淨發布快照均 150 tests 通過；包含 HTTP/1.1 真實 TCP 測試（10 次請求、1 條連線）。紅藍軍復審通過，舊 Cookie 污染已修正。尚無外部 CDN 速度倍率證據。
- 部署前任務：9 done、4 failed、1 cancelled，沒有進行中的下載。沒有自動重試舊 OneDrive 任務或刪除任何雲端文件。
- 備份：`/home/ubuntu/panbridge-backups/20260930-pool-f7a5ff8/downloader.py`；未變更資料庫、凭證或儲存目標。
- 原 OneDrive 租戶服務失效；先前完成狀態只代表當時交付成功，不保證目前仍能訪問。Google Drive 適配、#13/#14 重新搬運、暫停／選檔／刪除仍未發布。
- 詳見 [開源傳輸設計評估](OPEN_SOURCE_TRANSFER_REVIEW.md)。下列 2026-08-26 表格保留為歷史紀錄，不是最新雲盤可用性證明。

> 無密鑰。具體口令／secret 只在 VPS `.env`，不上 GitHub。
> **最後核對**：2026-08-26（Asia/Tokyo）
> GitHub `main` 與 Oracle 生產均為 **v0.4.5**；實際版本永遠以 `/api/health` 為準。

---

## 服務

| 項 | 2026-08-26 實測 |
|----|-----------------|
| `panbridge` | `active` · `NRestarts=0` |
| `cloudflared` | `active` · `NRestarts=0` |
| 公開健康檢查 | `https://panbridge.tdtc.indevs.in/api/health` → `0.4.5` |
| 本機健康檢查 | `127.0.0.1:8080/api/health` → `0.4.5` |
| 部署形態 | Oracle Cloud VPS · Ubuntu · systemd + uvicorn + Cloudflare Tunnel |
| 磁碟 | 約 50.9 GB · 已用 6.7 GB · 可用 44.2 GB |
| 正式入口 | `https://panbridge.tdtc.indevs.in` |
| 舊 HTTP 入口 | 308 跳轉正式 HTTPS（相容舊書籤） |

---

## 任務

| ID | 來源 → 目標 | 狀態 | 檔案／總量 | 說明 |
|----|-------------|------|-----------|------|
| #1 | quark → auto | `done` | 1 檔 · 155 MB | 測試任務 |
| #2 | baidu → OneDrive | `done` | 12 檔 · 26.66 GB | 大檔與其餘項目均已完成 |
| #4 | quark → OneDrive | `done` | 1452 檔 · 83.75 GB | 全部完成 |
| #5 | quark → OneDrive | `done` | 13 檔 · 42.90 GB | 全部完成 |
| #6 | quark → pCloud | `done` | 806 檔 · 1.48 GB | 全部完成 |
| **#7** | quark → OneDrive | **`done`** | 745/745 完成 · 2.39 GB | v0.4.5 安全改名並修復原 65 個失敗檔 |

Job #7 已在 v0.4.5 部署後只重試原 65 個失敗檔。程式直接復用 Oracle 上 65/65 大小精確的暫存（合計 214,436,206 bytes），沒有重新向夸克下載；最終 745/745 `done`、完整性錯誤 0。

65 個新上傳項目全部保存 Microsoft 實際名稱、Drive ID、item ID 與路徑，資料庫不一致 0、非法實際名稱 0；成功後 Oracle 暫存殘留 0。另抽查最小／中間／最大三個新項目，Graph 名稱、Drive、大小與 Range `206` 均一致。

---

## v0.4.5 已發布修復

- OneDrive 禁用字元、控制字元、保留名稱、尾端空白／句點及超長路徑會自動安全改名。
- 原始名稱仍留在 PanBridge；OneDrive 實際採用的名稱與資料夾路徑另行保存。
- 所有非空檔使用 `conflictBehavior=rename` 的上傳工作階段，大小寫或 Unicode 等價名稱不會覆蓋既有檔案。
- 大小寫／Unicode 等價但來源不同的資料夾會分流到帶短識別碼的名稱，不會錯誤合併。
- Microsoft 已確定拒絕的建立請求只嘗試一次，不再把同一完整檔案無效上傳四輪。
- 0-byte 空檔採取安全停止：不以簡易 PUT 冒險覆蓋同名非空檔。

---

## 發布驗收門檻

1. 本機 146 tests、v0.4.5 專項、編譯、diff check 與 10 萬隨機名稱檢查全部通過。
2. 紅軍與藍軍均給出 GO，無覆蓋／假完成／資料遺失 blocker。
3. GitHub `main` 已快進推送；Oracle 更新前已備份程式、`.env` 與 SQLite。
4. 公開與本機健康檢查均回報 `0.4.5`；HTTPS、HSTS、舊 HTTP 308、Secure Cookie、播放器按鈕與 `.m3u` 通過。
5. 1.1 MB 與 26.66 GB 既有影片均以 Microsoft HTTPS 直連回應 Range `206`；未經 Oracle 傳影片內容。
6. Job #7 原 65 個失敗檔均成功，最終 745/745 `done`；部署後日誌為 0 warning、0 error、0 `invalidRequest`、0 登入失效。

### 回復點

- 發布提交：`4ceddd98d3860563a02b6dc9261546e6a3dcaae1`
- 發布前備份：`/home/ubuntu/panbridge-backups/20260826T075750Z`
- 備份包含舊程式、權限設為 600 的 `.env` 副本，以及 SQLite 一致性備份。

---

## 運維注意

1. 不要為刷新 UI 無謂重啟；更新服務前先確認沒有正在下載／上傳的工作。
2. `.env`、`data/app.db`、帳號 Cookie／token 永不進 Git。
3. 完成的 OneDrive 影片播放由 Microsoft 直鏈供應；Oracle 僅負責控制面與未完成檔中繼。
4. 外部播放器應使用正式 HTTPS 播放頁或 `.m3u`；舊 HTTP 只作跳轉相容。

---

## 變更紀錄（狀態文檔）

| 日期 | 筆記 |
|------|------|
| 2026-07-24 | 初版；v0.3.x 大檔續傳與排隊優化期間快照 |
| 2026-08-20 | v0.4.4 正式 HTTPS、OneDrive 完成檔直連與播放驗收 |
| 2026-08-26 | v0.4.5 推送、備份、部署與線上播放驗收完成；Job #7 原 65 個失敗檔修復，最終 745/745 `done` |
