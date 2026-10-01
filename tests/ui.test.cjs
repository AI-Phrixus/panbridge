const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const PB = require('../web/static/ui.js');
const flush = () => new Promise(resolve => setImmediate(resolve));
function deferred() {let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function clock() {
  let id=0;const timers=new Map();return {timers,
    setTimeout(fn,ms){timers.set(++id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);}};
}
test('UI utilities escape HTML, whitelist statuses and clamp progress', () => {
  assert.equal(PB.progress(200),100);assert.equal(PB.progress(-10),0);assert.equal(PB.progress('x'),0);
  assert.match(PB.esc('<img onerror="x">'),/&lt;img/);
  assert.equal(PB.badge('" onmouseover="x'),'<span class="badge unknown">未知狀態</span>');
  assert.equal(PB.statusLabel('paused'),'已暫停');
});
test('poll requests never overlap, refreshes coalesce, visibility pauses work', async () => {
  const c=clock(), wait=deferred();let calls=0,live=0,max=0,hidden=false;
  const poll=PB.poll(async()=>{calls++;max=Math.max(max,++live);if(calls===1)await wait.promise;live--;},
    {clock:c,events:{addEventListener(){},removeEventListener(){}},hidden:()=>hidden});
  await flush();const a=poll.refresh(), b=poll.refresh();assert.equal(calls,1);
  wait.resolve();await Promise.all([a,b]);assert.equal(calls,2);assert.equal(max,1);
  hidden=true;await poll.refresh();assert.equal(calls,2);poll.stop();assert.equal(c.timers.size,0);
});
test('failed polling backs off with a bound, then resets after success', async () => {
  const c=clock();let fail=true,errors=0;
  const poll=PB.poll(async()=>{if(fail)throw new Error('offline');},
    {clock:c,events:null,hidden:()=>false,interval:3000,onError:()=>errors++});
  await flush();assert.equal([...c.timers.values()][0].ms,6000);
  for(let i=0;i<10;i++)await poll.refresh();assert.equal([...c.timers.values()][0].ms,30000);
  fail=false;await poll.refresh();assert.equal([...c.timers.values()][0].ms,3000);
  assert.equal(errors,11);poll.stop();
});
test('starting on a hidden page makes no request; returning visible refreshes once', async () => {
  let hidden=true,visibility,calls=0;const c=clock();
  const poll=PB.poll(async()=>{calls++;},{clock:c,hidden:()=>hidden,
    events:{addEventListener(name,fn){visibility=fn;},removeEventListener(){}}});
  await flush();assert.equal(calls,0);hidden=false;visibility();await flush();assert.equal(calls,1);poll.stop();
});
test('duplicate state-changing actions are suppressed and lock releases after failure', async () => {
  const wait=deferred();let calls=0;
  const first=PB.once('test-job',async()=>{calls++;await wait.promise;});
  assert.equal(await PB.once('test-job',async()=>calls++),false);
  wait.resolve();assert.equal(await first,true);assert.equal(calls,1);
  await assert.rejects(PB.once('test-job',async()=>{throw new Error('failure');}));
  assert.equal(await PB.once('test-job',async()=>calls++),true);
});
test('network failure does not replay POST and never exposes raw transport URL', async t => {
  const saved=global.fetch;t.after(()=>{global.fetch=saved;});let calls=0;
  global.fetch=async()=>{calls++;throw new TypeError('https://private.example/?token=secret');};
  await assert.rejects(PB.request('/api/tasks',{method:'POST'}), /操作可能已完成/);
  assert.equal(calls,1);
  await assert.rejects(PB.request('/api/tasks'), e=>!e.message.includes('secret')&&e.message.includes('保留上次資料'));
});
test('HTTP validation errors are readable, unauthorized requests return to login', async t => {
  const saved=global.fetch, location=global.location;t.after(()=>{global.fetch=saved;global.location=location;});
  global.fetch=async()=>({status:422,ok:false,json:async()=>({detail:[{msg:'請選擇文件'}]})});
  await assert.rejects(PB.request('/api/tasks'),/請選擇文件/);
  global.location={};global.fetch=async()=>({status:401,ok:false});
  await assert.rejects(PB.request('/api/tasks'),/登入已失效/);assert.equal(global.location.href,'/login');
});
test('malformed successful responses are not treated as successful mutations',async t=>{
  const saved=global.fetch;t.after(()=>{global.fetch=saved;});
  global.fetch=async()=>({status:200,ok:true,json:async()=>{throw new SyntaxError('HTML response');}});
  await assert.rejects(PB.request('/api/tasks',{method:'POST'}),/操作結果尚未確認/);
  global.fetch=async()=>({status:200,ok:true,json:async()=>null});
  await assert.rejects(PB.request('/api/tasks'),/回應格式異常/);
});
test('browser native timers retain their Window receiver',async()=>{
  const timers=new Map();let id=0;
  const window={document:{hidden:false,addEventListener(){},removeEventListener(){}},
    setTimeout(fn,ms){assert.equal(this,window);timers.set(++id,{fn,ms});return id;},
    clearTimeout(key){assert.equal(this,window);timers.delete(key);}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/../web/static/ui.js','utf8'),{window});
  const poll=window.PB.poll(async()=>{});await flush();
  assert.equal(timers.size,1);assert.equal([...timers.values()][0].ms,3000);
  await poll.refresh();assert.equal(timers.size,1);poll.stop();assert.equal(timers.size,0);
});
test('render cache can be invalidated after checkbox interactions',()=>{
  let writes=0;const element={contains:()=>false,set innerHTML(value){writes++;},querySelectorAll:()=>[]};
  PB.render(element,'unchecked');PB.render(element,'unchecked');assert.equal(writes,1);
  PB.invalidate(element);PB.render(element,'unchecked');assert.equal(writes,2);
});
test('page scripts parse, use bounded polling, and do not create native modal dialogs', () => {
  for(const page of ['index','task']) {
    const html=fs.readFileSync(__dirname+'/../web/templates/'+page+'.html','utf8');
    const source=html.match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace('{{ job_id }}','9001');
    new vm.Script(source);
    assert.doesNotMatch(source,/\b(?:alert|confirm|setInterval)\s*\(/);
    assert.match(source,/PB\.poll/);assert.match(html,/role="status"/);
    assert.match(html,/action-confirm/);assert.match(html,/ui\.js\?v=0\.5\.4-ui1/);
  }
});
