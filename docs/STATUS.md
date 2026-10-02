# 生產狀態快照（可公開）

## 2026-10-02 v0.5.5：Oracle 磁碟預取流水線已部署

- 使用者明確要求擴容後正式優化下載模式。本次程式提交 `23e8837` 已推送 GitHub main、部署 Oracle；原網址 HTTPS `/api/health` 與 loopback 均回報 0.5.5，panbridge／cloudflared active，關鍵六個程式文件本地／Oracle SHA-256 全同。
- 真實設定：prefetch 3、staging 68,719,476,736 bytes（64 GiB）、reserve 2,147,483,648 bytes（2 GiB）、download connections 6、Baidu 4；max jobs 原設定仍為2。流水線的下載／上傳各1與3件預算在 Worker 跨任務共用，不把每任務連線乘倍。
- 本機與 Oracle 的獨立乾淨發布目錄均213程式測試全通過，ops安全9項通過；本機UI／入口25項通過。紅軍找到並修復未知大小漏算失敗留存暫存及收尾狀態競態；最後hard-fail latch復審5項通過。詳見 [PIPELINE_REVIEW.md](PIPELINE_REVIEW.md)。
- 安全切換短暫停止／啟動下載服務以落盤，沒有按resume/retry或重建任務。切換後確認先前296件#16 done及405件#18 done的ID、大小、下載／上傳bytes全保留；#16的14個既有failed沒有重新排隊，兩任務清單與帳號綁定不變。
- 21:17日本時間唯讀核對：#16 downloading 53.51%，297／935 done，已交付36,526,125,708 bytes，14 failed、1 downloading、623 queued。#18仍405／405 done、25,976,502,866 bytes。free147,653,750,784 bytes。
- 任務詳情已顯示流水線上下行／等待數；已完整預取文件可直接續上傳。來源暫時慢於模擬，不宣稱實際網速已提升某倍。已記錄第一個發布後完成文件的兩階段耗時，這些包含取鏈／續傳，不能當同檔改動前後純CDN測速。
- 備份仍474／477 ready、3 not done且帳號一致，未重複查數百件Google、未清理Mac。完整搬運／Google全量SHA-256／Windows和Infuse播放仍未完成，不由本次發布代替。
- 復原包在Oracle私有`/home/ubuntu/panbridge-backups/20261002-v055/code-before.tgz`；只備份舊程式與必要任務欄位快照。沒有DB schema改動、沒有匯出正式DB或憑證；若復原只能撤回程式，不能覆蓋已新增交付紀錄。發布中一條同步方向錯誤的組合命令被安全檢查攔下，未執行；先恢復舊服務後改為僅在Oracle內同步新版程式，無資料外傳。

## 2026-10-02 17:55 日本時間：原網址在 Mac／WARP 恢復

- 依使用者「修復這個問題」「請繼續」，17:47 在既有 Network Phishing Block 表達式加入只限 `panbridge.tdtc.indevs.in` 且目的埠 443 的排除條件。保存後讀回：`any(net.fqdn.security_category[*] in {131}) and not(net.sni.host == "panbridge.tdtc.indevs.in" and net.dst.port == 443)`；仍為 Block、啟用、順序 1。獨立 Malware Block 仍啟用、順序 2、未改動，先前 DNS 精確主機允許保留。
- Mac 實測 WARP 為 Connected／Network healthy；原網址 HTTPS `/api/health` 為 200、版本 0.5.4，`/login` 為 200，未登入 `/api/tasks` 為 401。沒有使用不安全的憑證選項。內建瀏覽器實際載入原網址的已登入任務列表，顯示 Google Drive、選檔、暫停和 UI 1；已通過原網址訪問驗收。
- 這次精確變更前握手失敗、變更後同 Mac／WARP 成功，支持 Gateway Network 分類攔截是剩餘障礙；未取得歷史分類變更或精確封鎖命中日誌，因此不宣稱已確定最早何時、為何改變分類。
- 17:55 唯讀核對 #16 downloading 51.46%、278／935 done，已交付 35,127,462,398 bytes；13 份 failed、1 downloading、643 queued。#18 done、405／405，大小及下載／上傳 bytes 均為 25,976,502,866。兩服務 active，free 147,684,950,016 bytes；本次沒有重啟或重排任務。
- 備份仍 474／477 ready、3 not done、帳號綁定一致；Mac 477 份保留，未重複查 Google API。外部播放器／Windows 實機播放仍屬獨立驗收，不由網站訪問成功代替。
- 使用入口仍為 `https://panbridge.tdtc.indevs.in/`；候選 workers.dev 遷移沒有完成，Google 回呼與應用程式公開來源保持原網址。私密規則復原資料與前後截圖保存在工作區 `Outputs/network-repair/`。

