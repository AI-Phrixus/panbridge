# UI 1：範圍、自檢與部署證據

日期 2026-10-01；後端版本 0.5.4 不變。只改 web 模板／樣式／共用 JavaScript，不修改搬運器、帳號權限或域名入口。

## 修復與可驗收行為

- 搜尋名稱／資料夾、按狀態篩選，每頁 100 文件；選取「全部搜尋結果」包含所有頁面。已選文件在換頁、刷新後保留，顯示数量及容量。
- 批量選檔一次遍歷後更新摘要，避免逐文件重算；DOM 只建立當頁最多 100 行，不再每次重建全部 935 行。這是演算法／頁面負擔改進，沒有實際網速基準測試。
- 刷新不重疊、錯誤退避上限 30 秒、背景隱藏停止刷新，回到頁面立即更新；保留上次可用資料。手動刷新與操作後刷新合併。
- 重複操作鎖、過期 GET 回應隔離、操作逾時不自動重播。非 JSON／缺失任務 ID 不宣稱建立成功，輸入不因異常回應清空。
- 刪除／取消頁面內確認，Escape／返回取消；非破壞性操作保持直接執行。刪除僅移除任務，不刪交付雲端文件。
- 雲端位置使用明確的 HTTPS／同來源連結，非同步取得後不自動開視窗。提示與結果捲動至可見位置，不輸出憑證、不公開分享。
- 中文狀態、0–100% 進度、安全 HTML 顯示、正常勾選框、鍵盤焦點、減少動態效果、窄螢幕表格獨立橫捲。

## 紅方挑錯／藍方驗證（同一代理分輪自檢）

紅方發現並修復：

1. 使用者手動勾選後，「取消全部」可能因快取跳過重繪、留下已勾的舊外觀：操作後使快取失效。
2. Node 測試正常但瀏覽器原生計時器接收者不正確，刷新停止：保持 Window 接收者，新增專項測試，再以內建瀏覽器重新驗證。
3. HTTP 200 非 JSON 被當空物件成功：改為未確認結果，禁止自動重播；新任務 ID 缺失亦不跳轉。
4. 同一任務搬到 Google 與刪除／暫停操作可能同時送出：共用任務鎖。
5. 建立任務期間改選項會顯示錯誤成功提示／清空新貼輸入：以提交時快照說明，只清空與提交時相同的文字。

藍方結果：

- `python -m pytest -q`：193 passed。
- `node --test tests/ui.test.cjs`：11 passed。
- `node --test edge/worker.test.mjs`：14 passed（既有入口本地測試；不等於正式 Worker 可用）。
- 內建瀏覽器 loopback 虛構資料：935 文件／10 頁；搜尋第1組選取100，再跨頁選第101文件；回到第1頁和刷新仍101，實際虛構端提交 IDs 正好1–101，只有一次 selection。
- 詳情與列表暫停→繼續按鈕跟隨狀態；取消確認返回不提交、確認後才提交。無原生對話框。空搜尋、101件等待文件篩選（2頁）正常。
- 390×844 viewport：整頁 scrollWidth 390，表格自身 700／可視332；驗收後恢復預設 viewport。修復後重新載入沒有新 JavaScript error。
- 虛構驗收服務 `tests/ui_preview.py` 僅綁127.0.0.1，不讀 `.env`／DB／OAuth、不連雲盤。圖在工作區 `Outputs/ui1`，圖中 DEMO 不是生產任務。

## Oracle 不重啟部署與復原

- 部署前後 `panbridge` MainPID **547939** 不變；panbridge、cloudflared active。Jinja auto_reload=True，無須重啟服務。
- 2026-10-01 01:39:58 UTC：已登入 loopback `/`、`/tasks/16`、`/tasks/18` HTTP200，均含UI1；公開靜態helper HTTP200，未登入 `/api/tasks` 仍401。健康版本0.5.4。
- 四檔本機／遠端 SHA-256 一致；helper實際HTTP回應SHA亦一致：

| 檔案 | SHA-256 |
| --- | --- |
| web/static/ui.js | d1635221c04b7c4526ceb338188370ed93cf2a19413e73dfa59d2d5a82782864 |
| web/static/style.css | 2fa92882c111287786a4f73af74848b40e63c5ea97d979e1d4973c0aac56a73b |
| web/templates/index.html | f8dadf5310ec0036975349368136b72a3cf051f8522efc04e455e7fcdbcf4dd0 |
| web/templates/task.html | c19366128cfb0a77d96337c88eb361905535d42dc6813202bcd99b65684c7081 |

- 復原包 `/home/ubuntu/panbridge-backups/20261001-ui1/web-before.tgz` 僅含原三個web檔，無DB／憑證。需要回復時先核對包內容與目標，恢復原模板／style，不必重啟；新增ui.js可保留（旧模板不載入），避免不必要刪除。
- #16 uploading10.81%（86done、1failed）、#18 downloading32.10%（220done）；未改任務狀態、未重試、未啟動重複任務或刪備份。

## 未完成／不作保證

域名正式訪問尚未恢復；沒有切換 Worker URL、DNS、PUBLIC_BASE_URL 或 OAuth 回呼。這次只能確認本機虛構GUI與Oracle生產loopback／靜態部署，不能當成用戶端正式域名GUI、Infuse／VLC或Windows實機驗收。

Google文件保持私人；Infuse直接連GoogleDrive的產品能力見 [Firecore](https://firecore.com/infuse)，Drive電腦版串流文件可在Mac Finder／Windows檔案總管存取，見 [Google官方說明](https://support.google.com/drive/answer/13401938?hl=en)。VLC透過電腦版讀取是使用建議，仍須實機／影片格式驗收；不等於Google匿名直鏈或本站直接播放已完成。
