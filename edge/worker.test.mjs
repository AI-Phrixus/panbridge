import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.mjs';

const ENTRY = 'https://panbridge.phrixusjhon.workers.dev';
test('fixed-origin entry security and streaming', async (t) => {
  const originalFetch = globalThis.fetch;
  const originalInfo = console.info;
  const events = [];
  console.info = (event) => events.push(event);
  t.after(() => { globalThis.fetch = originalFetch; console.info = originalInfo; });
  let seen;
  globalThis.fetch = async (url, init) => {
    seen = {url, init};
    return new Response('payload', {headers: {'Cache-Control': 'public, max-age=3600'}});
  };
  await t.test('path and query cannot select an arbitrary upstream', async () => {
    await worker.fetch(new Request(ENTRY + '//attacker.example/video?url=https://attacker.example'));
    assert.equal(new URL(seen.url).origin, 'https://panbridge.tdtc.indevs.in');
    assert.equal(new URL(seen.url).pathname, '//attacker.example/video');
    assert.equal(seen.init.redirect, 'manual');
  });
  await t.test('unregistered hosts and unsupported methods fail closed', async () => {
    assert.equal((await worker.fetch(new Request('https://attacker.example/'))).status, 421);
    assert.equal((await worker.fetch(new Request(ENTRY, {method: 'PURGE'}))).status, 405);
  });
  await t.test('plain HTTP redirects to the same HTTPS entry', async () => {
    const result = await worker.fetch(new Request(ENTRY.replace('https:', 'http:') + '/login?q=1'));
    assert.equal(result.status, 308);
    assert.equal(result.headers.get('Location'), ENTRY + '/login?q=1');
  });
  await t.test('Range and authentication are preserved only to the fixed origin', async () => {
    await worker.fetch(new Request(ENTRY + '/api/stream/1', {headers: {
      Range: 'bytes=0-1023', Cookie: 'panbridge_session=test-only',
      'X-Forwarded-Proto': 'http', 'X-Forwarded-Host': 'attacker.example',
      Forwarded: 'proto=http;host=attacker.example',
    }}));
    assert.equal(seen.init.headers.get('Range'), 'bytes=0-1023');
    assert.equal(seen.init.headers.get('Cookie'), 'panbridge_session=test-only');
    assert.equal(seen.init.headers.get('X-Forwarded-Proto'), null);
    assert.equal(seen.init.headers.get('Forwarded'), null);
    assert.equal(seen.init.cf.cacheTtl, 0);
  });
  await t.test('POST body is not consumed or replayed', async () => {
    const req = new Request(ENTRY + '/api/auth/login', {method: 'POST', body: '{"password":"test-only"}'});
    await worker.fetch(req);
    assert.equal(seen.init.body, req.body);
    assert.equal(seen.init.method, 'POST');
  });
  await t.test('cookies remain private and response is never cached', async () => {
    globalThis.fetch = async () => new Response('secret', {headers: {
      'Set-Cookie': 'panbridge_session=test-only; Secure; HttpOnly; SameSite=Lax',
      'Cache-Control': 'public',
    }});
    const result = await worker.fetch(new Request(ENTRY + '/login'));
    assert.equal(result.headers.get('Cache-Control'), 'no-store');
    assert.match(result.headers.get('Set-Cookie'), /Secure; HttpOnly/);
    assert.equal(result.headers.get('Referrer-Policy'), 'no-referrer');
  });
  await t.test('same-origin redirect is rewritten, external redirect is not followed', async () => {
    globalThis.fetch = async () => new Response(null, {status: 302, headers: {Location: '/login'}});
    assert.equal((await worker.fetch(new Request(ENTRY))).headers.get('Location'), ENTRY + '/login');
    globalThis.fetch = async () => new Response(null, {status: 302, headers: {Location: 'https://accounts.google.com/'}});
    assert.equal((await worker.fetch(new Request(ENTRY))).headers.get('Location'), 'https://accounts.google.com/');
  });
  await t.test('partial response preserves status, range and body', async () => {
    globalThis.fetch = async () => new Response('abc', {status: 206, headers: {'Content-Range': 'bytes 0-2/10'}});
    const result = await worker.fetch(new Request(ENTRY + '/video'));
    assert.equal(result.status, 206);
    assert.equal(result.headers.get('Content-Range'), 'bytes 0-2/10');
    assert.equal(await result.text(), 'abc');
  });
  await t.test('HEAD has no response body', async () => {
    globalThis.fetch = async () => new Response(null, {headers: {'Content-Length': '100'}});
    const result = await worker.fetch(new Request(ENTRY, {method: 'HEAD'}));
    assert.equal(result.body, null);
    assert.equal(result.headers.get('Content-Length'), '100');
  });
  await t.test('upstream errors do not reveal URL or credentials', async () => {
    globalThis.fetch = async () => { throw new Error('https://secret.example/?token=private'); };
    const result = await worker.fetch(new Request(ENTRY));
    assert.equal(result.status, 502);
    assert.doesNotMatch(await result.text(), /secret|token|private/);
  });
  await t.test('telemetry contains only enumerated feature, method and response measurements', async () => {
    globalThis.fetch = async () => new Response('ok');
    await worker.fetch(new Request(ENTRY + '/api/auth/google/callback?code=private-code&state=private-state', {
      headers: {Cookie: 'private-cookie', Authorization: 'Bearer private-token', 'CF-Connecting-IP': '192.0.2.1'},
    }));
    assert.equal(events.at(-1).feature, 'google_callback');
    assert.deepEqual(Object.keys(events.at(-1)).sort(), ['duration_ms', 'error_code', 'event', 'feature', 'method', 'status']);
    assert.doesNotMatch(JSON.stringify(events), /private-|192\.0\.2\.1|attacker|https:/);
  });
  await t.test('telemetry failure does not fail a valid application response', async () => {
    const info = console.info;
    console.info = () => { throw new Error('telemetry unavailable'); };
    try {
      assert.equal((await worker.fetch(new Request(ENTRY + '/health'))).status, 200);
    } finally { console.info = info; }
  });
  await t.test('unexpected invalid redirect yields a sanitized failure', async () => {
    globalThis.fetch = async () => new Response(null, {status: 302, headers: {Location: 'http://['}});
    const result = await worker.fetch(new Request(ENTRY));
    assert.equal(result.status, 502);
    assert.doesNotMatch(await result.text(), /http:|\[/);
  });
});