## 2026-10-02 15:54 日本時間：單次路由核對與失敗文件補傳

- 使用者當次明確批准後，只對 `panbridge.phrixusjhon.workers.dev` 完成一次停用／重新啟用，最後生產開關為 enabled、preview URLs 仍 disabled。沒有改其他網址、DNS、Access、防護、OAuth、公開來源或下載服務。
- Mac／Oracle 隨後正式 `/api/health` 仍 HTTP 500／1101。Ray：`a441b775edb41be3-NRT`、`a441b7a40a6b23a4-KIX`。正式回應沒有本地入口程式預期的 HSTS／no-referrer 標頭；這是執行鏈差異線索，不是根因確診。
- Quick Edit 仍顯示 `c1704060` 使用中；完整讀回 4,260 字元與本地 `edge/worker.mjs` 完全相同。HTTP 預覽返回 302 登入跳轉、no-store／HSTS／no-referrer；14 項本地入口測試全部通過，均不代表正式訪問恢復。
- 計畫用暫時固定 503、無上游／文件／登入的維護回應隔離診斷；正式編輯器改寫被操作審核攔下，尚未寫入或部署。已另問明確批准「一次測試並立即還原」。本地 probe 只在工作區 Outputs，不能當作線上版本。
- #18 早間讀取為 failed、397／405 done、8 ConnectTimeout。經既有 authenticated loopback retry API 僅重新排隊 8 份未完成文件；操作前後 397 個 done 文件 ID／size 完全相同，不建立新任務。
- 15:53 #18 已 downloading 96.95%、399／405 done、1 downloading、5 queued、0 failed；#16 downloading 49.73%、263／935 done、11 個既有 failed、1 downloading，其餘660 queued。未中斷 #16 或無限重試。
- 備份映射 472／477 ready、5 not done、帳號一致，沒有缺失或歧義；未再查數百份 Google API、未刪 Mac 副本。free 147,447,914,496 bytes，兩服務 active。

## 2026-10-01 17:14 日本時間：備份校驗工具與持續監控授權

- 使用者明確持續授權後续定時唯讀檢查；已更新原 `panbridge` 排程，仍為每 30 分鐘，不新增重複排程或放寬整體安全設定。系統權限流程仍遵守。
- 新增獨立 `ops/` 逐檔校驗工具，不修改或重啟下載程式。9 項安全測試通過；原 477 份備份唯一映射為 399 ready／78 not done，沒有缺失或歧義。
- 399 份 Google 官方 files.get 實測全部大小及 SHA-256 匹配、未放入垃圾桶；Google 帳號與任務綁定一致，API 後對應仍一致。結果保存本地私密 `google-verification.json`，全批 `complete=false`；Mac 備份保留。
- 工具只在 Oracle 使用現有有效存取憑證、只讀 SQLite、只發 Google GET，不自行更新／匯出憑證、不建立分享、不改文件；失敗清除驗收旗標。正常定時先做低成本映射，全部 477 ready 後再完整 fresh 校驗。
- 17:06 #16 downloading 23.21%、99／935 done；#18 downloading 47.23%、308／405 done；兩服務 active，可用 147,103,268,864 bytes。公開新入口重查仍 HTTP 500；一次路由重登記尚未獲操作批准，沒有執行。

