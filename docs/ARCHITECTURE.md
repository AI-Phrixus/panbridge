# 架構說明

## 總覽

```
┌─────────────┐     HTTPS      ┌──────────────────────────────┐
│  瀏覽器 UI  │ ◄────────────► │  FastAPI (uvicorn :8080)     │
│  繁體模板   │                │  routes: auth/tasks/stream   │
└─────────────┘                └───────────┬──────────────────┘
                                           │
                                           │ asyncio Worker
                                           ▼
                               ┌───────────────────────┐
                               │  workers/runner.py    │
                               │  claim job → 逐檔處理 │
                               └───────────┬───────────┘
                     ┌─────────────────────┼─────────────────────┐
                     ▼                     ▼                     ▼
              sources/*              transfer/*              sinks/*
           夸克/百度取鏈          resumable_download       OD/pCloud/local
                     │                     │                     │
                     └──────────► data/tmp/{job_id}/*.part ──────┘
                                           │
                                           ▼
                                    data/app.db (SQLite)
```

## 目錄職責

| 路徑 | 職責 |
|------|------|
| `app/main.py` | FastAPI app、lifespan 啟動 worker、頁面路由 |
| `app/config.py` | pydantic-settings / 環境變量 |
| `app/db.py` | aiosqlite schema、job/file CRUD、加權進度 |
| `app/security.py` | Fernet 加密憑證、session cookie |
| `app/workers/runner.py` | 任務調度、下載/上傳、看門狗、取消 |
| `app/sources/baidu.py` | 分享驗證、轉存、filemetas dlink |
| `app/sources/quark.py` | 分享 stoken、轉存、下載 URL |
| `app/sinks/onedrive.py` | Graph 上傳（小檔 PUT / 大檔 session 分片） |
| `app/sinks/pcloud.py` | pCloud uploadfile |
| `app/sinks/local.py` | 移到 `data/delivered/` |
| `app/transfer/downloader.py` | Range 續傳、timeout、stall、刷新直鏈 |
| `app/stream/resolve.py` | 播放：本地檔或源站直鏈代理 |
| `web/templates/*` | 登入 / 首頁 / 設定 / 任務詳情 |

## 任務生命週期

```
queued → resolving → saving → downloading → uploading → done
                                              ↘ failed（可 retry）
任意 active → cancelled
```

1. `POST /api/tasks` 寫入 job（queued）  
2. Worker `claim_next_job` 認領  
3. 無 files → source.resolve（解析分享、百度會轉存）  
4. 逐檔：prepare_download → resumable_download → sink.upload_file  
5. 全部 done → job done；部分 failed → job failed 但其他檔可能已完成  

## 斷點續傳

- 下載寫入 `data/tmp/{job_id}/{file_id}_{name}.part`  
- HTTP `Range: bytes={existing}-`  
- 進度寫入 `files.downloaded_bytes` + job `status_detail`  
- 進程重啟：狀態仍為 downloading → worker 再次認領 → 從 part 長度繼續  
- 直鏈 401/403：`url_refresh_cb` 重新取鏈  

## 進度計算

`recompute_job_progress`：**按檔案 size 加權**  
每檔：下載 70% + 上傳 30%；`done` = 100%。

## 安全

- 管理口令：`ADMIN_PASSWORD`（session cookie itsdangerous）  
- Cookie/token：`encrypt_json` 存 DB（key 派生自 `PANBRIDGE_SECRET`）  
- 串流/下載 API 需登入  

## 並發與資源

- 預設 `MAX_CONCURRENT_JOBS=1`  
- 百度下載強制單連線（防 403）  
- systemd `MemoryMax=800M`（Oracle free）  
- 磁碟預檢 `ensure_space`（大檔自適應預留）
