# Security and Reverse-Proxy Checklist

ScanStory currently uses Flask `ProxyFix(x_for=1, x_proto=1, x_host=1)`.
The application must not be directly exposed to the internet behind spoofable
forwarded headers.

## Requirements

- Only the trusted reverse proxy may connect to the application port.
- The reverse proxy must overwrite forwarded headers, not append untrusted
  client-provided values.
- The application host firewall must reject direct public traffic to the app
  port.
- TLS terminates at the trusted proxy or at a trusted upstream layer.
- `X-Forwarded-Proto` must accurately represent HTTPS.
- `X-Forwarded-For` must be set by the trusted proxy.
- Flask `ProxyFix(x_for=1)` must match exactly one trusted proxy hop.
- Rate limiter uses `request.remote_addr`, after ProxyFix normalization.
- `RATE_LIMIT_REDIS_URL` **must be set in production.** The shared Redis limiter
  now exists (`rate_limit.py`): with the variable set, every Gunicorn worker
  shares one counter budget per key and counters survive a rolling restart.
  With it unset the app falls back to the process-local in-memory limiter,
  which under `gunicorn -w N` silently turns every published limit into
  `N x limit` and loses all counters on restart. That fallback is for local
  development and the test suite only.
- Redis-unavailable policy is **fail closed**: if `RATE_LIMIT_REDIS_URL` is set
  and Redis cannot be reached, limited endpoints answer `429` with a short
  `Retry-After` rather than running unmetered. The same outage already takes
  `/ready` to 503, so the instance is out of rotation regardless.
- `REDIS_SOCKET_TIMEOUT_SECONDS` (default 5) bounds every Redis socket used by
  the limiter, the readiness probe and the queue. Do not unset it: a Redis that
  is unreachable-but-not-refusing (firewall DROP, hung host) otherwise blocks
  the request thread indefinitely instead of failing.

## Sanitized Nginx-Style Requirements

```nginx
# Pseudocode only. Replace placeholders through approved infrastructure config.
location / {
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_pass http://APP_BACKEND;
}
```

Do not use a configuration that forwards untrusted client-supplied
`X-Forwarded-For` values unchanged.

## Large-Upload and Slow-Network Proxy Requirements

ScanStory's resumable upload path (`/api/uploads/sessions/...`) sends one
project as many sequential chunks and then runs `finalize` synchronously inside
a single request, so the proxy — not the app — is what usually breaks a slow
transfer. These are requirements on the infrastructure configuration; this
repository owns no Nginx/Hostinger config and must not be treated as the source
of truth for it.

- **Body size** must be at least the largest permitted single chunk, with
  headroom. A proxy body-size limit below the chunk size fails every upload with
  a 413 the client cannot interpret as its own fault. The non-resumable `/upload`
  multipart route needs a limit above the largest whole image+video pair.
- **Request buffering off** (Nginx `proxy_request_buffering off`) for the upload
  routes, so a slow client streams through instead of first filling proxy disk
  and only then hitting the app.
- **Read timeout** must exceed the worst-case `finalize` — checksum of the
  assembled bytes, image and video validation, project creation, QR generation
  and enqueue — for the largest permitted project. A proxy that times out
  mid-finalize is precisely what leaves an `UploadSession` parked in
  `finalizing`; see the recovery sweep below.
- **Send/keepalive timeouts** must tolerate a genuinely slow mobile uploader
  pausing between chunks. The application's own inactivity window is
  `SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES` (default 1440); a much
  shorter proxy timeout silently undercuts the advertised resume window.
- **No body rewriting** on upload routes (no recompression, no re-serialization):
  chunk offsets and the optional client checksum are computed over exact bytes.
- **Range requests must pass through** to `/video/...` and `/image/...` — the
  scanner and the admin evidence view both rely on them.

### Operational follow-up

- Run `flask cleanup-upload-sessions` on a schedule. It expires abandoned
  sessions AND recovers sessions left in `finalizing` by a crash or proxy
  timeout (default threshold `SCANSTORY_UPLOAD_FINALIZING_STALE_MINUTES=120`).
  Without it, a process death mid-finalize wedges that project behind
  `FINALIZE_IN_PROGRESS` 409s permanently. It is dry-run by default; `--apply`
  persists.
- Alert on any `UploadSession` in `finalizing` older than the threshold. A
  non-zero steady-state count means finalizes are being killed, which points at
  the proxy read timeout or worker timeout above.

## Razorpay Webhook Signature Handling

`POST /webhooks/razorpay` is authenticated by header-based HMAC signature
verification instead of a session/cookie, so its proxy and secret-handling
requirements differ from browser routes:

- The reverse proxy must pass the `X-Razorpay-Signature` request header
  through unmodified — it is the sole authenticity check for this route.
- The reverse proxy/app must not transform, decompress-then-recompress, or
  otherwise mutate the raw request body before it reaches the handler; the
  signature is computed over the exact raw bytes Razorpay sent, and any
  body modification (including a proxy that pretty-prints/re-serializes
  JSON) would break verification for every legitimate delivery.
- `@csrf.exempt` on this specific route is intentional and safe: CSRF
  protection exists to stop a browser from being tricked into submitting an
  authenticated *cookie-bearing* request. This route accepts no session
  cookie and performs no cookie-based auth at all, so there is no CSRF
  attack surface to protect against here — the HMAC-SHA256 signature check
  against `RAZORPAY_WEBHOOK_SECRET` is the actual authenticity control, and
  it is stronger than CSRF tokens for a server-to-server caller.
- `RAZORPAY_WEBHOOK_SECRET` must be stored only in the approved secret
  manager, the same as `RAZORPAY_KEY_SECRET`, `FLASK_SECRET_KEY`, and other
  production secrets. It is a distinct value from `RAZORPAY_KEY_SECRET` with
  no fallback between them — rotating one does not rotate the other, and a
  leaked API key secret does not by itself allow forging webhook deliveries
  (or vice versa).
- The webhook route is deliberately not covered by `request_limiter`
  (per-IP) rate limiting, because Razorpay's own retries can legitimately
  arrive from shared/rotating source IPs. Do not add IP-based rate limiting
  to this route as a "fix" — signature verification plus the database
  unique-index idempotency gate are the intended controls.

## Verification

- Confirm direct public access to application port is impossible.
- Confirm application sees the proxy-normalized client IP.
- Confirm spoofed client-provided forwarded headers do not bypass auth or rate
  limits.
- Confirm session cookies are secure in HTTPS production.
- Confirm CSP is in the intended report-only or enforce mode for the release.
- Confirm direct app-port requests cannot send their own `X-Forwarded-For` to
  bypass per-IP login, upload, or scanner limits.
- Confirm `RATE_LIMIT_REDIS_URL` is set, and that a limit exhausted against one
  Gunicorn worker is also exhausted against the others.
- Confirm the largest permitted upload completes through the proxy, and that a
  deliberately paused chunk sequence resumes rather than 504-ing.
- Confirm the reverse proxy passes `X-Razorpay-Signature` through unmodified
  and does not alter the raw request body on the path to
  `POST /webhooks/razorpay`.
- Confirm `RAZORPAY_WEBHOOK_SECRET` is present only in the secret manager,
  is distinct from `RAZORPAY_KEY_SECRET`, and is never logged.
