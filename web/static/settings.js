/* Plaintext feedback, guarded writes, cancellable serialized QR reads. */
(function (root) {
  'use strict';
  function init(root, PB) {
    const node = id => root.document.getElementById(id);
    const value = id => node(id).value;
    const qr = new Map();
    const text = (id, message, kind = 'muted') => {
      node(id).textContent = message; node(id).className = kind;
    };
    const post = (url, payload) => PB.request(url, {method:'POST',
      ...(payload === undefined ? {} : {headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})});
    async function save(url, payload, method='POST') {
      const d = method==='POST' ? await post(url,payload) : await PB.request(url,{method});
      if (d.ok !== true) throw new Error('操作結果尚未確認，請刷新連線狀態，不要連續提交。');
    }
    async function providers() {
      try {
        const data = await PB.request('/api/auth/me');
        if (!Array.isArray(data.providers)) throw new Error('帳號資料格式異常，請刷新確認。');
        const names = {google:'Google Drive',quark:'夸克網盤',baidu:'百度網盤',pcloud:'pCloud'};
        const list = data.providers.filter(p => p && Object.hasOwn(names,p.provider));
        text('providers', list.length ? list.map(p => names[p.provider]+' · 更新於 '+p.updated_at).join('\n') : '尚未連接帳號。請先連接 Google Drive，再設定下載來源。');
      } catch(e) { text('providers',e.message,'err'); }
    }
    async function googleStatus() {
      try {
        const d = await PB.request('/api/auth/google/status');
        if (typeof d.connected !== 'boolean' || typeof d.configured !== 'boolean') throw new Error('Google 連線資料格式異常，請刷新確認。');
        text('google-status', d.connected ? 'Google Drive 已連接。現有任務仍綁定原帳號。' : d.configured ? 'OAuth 已設定。請連接正確的 Google One 帳號。' : '尚未設定 OAuth 用戶端。');
      } catch(e) { text('google-status',e.message,'err'); }
    }
    async function refresh() { await Promise.all([providers(),googleStatus()]); }
    function bind(id, group, messageId, action) {
      node(id).onclick = () => PB.once('settings:'+group, async () => {
        const buttons = root.document.querySelectorAll('[data-account="'+group+'"]');
        buttons.forEach(b => b.disabled = true); text(messageId,'處理中…');
        try { await action(); }
        catch(e) { text(messageId,e.message,'err'); }
        finally { buttons.forEach(b => b.disabled = false); }
      });
      node(id).setAttribute('data-account',group);
    }
    function stopQR(prefix, message) {
      const entry = qr.get(prefix);
      entry?.poll?.stop(); qr.delete(prefix);
      node(prefix+'-stop').hidden = true; node(prefix+'-qrbox').hidden = true;
      node(prefix+'-img').removeAttribute('src');
      if (message) text(prefix+'-status',message);
    }
    async function drainQR(prefix, message) {
      const entry=qr.get(prefix); stopQR(prefix,message);
      // A final GET may persist a confirmed QR credential on the server.
      // Let it settle before a replacement or DELETE, so it cannot undo that write.
      if (entry?.flight) {try {await entry.flight;} catch {}}
    }
    function showQR(prefix, data) {
      const raw = prefix === 'qk' ? data.qr_data_url : data.imgurl;
      if (!raw) return;
      let safe = false;
      if (prefix === 'qk') safe = /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(raw);
      else {
        try {
          const url = new URL(raw);
          safe = url.protocol === 'https:' && !url.username && !url.password && (url.hostname === 'baidu.com' || url.hostname.endsWith('.baidu.com'));
        } catch {}
      }
      if (!safe) throw new Error('二維碼格式異常，未載入外部圖片。請重新開始。');
      node(prefix+'-img').src = raw; node(prefix+'-qrbox').hidden = false;
    }
    function qrState(prefix, data) {
      const labels = {pending:'等待掃碼',scanned:'已掃碼，請在手機確認',confirmed:'已登入',expired:'二維碼已過期，請重新開始',error:'掃碼失敗，請重新開始'};
      if (!Object.hasOwn(labels,data.status)) throw new Error('掃碼狀態異常，請重新開始。');
      text(prefix+'-status',labels[data.status]); showQR(prefix,data);
      return ['confirmed','expired','error'].includes(data.status);
    }
    async function startQR(prefix, provider) {
      await drainQR(prefix);
      const data = await post('/api/auth/'+provider+'/qr/start');
      if (typeof data.id !== 'string' || !/^[a-f0-9]{32}$/.test(data.id)) throw new Error('掃碼識別碼異常，請重新開始。');
      const entry = {expires:Date.now()+600000}; qr.set(prefix,entry); node(prefix+'-stop').hidden = false;
      if (qrState(prefix,data)) {
        stopQR(prefix); text(prefix+'-msg',data.status==='confirmed'?'登入成功。':'請重新開始掃碼。'); await providers(); return;
      }
      text(prefix+'-msg','切換到其他頁面時暫停檢查。可按「停止等待」結束。');
      entry.poll = PB.poll(async () => {
        if (Date.now() >= entry.expires) {stopQR(prefix,'掃碼等待已結束，請重新開始。');return;}
        let state;
        entry.flight = PB.request('/api/auth/'+provider+'/qr/'+data.id);
        try {state = await entry.flight;} finally {entry.flight=null;}
        if (qr.get(prefix) !== entry) return;
        if (qrState(prefix,state)) {
          stopQR(prefix);
          text(prefix+'-msg',state.status==='confirmed'?'登入成功，憑證已保存。任務不會自動重試。':'掃碼已結束，請重新開始。',state.status==='confirmed'?'ok':'err');
          await providers();
        }
      }, {interval:2000,maxInterval:15000,onError:e=>{
        if (qr.get(prefix)===entry) text(prefix+'-status',e.message,'err');
      }});
    }
    bind('accounts-refresh','status','providers',refresh);
    bind('google-save','google','google-msg',async()=>{
      if (!value('google-client').trim() || !value('google-secret').trim()) throw new Error('請填寫 Client ID 和 Client Secret。');
      await save('/api/auth/google/config',{client_id:value('google-client').trim(),client_secret:value('google-secret').trim()});
      node('google-secret').value=''; text('google-msg','用戶端已加密保存。','ok'); await googleStatus();
    });
    bind('google-connect','google','google-msg',async()=>{
      const d=await post('/api/auth/google/start'); let url;
      try {url=new URL(d.url);} catch {throw new Error('授權網址格式異常，未跳轉。');}
      if (url.protocol!=='https:' || url.hostname!=='accounts.google.com' || url.username || url.password || url.port) throw new Error('授權網址不屬於 Google 官方登入，未跳轉。');
      root.location.href=url.href;
    });
    bind('google-clear','google','google-msg',async()=>{
      if (!await PB.confirmAction('斷開會使未完成的 Google 搬運無法繼續；不刪除雲端文件。確定斷開？','斷開 Google Drive')) {text('google-msg','已取消，連線設定未變。');return;}
      await save('/api/auth/google',undefined,'DELETE'); text('google-msg','已斷開 Google Drive，雲端文件保留。','ok'); await refresh();
    });
    for (const [prefix,provider,label] of [['qk','quark','夸克'],['bd','baidu','百度']]) {
      bind(prefix+'-qr',provider,prefix+'-msg',()=>startQR(prefix,provider));
      node(prefix+'-stop').onclick=()=>stopQR(prefix,'已停止等待。這不會撤回手機端已確認的登入；請刷新連線狀態。');
      bind(prefix+'-cookie-save',provider,prefix+'-msg',async()=>{
        if (!value(prefix+'-cookie').trim()) throw new Error('請先貼上完整 Cookie。');
        await drainQR(prefix,'已停止掃碼，改用手動憑證。');
        await save('/api/auth/'+provider+'/cookie',{cookie:value(prefix+'-cookie')});
        node(prefix+'-cookie').value=''; text(prefix+'-msg',label+'憑證已保存。','ok'); await providers();
      });
      bind(prefix+'-clear',provider,prefix+'-msg',async()=>{
        if (!await PB.confirmAction('清除'+label+'登入憑證會影響未完成下載；不刪任務或文件。確定清除？','清除憑證')) {text(prefix+'-msg','已取消，憑證未變。');return;}
        await drainQR(prefix,'已停止掃碼。');
        await save('/api/auth/'+provider,undefined,'DELETE'); text(prefix+'-msg','憑證已清除。','ok'); await providers();
      });
    }
    bind('pc-save','pcloud','pc-msg',async()=>{
      if (!value('pc-email').trim() || !value('pc-pass')) throw new Error('請填寫電子郵件和密碼。');
      await save('/api/auth/pcloud/login',{email:value('pc-email').trim(),password:value('pc-pass'),api_host:value('pc-host'),code:value('pc-code').trim()});
      node('pc-pass').value='';node('pc-code').value='';text('pc-msg','pCloud 已連接。','ok');await providers();
    });
    bind('pc-token-save','pcloud','pc-token-msg',async()=>{
      if (!value('pc-token').trim()) throw new Error('請填寫 auth token。');
      await save('/api/auth/pcloud/token',{auth:value('pc-token').trim(),api_host:value('pc-token-host')});
      node('pc-token').value='';text('pc-token-msg','pCloud 已連接。','ok');await providers();
    });
    root.addEventListener('pagehide',()=>{stopQR('qk');stopQR('bd');});
    refresh(); return {refresh,stopQR};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = init;
  else init(root,root.PB);
})(typeof window !== 'undefined' ? window : globalThis);
