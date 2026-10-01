# PanBridge HTTPS entry (migration not yet accepted)

Candidate: `https://panbridge.phrixusjhon.workers.dev`.
Fixed upstream: `https://panbridge.tdtc.indevs.in` via the existing Tunnel.
This is a website entry, not a download accelerator or a new storage provider.
The Oracle download workers and Google Drive permissions remain unchanged.

## Safety boundaries

- HTTPS on both legs; no arbitrary upstream, insecure fallback, or certificate bypass.
- No caching of authenticated responses; preserve host-only Secure/HttpOnly cookies.
- Follow no upstream redirect; rewrite same-origin redirects to the entry only.
- Stream bodies, preserve Range/HEAD, and do not retry mutating requests.
- Log only enumerated feature/method, response status, duration, and error category.
  No request URL/query/header/body, file name, IP, OAuth code or credential logging.
- User approved disabling automatic invocation logs while keeping persisted custom
  logs enabled. Traces remain disabled; no external log destination was added.
- Platform-generated exception records may still contain runtime metadata. The
  custom logger's redaction is not a claim that Cloudflare retains no platform data.

## 2026-10-01 observed state

- Dashboard created the dedicated free Worker `panbridge`. Production URL enabled;
  deployed version `f4b9de32` receives 100% according to the dashboard.
- Quick Edit preview reaches the existing application: root responds 302 to login,
  and custom logs show sanitized `panbridge_entry` events.
- Actual production requests from Mac, Oracle, and the in-app browser instead
  return Cloudflare HTTP 500 / error 1101. TLS connects, but application access has
  **not** passed. Preview success must not be treated as production acceptance.
- `worker.mjs` includes additional defensive handling of malformed redirects and
  logger failures. These final defensive edits are tested locally but **not yet
  deployed**: the Quick Edit input did not accept the second edit. The dashboard's
  current version is the first proxy/telemetry implementation, not this final file.
- Attempt to toggle production URL for re-registration was stopped by the action
  approval review. No route toggle occurred. Explicit authorization is required
  before stopping/re-enabling even this new, not-yet-usable entry.
- Google client configuration was inspected using the original project account.
  Its callback remains on the old hostname. `PUBLIC_BASE_URL` on Oracle was not
  changed; no service restart, credential regeneration, new scope or job reset.

## Acceptance / next step

After approval, at most one production URL re-registration attempt. If it remains
broken, do not keep looping on the same platform failure. Preserve the existing
service and report the concrete remaining hostname/platform requirement.

Before switching the application origin, production must pass public health,
unauthenticated API rejection, login cookie/redirect checks, and browser rendering.
Then update the existing Google OAuth callback and Oracle `PUBLIC_BASE_URL` with a
recovery point and checkpoint-safe service restart; keep existing tokens/scopes.
Do not claim Google sign-in migration complete until its new callback is verified.

Local checks: `node --test edge/worker.test.mjs` (14 tests passed). The suite tests
fixed-origin/open-proxy rejection, header spoofing, redirect handling, no caching,
cookie handling, POST forwarding, partial streaming, HEAD, sanitized telemetry,
and failure containment. These are offline logic checks, not live playback proof.
