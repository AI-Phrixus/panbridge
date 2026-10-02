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
    assert.match(html,/action-confirm/);assert.match(html,/ui\.js\?v=0\.5\.5-ui2/);
  }
});

test('new project rows start at one without changing IDs, source records or progress',()=>{
  const jobs=[{id:18,destination:'google',status:'done',progress:100},
    {id:13,destination:'onedrive',status:'failed'},
    {id:12,destination:'pcloud',status:'failed'},
    {id:17,destination:'google',status:'deleted'},
    {id:16,destination:'google',status:'downloading',progress:53.89},
    {id:18,destination:'google',status:'done',progress:100}];
  const before=JSON.stringify(jobs), rows=PB.taskRows(jobs,16);
  assert.deepEqual(rows.map(j=>[j.id,j.displayNumber,j.progress]),[[16,1,53.89],[18,2,100]]);
  assert.equal(JSON.stringify(jobs),before);
  assert.notEqual(rows[0],jobs[4]);
  assert.deepEqual(PB.taskRows([...jobs,{id:19,destination:'pcloud',status:'queued'}],16).map(j=>j.id),[16,18,19]);
});
test('retired targets, deleted tasks and invalid IDs never enter the new view',()=>{
  assert.deepEqual(PB.taskRows([{id:21,destination:'onedrive'},{id:'22'},null,{id:23,status:'deleted'}],16),[]);
  assert.throws(()=>PB.taskRows({},16),/格式異常/);
  assert.throws(()=>PB.taskRows([],0),/格式異常/);
  assert.throws(()=>PB.taskRows([],1.5),/格式異常/);
});
test('project configuration is optional on fresh installs but invalid configuration fails closed',async t=>{
  const saved=global.fetch;t.after(()=>{global.fetch=saved;});
  global.fetch=async()=>({status:404,ok:false});
  assert.deepEqual(await PB.loadTaskView(),{firstJobId:1});
  global.fetch=async()=>({status:200,ok:true,json:async()=>({schema:1,first_job_id:16})});
  assert.deepEqual(await PB.loadTaskView(),{firstJobId:16});
  for(const first_job_id of [0,-1,1.5,'16',null,Number.MAX_SAFE_INTEGER+1]){
    global.fetch=async()=>({status:200,ok:true,json:async()=>({schema:1,first_job_id})});
    await assert.rejects(PB.loadTaskView(),/設定異常/);
  }
  global.fetch=async()=>({status:500,ok:false,json:async()=>({detail:'offline'})});
  await assert.rejects(PB.loadTaskView(),/offline/);
});
test('optional configuration 404 handling cannot turn a failed mutation into success',async t=>{
  const saved=global.fetch;t.after(()=>{global.fetch=saved;});
  global.fetch=async()=>({status:404,ok:false,json:async()=>({detail:'not found'})});
  await assert.rejects(PB.request('/api/tasks/16',{method:'DELETE',allowNotFound:true}),/not found/);
});

function pageFixture(page,jobId=16){
  const nodes=new Map(), polls=[], requests=[], notices=[];
  const node=id=>{
    if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,disabled:false,innerHTML:'',textContent:'',
      querySelectorAll:()=>[],contains:()=>false});
    return nodes.get(id);
  };
  const jobs=[{id:13,destination:'onedrive',status:'failed'},
    {id:18,destination:'google',status:'done',progress:100,title:'second'},
    {id:16,destination:'google',status:'downloading',progress:53.89,title:'first',source_type:'quark'}];
  const context={document:{querySelector:s=>node(s.replace(/^#/,'')),getElementById:node,querySelectorAll:()=>[]},
    location:{origin:'https://example.invalid'},URL,Set,Map,console};
  context.window=context;
  context.PB={...PB,loadTaskView:async()=>({firstJobId:16}),
    request:async(url,init={})=>{
      requests.push({url,method:init.method||'GET'});
      if(url==='/api/tasks')return {jobs};
      if(url==='/api/tasks/'+jobId)return {job:jobs.find(j=>j.id===jobId),files:[]};
      return {ok:true};
    },render:(el,html)=>{el.innerHTML=html;},notice:text=>notices.push(text),confirmAction:async()=>true,
    poll:task=>{polls.push(task);return {refresh:task,stop(){}};}};
  const html=fs.readFileSync(__dirname+'/../web/templates/'+page+'.html','utf8');
  const source=html.match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace('{{ job_id }}',String(jobId));
  vm.runInNewContext(source,context);
  return {context,polls,requests,notices,node};
}
test('display task one links to and controls actual task 16, never task one',async()=>{
  const fixture=pageFixture('index');await fixture.polls[0]();
  const html=fixture.node('jobs').innerHTML;
  assert.match(html,/href="\/tasks\/16">#1<\/a>/);
  assert.match(html,/href="\/tasks\/18">#2<\/a>/);
  assert.doesNotMatch(html,/href="\/tasks\/13"/);
  await fixture.context.control(16,'pause');
  assert.ok(fixture.requests.some(r=>r.url==='/api/tasks/16/pause'&&r.method==='POST'));
  assert.ok(fixture.requests.every(r=>r.url!=='/api/tasks/1/pause'));
  assert.match(fixture.notices[0],/任務 #1：已暫停/);
});
test('task detail uses the same new sequence while keeping actual request IDs',async()=>{
  const fixture=pageFixture('task');await fixture.polls[0]();
  assert.equal(fixture.node('task-heading').textContent,'任務 #1');
  assert.equal(fixture.node('task-title').textContent,'first');
  await fixture.context.controlJob('pause');
  assert.ok(fixture.requests.some(r=>r.url==='/api/tasks/16/pause'&&r.method==='POST'));
  assert.ok(fixture.requests.every(r=>r.url!=='/api/tasks/1'));
});
test('opening a historical task never loads its files or exposes migration controls',async()=>{
  const fixture=pageFixture('task',13);await fixture.polls[0]();
  assert.equal(fixture.node('task-heading').textContent,'舊項目記錄');
  assert.equal(fixture.node('task-files').hidden,true);
  assert.equal(fixture.node('jobActions').innerHTML,'');
  assert.ok(fixture.requests.every(r=>r.url!=='/api/tasks/13'));
});
