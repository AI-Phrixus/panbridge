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
| POST | `/api/auth/onedrive/device/start` | `{"client_id"}` |
| GET | `/api/auth/onedrive/device/{id}` | 裝置碼輪詢 |
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
| GET | `/api/tasks/{id}/files/{fid}/download` | local 目標下載 |
| GET/HEAD | `/api/tasks/{id}/files/{fid}/stream` | 串流（單一 Range）；瀏覽器 session 或 `token` |
| GET | `/api/tasks/{id}/files/{fid}/playlist.m3u` | 下載 VLC／PotPlayer／Infuse 播放清單（需登入） |
| GET/HEAD | `/api/tasks/{id}/files/{fid}/hls-asset` | 內部 HLS 子資源代理；只接受伺服器簽名的 Quark HTTPS URL |
| GET | `/api/tasks/{id}/files/{fid}/location` | 雲端 URL |
| GET | `/api/tasks/{id}/location` | 任務資料夾 URL |
| DELETE | `/api/tasks/{id}/files/{fid}/local` | 刪本機 delivered |

`destination`：`auto` \| `onedrive` \| `pcloud` \| `local`

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