## 2026-10-01 v0.5.4 · UI 1：不中斷搬運的介面更新

- 任務列表／詳情改為串行刷新，不重疊；失敗有上限退避，隱藏頁面停止刷新。操作請求不自動重播，異常回應不宣稱成功。
- 文件名稱／資料夾／狀態篩選，每頁 100 件；跨頁選取保留、顯示已選數量與容量。批量選取由反覆重算改為一次遍歷；未量測下載速度提升。
- 取消／刪除改為頁面內確認、錯誤与操作結果在頁面內可見；異步雲端位置改為使用者點擊連結，避免新視窗被封鎖。勾選框、鍵盤焦點與窄螢幕表格捲動修復。
- 193 項 Python 回歸、11 項 UI 邏輯、14 項既有入口邏輯測試全部通過。內建瀏覽器以虛構 935 文件驗證搜尋／篩選／跨頁 101 件精確提交、暫停／繼續／取消確認；沒有操作真實任務。詳見 [UI1_REVIEW.md](UI1_REVIEW.md)。
- 僅部署四個 web 檔案，沒有重啟服務，MainPID 部署前後均 547939、兩服務 active；本機／Oracle SHA-256 完全一致。Oracle loopback 已登入列表和 #16/#18 詳情均 HTTP 200 且包含 UI 1，未登入 API 仍 401；後端健康版本仍 0.5.4。
- 10:39 日本時間 #16 uploading 10.81%，86／935 done、1 份既有失敗；#18 downloading 32.10%，220／405 done；總 bytes 與預期相符。Oracle 可用 145,620,160,512 bytes。未重排失敗文件、未新增下載、未刪 Mac 備份。
- 更新 README 和交接文件；復原點 `/home/ubuntu/panbridge-backups/20261001-ui1/web-before.tgz`。新域名正式入口仍未通過驗收，沒有變更 DNS／Worker 路由／Google OAuth 回呼。公開入口的實際 UI 與播放器驗收不以本機模擬／loopback 替代。

## 2026-10-01 改用新入口：尚未完成遷移

- 使用者要求改域名，不再增加安全分類例外。既有可用網站區域均在 `indevs.in` 或 `eu.cc` 下；Radar 對 `tdtc777.eu.cc` 亦顯示從 `eu.cc` 繼承 Phishing／CIPA Filter。反向 IPv6 區域不適合作為網站入口；未逐一反覆測試同父域名稱。
- 在使用者原 Cloudflare 免費帳戶建立獨立 Worker `panbridge`，候選網址 `https://panbridge.phrixusjhon.workers.dev`；不購買域名、不新增付費方案、不改其他網站。
- 固定 HTTPS 上游為既有 PanBridge，沒有開放任意代理、快取私密回應或擴大 Google Drive 權限。使用者同意折衷記錄：關閉自動完整請求叫用記錄、保留功能類別／狀態／耗時／錯誤代碼的去敏感化程式記錄。
- Dashboard 顯示 `f4b9de32` 為使用中、100% 流量，Quick Edit 預覽能抵達應用程式並返回登入 302。Mac、Oracle 與內建瀏覽器的正式網址均返回 Cloudflare 500／1101；HTTPS 能建立不等於網站恢復，原因尚未確定。
- 13:47 日本時間，最後一輪容錯修改已成功寫入，讀回 4,260 字元與本地程式逐字一致；Dashboard 確認 `c1704060` 使用中，14 項入口測試再次通過。但 Mac／Oracle 正式 `/api/health` 仍為 500／1101，預覽 HTTP 為正常 302；尚未完成正式入口修復。詳見 `edge/README.md`。
- 即時日誌沒有收到刻意發出的正式請求事件，形成路由／執行鏈差異線索，尚未確診。未開啟新的敏感記錄；僅一次新網址停用／再啟用待當次明確批准，未執行。
- 13:50 日本時間 #16／#18 持續 downloading，93／935、290／405 done，16.73%／37.81%；各一份 ConnectTimeout 待補傳，其餘繼續。Oracle free 146,028,711,936 bytes。Mac 備份未刪除，不以進度替代 Google 全量校驗。
- 重新登記新入口生產 URL 需要停用再啟用；當次操作檢查未允許，沒有切換路由。原 Google OAuth 回呼、Oracle 公開網址、Tunnel 和下載服務均未改動；原 DNS 新例外亦尚未清理，沒有建立 Network Allow 例外。
- 09:40 日本時間核對 #16／#18 仍 downloading，86／935 與 196／405 done，9.50%／29.08%，#16 仍有 1 份已知逾時待补傳。Oracle 可用 146,854,551,552 bytes；Mac 備份保留，沒有新增或重置搬運任務。

