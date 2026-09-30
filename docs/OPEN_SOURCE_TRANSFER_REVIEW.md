# 開源傳輸設計評估（2026-09-30）

## 原則與參考

這次採用公開設計原則、自行實作，沒有複製第三方程式碼，也沒有新增外部執行程式依賴。

- [OpenList-Worker](https://github.com/OpenListTeam/OpenList-Worker)：多雲儲存驅動、直連／代理分工；不是把所有流量搬到 Cloudflare 就會加速的下載器。AGPL-3.0，任何未來程式碼重用須另行檢查授權與義務。
- [aria2 官方手冊](https://aria2.github.io/manual/en/html/aria2c.html)：區分多文件並發與單文件分段連線；限制每服務器連線數，保留續傳／完整性檢查。
- [rclone 官方文件](https://rclone.org/docs/)：按後端能力啟用多執行緒、控制連線／緩衝資源；不能把並發直接當作速度，也不能用減少目的端檢查換取重複交付。
- [HTTPX client 文件](https://www.python-httpx.org/advanced/clients/)：client 連線池可重用 TCP，降低重複握手开銷。

## 本地實作的第一批優化

`app/transfer/downloader.py` 的每文件連線池涵蓋分段和重試，socket 上限不超出現有文件並發預算。每次請求嘗試有獨立 CookieJar，避免舊 CDN Cookie 污染刷新後的登入資訊；同一次轉址链仍能接收必要 Cookie。透過 HTTPX 公開 send API 保留環境代理路由，不依賴私有 transport 欄位。

取消／出錯時先取消並等待所有分段工作，再關閉池。保留既有 Range 校驗、稀疏檔案已驗證進度、斷點資料與 fsync；未提高預設並發，也沒有降低 TLS 驗證。

## 證據與限制

- 回歸測試包含：內容逐位元組一致、分段共用 owner、取消清理、獨立登入 Cookie、同次轉址 Cookie seed。
- 新增 localhost HTTP/1.1 實際 socket 測試：10 次獨立 CookieJar 請求必須僅建立 1 條 TCP 連線。在禁止 socket.bind 的沙盒中此項明確 skip；外部 CDN 吞吐仍需獨立測量。
- 需在正式環境做同檔案／同來源的改動前後測速，记录時間、位元組、重試、限流與內容完整性；未完成前不宣稱速度倍率。
- Google Drive 適配、暫停／選檔／刪除 UI 尚在開發；本文件不代表這些功能完成或已部署。
- 已交付影片應由播放器直接讀雲端；不把 Oracle 變成影片內容代理。暫不部署額外 OpenList／aria2／rclone 常駐服務。
