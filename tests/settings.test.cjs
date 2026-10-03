const {test}=require('node:test');
const assert=require('node:assert/strict');
const init=require('../web/static/settings.js');
const PB=require('../web/static/ui.js');
const flush=()=>new Promise(r=>setImmediate(r));
function fixture(){
  const nodes=new Map(),requests=[],polls=[];let handler=async url=>{
    if(url.endsWith('/me'))return {providers:[{provider:'google',updated_at:'today'},{provider:'onedrive',updated_at:'old'}]};
    if(url.endsWith('/status'))return {connected:true,configured:true};
    return {ok:true};
  };
  const node=id=>{
    if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,disabled:false,attributes:{},style:{},
      textContent:'',setAttribute(k,v){this.attributes[k]=v;},removeAttribute(k){delete this[k];},
      set innerHTML(v){throw new Error('HTML sink forbidden');}});
    return nodes.get(id);
  };
  const root={location:{href:''},document:{getElementById:node,
    querySelectorAll:selector=>[...nodes.values()].filter(n=>selector.includes('"'+n.attributes['data-account']+'"'))},addEventListener(){}};
  let confirms=0,answer=true;
  init(root,{once:PB.once,request:async(url,options={})=>{requests.push({url,options});return handler(url,options);},
    confirmAction:async()=>{confirms++;return answer;},poll:(task,options)=>{
      const p={task,options,stopped:false,stop(){this.stopped=true;}};polls.push(p);return p;
    }});
  return {node,root,requests,polls,setHandler:fn=>handler=fn,setAnswer:a=>answer=a,get confirms(){return confirms;}};
}
test('settings uses plaintext for hostile errors and credential provider values',async()=>{
  const f=fixture();await flush();
  assert.doesNotMatch(f.node('providers').textContent,/onedrive/);
  const hostile='<img src=x onerror=attack()>', message='失敗：'+hostile;
  f.node('qk-cookie').value='fictional cookie';
  f.setHandler(async()=>{throw new Error(message);});
  await f.node('qk-cookie-save').onclick();
  assert.equal(f.node('qk-msg').textContent,message);
  assert.equal(f.node('qk-cookie').value,'fictional cookie');
  assert.equal(f.node('qk-cookie-save').disabled,false);
});
test('malformed successful responses do not clear inputs or claim saved',async()=>{
  const f=fixture();await flush();
  f.node('google-client').value='fictional';f.node('google-secret').value='fictional';
  f.setHandler(async()=>{throw new Error('回應格式異常，操作結果尚未確認');});
  await f.node('google-save').onclick();
  assert.match(f.node('google-msg').textContent,/尚未確認/);
  assert.equal(f.node('google-secret').value,'fictional');
});
test('account mutation lock prevents duplicate writes and unsafe OAuth navigation',async()=>{
  const f=fixture();await flush();let release;
  f.setHandler(()=>new Promise(r=>release=r));
  const first=f.node('google-connect').onclick();
  await f.node('google-connect').onclick();
  assert.equal(f.requests.filter(r=>r.url.endsWith('/start')).length,1);
  assert.equal(f.node('google-clear').disabled,true);
  release({url:'https://accounts.google.com.evil.invalid/?code=fictional'});await first;
  assert.equal(f.root.location.href,'');assert.match(f.node('google-msg').textContent,/未跳轉/);
});
test('disconnect cancellation makes no DELETE and successful mutations clear secrets',async()=>{
  const f=fixture();await flush();f.setAnswer(false);
  await f.node('google-clear').onclick();assert.equal(f.confirms,1);
  assert.ok(f.requests.every(r=>r.options.method!=='DELETE'));
  f.node('bd-cookie').value='fictional';
  await f.node('bd-cookie-save').onclick();assert.equal(f.node('bd-cookie').value,'');
});
test('QR stop/restart ignores stale responses and stops terminal polls',async()=>{
  const f=fixture();await flush();let finish;
  f.setHandler(async url=>url.endsWith('/start')?{id:'a'.repeat(32),status:'pending',qr_data_url:'data:image/png;base64,YQ=='}:
    new Promise(r=>finish=r));
  await f.node('qk-qr').onclick();const old=f.polls[0];
  const reading=old.task();f.node('qk-stop').onclick();
  finish({status:'confirmed'});await reading;
  assert.equal(old.stopped,true);assert.doesNotMatch(f.node('qk-msg').textContent,/登入成功/);
  await f.node('qk-qr').onclick();
  f.setHandler(async url=>url.endsWith('/me')?{providers:[]}:{status:'expired'});
  await f.polls[1].task();assert.equal(f.polls[1].stopped,true);
  assert.equal(f.node('qk-qrbox').hidden,true);
});
test('QR refuses an arbitrary external image and does not display debug or returned HTML',async()=>{
  const f=fixture();await flush();
  f.setHandler(async()=>({id:'b'.repeat(32),status:'pending',imgurl:'https://evil.invalid/pixel'}));
  await f.node('bd-qr').onclick();assert.match(f.node('bd-msg').textContent,/未載入/);
  assert.equal(f.node('bd-qrbox').hidden,true);assert.equal(f.polls.length,0);
});
test('empty JSON success is unconfirmed, not a successful credential save',async()=>{
  const f=fixture();await flush();f.node('bd-cookie').value='fictional';
  f.setHandler(async()=>({}));await f.node('bd-cookie-save').onclick();
  assert.match(f.node('bd-msg').textContent,/尚未確認/);assert.equal(f.node('bd-cookie').value,'fictional');
});
test('credential deletion waits for an in-flight QR GET and ignores its stale result',async()=>{
  const f=fixture();await flush();let finish;
  f.setHandler(async(url,options)=>url.endsWith('/start')?{id:'c'.repeat(32),status:'pending'}:
    options.method==='DELETE'?{ok:true}:url.includes('/qr/')?new Promise(r=>finish=r):{providers:[]});
  await f.node('bd-qr').onclick();const reading=f.polls[0].task();
  const clear=f.node('bd-clear').onclick();await flush();
  assert.ok(f.requests.every(r=>r.options.method!=='DELETE'));
  finish({status:'confirmed'});await Promise.all([reading,clear]);
  assert.equal(f.requests.filter(r=>r.options.method==='DELETE').length,1);
  assert.equal(f.node('bd-msg').textContent,'憑證已清除。');
});