## 2026-10-01 本站 DNS 例外與連線層驗收

- 使用者確認後，在既有 Gateway 建立「PanBridge 精確主機名允許」DNS 政策：`dns.fqdn == "panbridge.tdtc.indevs.in"`，Allow、啟用、優先順序 1。沒有放行整個 `indevs.in` 或其他子域名；原兩條安全封鎖規則保持啟用及相對順序，DNSSEC 驗證保持開啟，未更改 WARP／本機 DNS 設定。
- Gateway DNS 與 Mac 系統解析均已由 0.0.0.0／:: 恢復正常 Cloudflare A／AAAA 位址，WARP 仍 Connected／Network healthy。
- Mac HTTPS 握手仍中斷（IPv4／HTTP1.1 亦同），內建瀏覽器返回 QUIC 連線錯誤；不能宣稱網站已恢復。Oracle 端公開健康檢查正常。唯讀核對另有已啟用的 network Phishing 封鎖，符合剩餘攔截的可能原因，尚無精確命中日誌。
- HTTP 控制台明確提示 TLS 解密未開啟，HTTP 政策不作用於 HTTPS；沒有更改 HTTP／TLS 解密。下一步建議僅匹配本站 exact SNI 且目的埠 443 的 Network Allow 例外，須另獲使用者確認；不放行共享 CDN IP，不停用分類封鎖。
- 09:17 日本時間核對 #16／#18 仍 downloading，84／935 與 195／405 done，進度 8.80%／28.62%；#16 已知單份逾時保留待補傳，Mac 備份未清理。

## 2026-10-01 v0.5.4 來源連結逾時修復

