# 日常運維

## 服務

```bash
sudo systemctl status panbridge
sudo systemctl restart panbridge
sudo journalctl -u panbridge -n 100 --no-pager
sudo journalctl -u panbridge -f
```

## 健康與版本

```bash
curl -s http://127.0.0.1:8080/api/health
# {"ok":true,"version":"0.3.5"}
```

## 任務

- UI：`/` 列表、`/tasks/{id}` 詳情  
- 失敗：點「重試」（進行中不可重試，需先取消）  
- 取消：狀態 cancelled；下載會在下一次進度回呼中斷  
- 公開狀態摘要：[STATUS.md](./STATUS.md)

### CLI 查 DB

```bash
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/home/ubuntu/panbridge/data/app.db")
con.row_factory = sqlite3.Row
print([dict(r) for r in con.execute("SELECT id,status,progress,destination,status_detail FROM jobs ORDER BY id")])
PY
```

### 監看大檔是否在下載（最可靠）

```bash
# 兩次間隔 30–60 秒，size 變大 = 健康
ls -lh /home/ubuntu/panbridge/data/tmp/2/
sleep 30
ls -lh /home/ubuntu/panbridge/data/tmp/2/
```

或：

```bash
watch -n 30 'ls -lh /home/ubuntu/panbridge/data/tmp/2/ 2>/dev/null; curl -s localhost:8080/api/health'
```

**不要**只看總進度 0.x% 就判定卡死（24GB 檔在 80KB/s 時百分比半天幾乎不動是正常的）。

### 何時才重啟服務

| 情況 | 建議 |
|------|------|
| `.part` 持續增長 | **不要** restart |
| 部署新程式碼 | 可 restart（會斷流但續傳） |
| `.part` 15 分鐘完全不動 + 日誌無進展 | 可 restart 一次並驗證續傳 |
| 改 `.env` / secret | 需 restart（可能要重登網盤） |

## 升級程式

1. 拉最新 git 或 rsync  
2. `pip install -r requirements.txt`  
3. `sudo systemctl restart panbridge`  
4. 確認 health version  
5. 看進行中任務是否從 `.part` 續傳  

## 磁碟清理

```bash
df -h /
du -sh /home/ubuntu/panbridge/data/*
# 已完成且已上傳的 tmp 應自動刪；若殘留：
# 確認任務 done 後再手動刪 tmp/{job_id}
ls -la /home/ubuntu/panbridge/data/tmp/
ls -la /home/ubuntu/panbridge/data/delivered/
```

## 憑證重登

設定頁清除後重連，或：

```bash
# 危險：直接刪 DB 憑證列（需懂 SQL）
# 較安全：UI 設定頁「清除」按鈕
```

改 `PANBRIDGE_SECRET` 會讓所有舊加密憑證失效。

## 記憶體 / OOM

`MemoryMax=800M`。若被 systemd 殺：

```bash
journalctl -u panbridge | grep -i killed
# 可暫時提高 MemoryMax，或保持單任務
```

## 安全建議

- 改強 `ADMIN_PASSWORD`  
- 不要把 8080 暴露在無密碼公網過久；優先 Tunnel 或 IP 限制  
- 定期輪換網盤 Cookie（百度/夸克過期會導致 resolve/download 失敗）  
- **切勿**把 `.env`、`app.db` 提交 Git  

## 日誌噪音

掃描器可能打出 `Invalid HTTP request` / 錯 path，可忽略。真錯誤看 `panbridge.worker` / `panbridge.download`。
