# Performance Baseline Plan

## Browser

Run for `/`, `/scanner/<project_id>`, `/dashboard`, `/blog`.

```bash
npx lighthouse https://myscanstory.com/ --preset=desktop --output=html --output-path=lh-desktop.html
npx lighthouse https://myscanstory.com/ --preset=mobile --output=html --output-path=lh-mobile.html
```

Capture:

- LCP, INP, CLS, FCP, TTFB, TBT, Speed Index.
- Total transferred bytes.
- Largest requests.
- Main-thread long tasks.
- FPS during landing scroll and scanner use.

## Backend

Use staging first:

```bash
autocannon -c 1 -d 30 http://localhost:5000/
autocannon -c 10 -d 30 http://localhost:5000/
autocannon -c 25 -d 30 http://localhost:5000/
```

Use scanner-specific tests only with sample images and approved limits.

## Server

Record before/after:

- CPU idle/peak.
- RAM idle/peak.
- swap usage.
- disk I/O.
- network throughput.
- app restarts.
- backend p50/p95/p99 latency.

## Limitations

This repo-only audit cannot verify production traffic, production config, database size, network latency, or real Core Web Vitals.

