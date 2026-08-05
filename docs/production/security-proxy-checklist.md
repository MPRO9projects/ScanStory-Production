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
- Current limiter is process-local and is not shared between Gunicorn workers.
- Redis/shared limiter is required before horizontal scale.
- Because the limiter is process-local memory, counters are not shared across
  Gunicorn workers and are lost on process restart. Treat it as an interim
  single-process protection layer until the Redis/shared limiter is built.

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
- Confirm the reverse proxy passes `X-Razorpay-Signature` through unmodified
  and does not alter the raw request body on the path to
  `POST /webhooks/razorpay`.
- Confirm `RAZORPAY_WEBHOOK_SECRET` is present only in the secret manager,
  is distinct from `RAZORPAY_KEY_SECRET`, and is never logged.
