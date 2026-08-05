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

## Verification

- Confirm direct public access to application port is impossible.
- Confirm application sees the proxy-normalized client IP.
- Confirm spoofed client-provided forwarded headers do not bypass auth or rate
  limits.
- Confirm session cookies are secure in HTTPS production.
- Confirm CSP is in the intended report-only or enforce mode for the release.
- Confirm direct app-port requests cannot send their own `X-Forwarded-For` to
  bypass per-IP login, upload, or scanner limits.
