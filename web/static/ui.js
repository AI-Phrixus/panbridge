/* Shared, dependency-free UI utilities. Never retries a state-changing request. */
(function (root) {
  'use strict';
  const labels = {queued:'等待搬運',resolving:'讀取文件',saving:'準備來源',
    awaiting_selection:'等待選檔',downloading:'下載中',uploading:'上傳中',
    paused:'已暫停',done:'已完成',failed:'需要處理',cancelled:'已取消',skipped:'未選取'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const progress = n => Math.max(0, Math.min(100, Number(n) || 0));
  const statusLabel = s => labels[s] || '未知狀態';
  const badge = s => '<span class="badge '+(Object.hasOwn(labels,s)?s:'unknown')+'">'+statusLabel(s)+'</span>';
  function errorText(detail, fallback) {
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (Array.isArray(detail)) return detail.map(x => typeof x.msg === 'string' ? x.msg : '').filter(Boolean).join('；') || fallback;
    return fallback;
  }
  async function request(url, options = {}) {
    const {timeoutMs = 15000, ...init} = options;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await root.fetch(url, {...init, signal:controller.signal, cache:'no-store'});
      if (response.status === 401) {
        if (root.location) root.location.href = '/login';
        throw new Error('登入已失效，請重新登入。');
      }
      let data;
      try { data = await response.json(); }
      catch { throw new Error(response.ok
        ? '回應格式異常，操作結果尚未確認；請刷新確認，不要連續提交。'
        : '操作失敗（HTTP '+response.status+'），請稍後再試。'); }
      if (data === null || typeof data !== 'object') throw new Error('回應格式異常，請刷新確認。');
      if (!response.ok) throw new Error(errorText(data.detail, '操作失敗（HTTP '+response.status+'），請稍後再試。'));
      return data;
    } catch (error) {
      if (controller.signal.aborted || error.name === 'AbortError' || error instanceof TypeError) {
        throw new Error(init.method && init.method !== 'GET'
          ? '連線中斷或逾時，操作可能已完成；請先刷新確認，不要連續提交。'
          : '目前無法連線，保留上次資料；連線恢復後會自動更新。');
      }
      throw error;
    } finally { clearTimeout(timer); }
  }
  function poll(task, options = {}) {
    const interval = options.interval || 3000;
    const hidden = options.hidden || (() => !!root.document?.hidden);
    // Native browser timers require their Window receiver, unlike Node timers.
    const clock = options.clock || {setTimeout:(fn,ms)=>root.setTimeout(fn,ms),clearTimeout:id=>root.clearTimeout(id)};
    const events = options.events || root.document;
    let timer, flight = null, stopped = false, requested = false, failures = 0;
    function clear() { if (timer != null) clock.clearTimeout(timer); timer = null; }
    function schedule() {
      if (stopped || hidden()) return;
      timer = clock.setTimeout(run, Math.min(options.maxInterval || 30000, interval * 2 ** Math.min(failures, 4)));
    }
    function run() {
      if (stopped || hidden()) return Promise.resolve();
      if (flight) { requested = true; return flight; }
      clear();
      flight = Promise.resolve().then(async () => {
        do {
          requested = false;
          try { await task(); failures = 0; options.onSuccess?.(); }
          catch (error) { failures++; options.onError?.(error); }
        } while (requested && !stopped && !hidden());
      }).finally(() => { flight = null; schedule(); });
      return flight;
    }
    function visibility() { clear(); if (!hidden()) run(); }
    events?.addEventListener('visibilitychange', visibility);
    run();
    return {refresh:run, stop() {stopped=true; clear(); events?.removeEventListener('visibilitychange',visibility);}};
  }
  const pending = new Set();
  async function once(key, action) {
    if (pending.has(key)) return false;
    pending.add(key);
    try { await action(); return true; } finally { pending.delete(key); }
  }
  function notice(text, kind = 'info') {
    const element = root.document?.getElementById('notice');
    if (!element) return;
    element.textContent = text;
    element.className = 'notice '+kind;
    element.hidden = !text;
    // A file action can be far down the table; keep its outcome discoverable.
    if (text) element.scrollIntoView?.({block:'center',behavior:'auto'});
  }
  const rendered = new WeakMap();
  function invalidate(element) { rendered.delete(element); }
  function render(element, html) {
    if (rendered.get(element) === html) return;
    const focused = root.document?.activeElement;
    const inside = element.contains(focused);
    // Keep an in-progress checkbox/input interaction stable during polling.
    if (inside && ['INPUT','SELECT','TEXTAREA'].includes(focused.tagName)) return;
    const key = inside && (focused.getAttribute('onclick') || focused.getAttribute('href'));
    element.innerHTML = html; rendered.set(element, html);
    if (key) {
      const replacement = [...element.querySelectorAll('button,a')].find(el =>
        (el.getAttribute('onclick') || el.getAttribute('href')) === key);
      replacement?.focus();
    }
  }
  let resolveConfirmation;
  function confirmAction(message, label = '確認') {
    if (resolveConfirmation) return Promise.resolve(false);
    const box = root.document.getElementById('action-confirm');
    const previous = root.document.activeElement;
    root.document.getElementById('confirm-message').textContent = message;
    const yes = root.document.getElementById('confirm-yes');
    yes.textContent = label; box.hidden = false; box.focus();
    return new Promise(resolve => {
      function finish(answer) {
        box.hidden = true; resolveConfirmation = null;
        if (previous?.isConnected) previous.focus();
        yes.onclick = null; root.document.getElementById('confirm-no').onclick = null;
        box.onkeydown = null; resolve(answer);
      }
      resolveConfirmation = finish;
      yes.onclick = () => finish(true);
      root.document.getElementById('confirm-no').onclick = () => finish(false);
      box.onkeydown = e => {if(e.key === 'Escape') finish(false);};
    });
  }
  const api = {esc,progress,statusLabel,badge,request,poll,once,notice,render,invalidate,confirmAction};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PB = api;
})(typeof window !== 'undefined' ? window : globalThis);
