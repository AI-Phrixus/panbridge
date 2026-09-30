# Google Drive 與任務控制（v0.5.0）

Google One 提供容量，PanBridge 實際透過 Google Drive API 私有交付文件。未設定帳號前不能搬運，程式測試通過不代表線上驗收完成。

## 自用連接

1. 啟用 Google Drive API，建立 OAuth 品牌及 Web 用戶端。
2. 回呼設定為正式 HTTPS 網址加 `/api/auth/google/callback`；本實例為 `https://panbridge.tdtc.indevs.in/api/auth/google/callback`。
3. 僅請求 `https://www.googleapis.com/auth/drive.file`，不請求整個雲盤讀寫權限。
4. 用戶端憑證存入應用加密資料庫 `google_oauth`；連接後的使用者憑證加密保存於 `google`。不放 GitHub、文件或日誌。
5. 外部 OAuth 應用需檢查發布狀態；Testing 下 Drive 授權可能七天失效。正式發布不代表 Google 已驗證品牌，也不會公開分享文件。
6. 先小文件核對大小、內容及雲端可訪問性，再執行大任務。

## 任務控制

- 新任務可先列文件，勾選所需文件後才開始下載；未選文件標記 skipped，不計入總進度。
- 暫停會中止活動工作並保留下載暫存及加密上傳檢查點，繼續時恢復。
- 刪除只隱藏任務記錄並停止工作；保留雲端文件、暫存及資料庫紀錄，備份亦可能保留。不是雲端永久刪除。
- 建立 Google 複本任務保留原 OneDrive 任務；使用新的文件列單，不冒用舊來源 ID。
- 任務綁定 Google 帳號的穩定識別值；改連另一帳號時安全失敗，不把文件投到另一帳號。

## 播放邊界

交付完成後播放器直接使用 Google Drive，不用 Oracle 代理影片。Infuse 可使用它自身的 Google Drive 連接；Windows/macOS VLC 可透過 Google Drive 桌面版開啟雲盤文件。Google 私有預覽 URL 不是裸媒體直鏈，不能冒充任意播放器通用播放 URL。實際播放器驗收仍需在使用者设备完成。

## 復原與安全

部署前備份程式、SQLite 及環境檔，限制備份權限。中止上傳保留完整本地文件。過期上傳會话只移除該會話檢查點，重建前查詢已交付文件以防重複。憑證及檢查點加密不代表所有暫存文件亦被應用加密。

隱私說明：`/privacy`。解除應用連接移除使用者 Google 憑證，不刪已交付文件，也不刪 OAuth 用戶端配置；Google 帳號端可另外撤銷存取權。
