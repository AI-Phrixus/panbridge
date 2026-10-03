# HTTP API 摘要

除特別註明外，需登入 Cookie：`panbridge_session`（`POST /api/auth/login` 後設定）。

## 認證

| Method | Path | 說明 |
|--------|------|------|
| POST | `/api/auth/login` | `{"password"}` |
| POST | `/api/auth/logout` | 清 session |
| GET | `/api/auth/me` | 已綁定 provider 列表 |
| POST | `/api/auth/baidu/qr/start` | 百度掃碼開始 |
| GET | `/api/auth/baidu/qr/{id}` | 掃碼狀態 |
| POST | `/api/auth/baidu/cookie` | 貼 Cookie |
| POST | `/api/auth/quark/qr/start` | 夸克掃碼（純 API 登錄 QR，非官網截圖） |
| GET | `/api/auth/quark/qr/{id}` | 狀態；confirmed 後自動存 Cookie |
| POST | `/api/auth/quark/cookie` | 貼 Cookie |
| POST | `/api/auth/pcloud/login` | email/password/code? |
| POST | `/api/auth/pcloud/token` | 貼 auth token |
| GET | `/api/auth/google/status` | 是否已設定／已連接（不返回憑證） |
| POST | `/api/auth/google/config` | 保存網頁 OAuth 用戶端 |
| POST | `/api/auth/google/start` | 取得 Google 官方授權網址 |
| GET | `/api/auth/google/callback` | OAuth 回呼（綁定原登入） |
| DELETE | `/api/auth/{provider}` | 刪憑證 |

## 任務

| Method | Path | 說明 |
|--------|------|------|
| GET | `/api/tasks/system/status` | 磁碟、版本、空間、providers |
| GET | `/api/tasks` | 任務列表 |
| POST | `/api/tasks` | 建立：`{text, passcode?, pcloud_path?, destination?}` |
| GET | `/api/tasks/{id}` | 任務 + 檔案列表 |
| POST | `/api/tasks/{id}/retry` | 重試（非 active） |
| POST | `/api/tasks/{id}/cancel` | 取消 |
| POST | `/api/tasks/{id}/pause` | 保存檢查點並暫停 |
| POST | `/api/tasks/{id}/resume` | 接續原任務，可能重排 failed；不要用作無限重試 |
| POST | `/api/tasks/{id}/selection` | `{file_ids: [...]}` 提交選檔 |
| DELETE | `/api/tasks/{id}` | 軟刪除記錄，不刪已交付雲端文件 |
| GET | `/api/tasks/{id}/files/{fid}/download` | local 目標下載 |
| GET/HEAD | `/api/tasks/{id}/files/{fid}/stream` | 串流（單一 Range）；瀏覽器 session 或 `token` |
| GET | `/api/tasks/{id}/files/{fid}/playlist.m3u` | 下載 VLC／PotPlayer／Infuse 播放清單（需登入） |
| GET/HEAD | `/api/tasks/{id}/files/{fid}/hls-asset` | 內部 HLS 子資源代理；只接受伺服器簽名的 Quark HTTPS URL |
| GET | `/api/tasks/{id}/files/{fid}/location` | 雲端 URL |
| GET | `/api/tasks/{id}/location` | 任務資料夾 URL |
| DELETE | `/api/tasks/{id}/files/{fid}/local` | 刪本機 delivered |

`destination`：`auto` \| `google` \| `pcloud` \| `local`。本部署 OneDrive 已停用，不建立或重試舊目標任務。

顯示序號不是 API ID。目前 #1／#2 對應內部 16／18，所有 API 使用內部 ID。

## 頁面

| Path | 說明 |
|------|------|
| `/` | 任務列表 |
| `/login` | 登入 |
| `/settings` | 帳號設定 |
| `/tasks/{id}` | 任務詳情 |
| `/play/{job}/{file}` | 播放頁 |
| `/browse/local/{job}` | 本機暫存瀏覽 |
| `/api/health` · `/health` | 健康（無需登入） |

播放頁會產生綁定單一 job/file 的限時 `token`，供 VLC／Infuse／IINA／PotPlayer 等不會攜帶瀏覽器 Cookie 的播放器使用。`transcode=1` 會對夸克影片優先嘗試線上轉碼；HLS 代理只允許 HTTPS `*.quark.cn` 並逐跳檢查重新導向。

上述播放與 `.m3u` 能力屬非 Google 目標。Google 私人文件不提供通用媒體直鏈，`/location` 是需要 Google 登入的網頁位置，不等同 Range 媒體 URL。Infuse 可直接連接自己的 Drive，VLC 可開啟 Drive 電腦版文件；不擴大 OAuth、不公開分享、不以 Oracle 代理已交付 Google 影片。實機小／大檔與拖曳尚待驗收。