- 原 #13/#14 已於 9 月 30 日依使用者指示啟動 Google Drive 任務 #16/#18，分別 935 文件／68,297,220,656 bytes、405 文件／25,976,502,866 bytes；重複 #17 保持暫停，沒有重試停用的 OneDrive 目標。
- PanBridge 開機磁碟已由 50 GB 線上擴至 150 GB、ext4 同步擴大，仍為永遠免費；另一台主機保留 50 GB。A1 容量檢查已停止。
- 早間核對 #16/#18 均在下載，已交付 82／181 文件；發現 7／14 個文件因取得夸克下載連結時 ConnectTimeout 而跳過，錯誤文字空白。這不是 Google 授權失效或磁碟不足。
- 只對可安全重播的下載連結查詢新增 3 次有上限退避重試，處理連線中斷、逾時及 HTTP 408/429/5xx；不重播轉存／移動／刪除請求，不重試登入失效，不吞取消。連線建立逾時從 60 秒改為 15 秒，其餘讀取逾時維持 60 秒。
- 錯誤提示不再因空字串而空白，傳輸例外只顯示類型與恢復提示，不帶請求網址或憑證。
- Mac 備份仍保留；477 份對應 Google 文件逐一大小及 SHA-256 校驗通過前不得刪除。整批搬運、清理及播放器驗收尚未完成。
- 本機 WARP 已連接且網路健康；其受網路政策管理的 Gateway DNS 對本站返回 0.0.0.0/::，Cloudflare 公開 DNS 返回正常位址。同帳戶啟用了安全分類封鎖政策；Gateway 日誌目前無結果，未取得精確命中規則 ID。
- [Cloudflare Radar 即時分類](https://radar.cloudflare.com/domains/feedback/panbridge.tdtc.indevs.in) 明確顯示本站從上層 `indevs.in` 繼承 Phishing／CIPA Filter。這與安全分類攔截相符，不是本站遭入侵的證據；未提交分類變更，也未修改 DNS、WARP 或安全策略。僅限本站的 DNS 例外須另獲使用者確認。
- v0.5.4 修復提交 `29f6493` 已推送 GitHub main 並部署 Oracle；本地完整回歸 193 tests 通過，部署檔案 SHA-256 與發布一致，健康端點回報 0.5.4。部署前保存程式復原包，暫停兩任務以保存續傳，再恢復原任務。
- 恢復後 21 個 failed 文件已重新排隊，82／193 個已完成文件狀態保持；10 月 1 日 08:54（日本時間）核對 #16 downloading 8.32%、83／935 done，#18 downloading 27.70%、193／405 done，兩者當下沒有 failed 文件。抽查先前失敗的文件 #4870，已完整下載並交付。這不代表其餘 20 份已交付或整批搬運完成。
- 09:03 再核對 #16 已交付 84／935、進度 8.58%，#18 已交付 194／405、進度 28.24%，均持續 downloading。#16 文件 #4874 在有上限重試後仍 ConnectTimeout，下載 bytes 0，已留下非空恢復提示；其餘文件繼續，這份待補傳。不可把有限重試等同外部網路不會再失敗，或聲稱全部文件已恢復成功。

## 2026-09-30 暫存備份與已授權清理完成

- 原 #13 暫存 137 文件、26,465,458,567 bytes；原 #14 暫存 340 文件、17,176,808,597 bytes。合計 477 文件、43,642,267,164 bytes，已完整備份到使用者 Mac。
- Mac 副本逐檔 SHA-256 與備份前來源一致；來源大小／時間／歸屬在備份後核對一致。清理前再次核對全部 Mac 文件雜湊，確認副本完整。這是複製一致性驗收，不是原始分享來源內容的驗真。
- 使用者明確授權後，只依清單移除原 #13/#14 的 477 個 Oracle 暫存文件及空目錄，未遞迴清理其他資料。任務記錄、憑證、Mac 備份及雲端文件保留。
- Oracle 可用空間由 69,009,408 bytes 增至 43,713,130,496 bytes（約 40.7 GiB），根磁碟使用率約 15%。panbridge/cloudflared 均 active；Oracle 本機與從 Oracle 檢查公開 HTTPS 健康端點均回報 0.5.3。
- #13/#14 保留原 failed 歷史記錄，原下載進度不代表 Oracle 暫存仍存在。新 Google #16/#18 仍 awaiting_selection，文件數 935/405，未自動勾選或開始搬運。整批新下載與 Infuse/VLC 播放仍待驗收。
- 備份完成檢查排程已停止。此清理不改變程式版本。Mac 命令環境對本站解析到 0.0.0.0/::，未更動任何 DNS/VPN/安全設定；這不等於已證實內建瀏覽器無法連接。

## 2026-09-30 v0.5.3 重新搬運操作修復

- 建立 #14 Google 任務時，內建瀏覽器操作逾時，伺服器核對沒有建立新任務。使用者截圖確認原生確認框仍開啟；先前未及時辨識彈窗是操作判斷失誤，不應等同服務失效。
- 重新搬運改為頁面內二次確認及頁面內錯誤提示，不依賴瀏覽器原生 confirm/alert；保留請求中禁用及後端防重複。只建立選檔任務，不自動勾選或下載。
- 179 tests 通過，Oracle 健康端點回報 0.5.3。內建瀏覽器實測頁面內確認可操作，確認後復用 #16，沒有建立重複任務。
- 使用者處理確認框後，資料庫核對 #13 → #16（935 文件）、#14 → #18（405 文件），兩者均等待選檔，下載／上傳 bytes 均 0；#17 是 #13 的重複記錄，維持暫停。沒有開始任何新任務文件下載。

## 2026-09-30 v0.5.2 防重複任務熱修復

- 內建瀏覽器建立請求逾時後發現 #16/#17 為同一來源的 Google 任務。#16 已列檔等待選擇；#17 已暫停，沒有開始下載。原 #13/#14 保留。
- 重新搬運 API 在原任務鎖內復用相同來源、帳號、目標路徑的未完成任務；介面亦增加請求中的重入防護。新並行回歸測試確認兩次請求返回同一任務。

## 2026-09-30 Google 真實連接與上傳驗收

- 內建瀏覽器實測返回 `settings?google=connected`，設定頁顯示「Google Drive 已連接」。伺服器 API 核對為使用者指定的 Google One 帳號，配額 5,497,558,138,880 bytes（5 TiB）；續期憑證已加密保存。
- 私有驗證資料夾 `/PanBridge/連接驗證-20260930` 上傳 #14 暫存 `说明.zip`（484 bytes）及 #13 暫存 `Lesson01.mp3`（8,415,190 bytes）。沒有新下載、沒有刪除原暫存或改寫原任務完成狀態。
- 兩檔皆由 Google API 讀回，內容逐位元與本地相同、大小一致；permissions 無 anyone，重試返回同一文件 ID，未生成重複副本。
- MP3 主動中斷於 8,388,608 bytes，完整本地文件及加密會話檢查點保留；重新建立上傳器後，首次進度即為同一偏移，完成後內容驗證通過。
- SHA-256：ZIP `d52e808c11938c9828692e6fd754fca2305a0c65e9f95320fd17dc44ccae87b8`；MP3 `2daf8b96f55ad603618a61a3565a45b53fcea47ee79f6aa92ce0a30ce2782ae9`。
- 此驗收涵蓋 Google 上傳／讀回／恢復／防重複，不代表來源重新下載、大任務完整流程或 Infuse/VLC 真實播放已驗收。
- 舊暫存 477 個文件大小與記錄一致；這不是來源雜湊校驗。Oracle 尚僅約 70 MB 可用，仍等待使用者決定 41 GB 暫存的保留或清理方式。

## 2026-09-30 v0.5.1 介面清理

- 177 tests 通過，Oracle 程式已同步；內建瀏覽器實際確認設定頁移除停用目標區塊。回復程式備份：`/home/ubuntu/panbridge-backups/20260930-v051/code.tgz`。

- 依使用者要求，移除已停用目標的設定、狀態、容量查詢及新增任務選項。
- 舊任務保留原資料，以「舊目標（已停用）」呈現；隱藏失效重試、播放、雲端位置與舊錯誤，保留重新搬運到 Google Drive 入口。
- 沒有删除憑證、任務、下載暫存或雲端文件。後端歷史適配保留供資料相容，介面不再推薦使用。
- 本次觀察 Google 返回顯示 `login required`，尚不能聲稱使用者 Google Drive 已連接；須重新確認本站會話並重新開始授權。

## 2026-09-30 v0.5.0 發布

- Google Drive 適配及暫停、選檔、刪除功能已實作；174 tests 通過，紅藍軍已修正並復審帳號綁定、選檔繞過、上傳完成檢查與資料夾競態。
- Oracle 已同步新版並重啟，公開 HTTPS `/api/health` 實測回報 `0.5.0`；Google 真實搬運驗收尚未完成。
- Google Cloud OAuth 品牌及專用 Web 用戶端已建立；憑證已加密保存於 Oracle，解密格式驗證通過，未放 GitHub。尚未連接使用者雲盤。
- 品牌首頁與隱私連結已保存，公開 `/privacy` HTTP 200；使用者確認後，Google 控制台實測 OAuth 發布狀態「實際運作中」。尚未取得 Google 使用者文件授權，不等於文件搬運已驗收。
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
