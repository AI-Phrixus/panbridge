// A fixed-origin HTTPS entry point, not an open proxy or a file-transfer worker.
// No credentials, user content, or request URLs are logged or stored here.
const PUBLIC_HOST = 'panbridge.phrixusjhon.workers.dev';
const UPSTREAM = 'https://panbridge.tdtc.indevs.in';
const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'];

function feature(path) {
  if (path.startsWith('/api/auth/google/callback')) return 'google_callback';
  if (path === '/login' || path.startsWith('/api/auth/')) return 'authentication';
  if (path === '/settings') return 'settings';
  if (path.startsWith('/tasks/') || path.startsWith('/api/tasks')) return 'tasks';
  if (path.startsWith('/api/stream') || path.startsWith('/api/files')) return 'playback';
  if (path.startsWith('/static/')) return 'static';
  if (path === '/health') return 'health';
  return 'other';
}

async function proxy(request) {
    const incoming = new URL(request.url);
    if (incoming.hostname !== PUBLIC_HOST || incoming.port) {
      return new Response('Unknown entry point', {status: 421});
    }
    if (incoming.protocol !== 'https:') {
      incoming.protocol = 'https:';
      return new Response(null, {status: 308, headers: {Location: incoming.href}});
    }
    if (!METHODS.includes(request.method)) {
      return new Response('Method not allowed', {status: 405});
    }
    const upstream = new URL(UPSTREAM);
    upstream.pathname = incoming.pathname;
    upstream.search = incoming.search;
    const headers = new Headers(request.headers);
    // Do not let caller-controlled proxy headers alter the trusted origin scheme.
    for (const name of [...headers.keys()]) {
      if (name === 'host' || name === 'forwarded' || name.startsWith('x-forwarded-')) {
        headers.delete(name);
      }
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    let response;
    try {
      response = await fetch(upstream.href, {
        method: request.method, headers,
        body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
        // Never follow a provider redirect with the user's PanBridge cookie.
        redirect: 'manual', signal: controller.signal,
        cf: {cacheEverything: false, cacheTtl: 0},
      });
    } catch {
      return new Response('PanBridge temporarily unavailable. Please retry later.', {
        status: 502, headers: {'Cache-Control': 'no-store'},
      });
    } finally {
      clearTimeout(timeout);
    }
    const outgoing = new Headers(response.headers);
    const location = outgoing.get('Location');
    if (location) {
      const target = new URL(location, upstream);
      if (target.origin === UPSTREAM) {
        target.hostname = PUBLIC_HOST;
        outgoing.set('Location', target.href);
      }
    }
    outgoing.set('Cache-Control', 'no-store');
    outgoing.set('Strict-Transport-Security', 'max-age=31536000');
    outgoing.set('Referrer-Policy', 'no-referrer');
    // Forward the stream; do not buffer media or large files in memory.
    return new Response(request.method === 'HEAD' ? null : response.body, {
      status: response.status, statusText: response.statusText, headers: outgoing,
    });
}

export default {
  async fetch(request) {
    const start = Date.now();
    let response;
    let category = 'other';
    try {
      category = feature(new URL(request.url).pathname);
      response = await proxy(request);
    } catch {
      response = new Response('PanBridge entry temporarily unavailable.', {
        status: 502, headers: {'Cache-Control': 'no-store'},
      });
    }
    // Enum-only telemetry: never log URLs, query strings, headers, IPs or bodies.
    // A telemetry failure must not turn a valid application response into a 500.
    try { console.info({
      event: 'panbridge_entry', feature: category,
      method: METHODS.includes(request.method) ? request.method : 'OTHER',
      status: response.status, duration_ms: Math.max(0, Date.now() - start),
      error_code: response.status === 502 ? 'UPSTREAM_UNAVAILABLE'
        : response.status >= 400 ? 'HTTP_ERROR' : 'NONE',
    }); } catch { /* The application remains usable if telemetry is unavailable. */ }
    return response;
  },
};
