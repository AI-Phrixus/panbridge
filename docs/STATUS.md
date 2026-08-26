# 生產狀態快照（可公開）

> 無密鑰。具體口令／secret 只在 VPS `.env`，不上 GitHub。
> **最後核對**：2026-08-26（Asia/Tokyo）
> 生產仍是 **v0.4.4**；倉庫工作樹的 **v0.4.5 尚未部署**。實際版本永遠以 `/api/health` 為準。

---

## 服務

| 項 | 2026-08-26 實測 |
|----|-----------------|
| `panbridge` | `active` · `NRestarts=0` |
| `cloudflared` | `active` · `NRestarts=0` |
| 公開健康檢查 | `https://panbridge.tdtc.indevs.in/api/health` → `0.4.4` |
| 本機健康檢查 | `127.0.0.1:8080/api/health` → `0.4.4` |
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
| **#7** | quark → OneDrive | **`failed`** | 680/745 完成 · 97.31% | 65 個來源名稱含 OneDrive 禁用字元；等 v0.4.5 部署後重試 |

Job #7 的 65 個未完成名稱已用 v0.4.5 規則做只讀驗證：65 個都會安全改名，規則處理後非法字元 0、OneDrive 等價名稱碰撞 0、超長路徑 0。部署前不要反覆重試；v0.4.4 只會再次收到 Microsoft 400。

65 個失敗檔的本機暫存全部仍在 Oracle，65/65 大小與資料庫完全一致（合計 214,436,206 bytes）。因此修復重試可直接上傳 OneDrive，不需要重新從夸克下載，也不依賴當時的夸克直鏈仍有效。

---

## v0.4.5 待發布修復

- OneDrive 禁用字元、控制字元、保留名稱、尾端空白／句點及超長路徑會自動安全改名。
- 原始名稱仍留在 PanBridge；OneDrive 實際採用的名稱與資料夾路徑另行保存。
- 所有非空檔使用 `conflictBehavior=rename` 的上傳工作階段，大小寫或 Unicode 等價名稱不會覆蓋既有檔案。
- 大小寫／Unicode 等價但來源不同的資料夾會分流到帶短識別碼的名稱，不會錯誤合併。
- Microsoft 已確定拒絕的建立請求只嘗試一次，不再把同一完整檔案無效上傳四輪。
- 0-byte 空檔採取安全停止：不以簡易 PUT 冒險覆蓋同名非空檔。

---

## 發布驗收門檻

1. 本機完整測試、編譯與 diff 檢查全部通過。
2. 紅軍確認無覆蓋／假完成／資料遺失 blocker，藍軍給出 GO。（2026-08-26 已完成：RED GO / BLUE GO）
3. 獲得操作者推送與部署許可後，先備份程式與 SQLite，再更新服務。
4. 公開與本機健康檢查都回報 `0.4.5`，HTTPS、Range、播放頁 smoke 不退化。
5. 僅重試 Job #7 的 65 個失敗檔，抽查 Microsoft 回傳名稱、遠端大小、item ID 與 PanBridge 記錄一致。
6. Job #7 最終必須是 745/745 `done`；若仍有失敗，保留暫存與錯誤證據，不宣稱完成。

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
| 2026-08-26 | Jobs #2/#4/#5/#6 均已完成；定位 Job #7 的 65 個 OneDrive 非法名稱並完成 v0.4.5 本機修復 |
