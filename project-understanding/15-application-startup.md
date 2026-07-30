# Application Startup

## Startup Diagram

```mermaid
flowchart TD
  A[Python imports app.py] --> B[load_dotenv]
  B --> C[Create Flask app]
  C --> D[ProxyFix + logging DEBUG]
  D --> E[Configure secret, DB URI, pool]
  E --> F[db.init_app]
  F --> G[Set MIME for WASM]
  G --> H[Create local folders]
  H --> I[Import-time app_context]
  I --> J[db.create_all]
  J --> K[Create default plans/admin/config if empty]
  K --> L[Define helpers, routes, handlers]
  L --> M{Executed as __main__?}
  M -->|yes| N[db.create_all + bootstrap_database]
  N --> O[app.run debug=True reloader=True]
  M -->|no WSGI| P[Expose app object]
```

## Runs Once Per Process

- env loading, app creation, DB config, folder creation, Razorpay client init, route registration.
- import-time `db.create_all()` and default bootstrap block.
- global caches/objects: `_fast_bf`, `_tls`, logo cache, `load_features` cache.

## Runs Per Request

- before/after request logging.
- route DB lookups and template rendering.
- scanner detection CV work.

## Debug Reloader Consideration

When run as `python app.py`, Flask debug reloader can start app code more than once. This matters because import-time bootstrap and main-time bootstrap both exist.

