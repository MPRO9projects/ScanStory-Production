# Repository Structure

## Top-Level Folders

| Folder | Purpose | Status |
|---|---|---|
| `templates/` | Jinja HTML templates for user and admin pages | active |
| `static/` | bundled static assets, videos, OpenCV.js/WASM, service worker | active |
| `.codex/` | Codex hook/log files | tooling |
| `codex-skills/` | project-local Codex skill copies | tooling, not app runtime |
| `agent skills/` | imported skill/plugin references | tooling, not app runtime |
| `performance-audit/` | prior performance audit reports | documentation |
| `project-understanding/` | this understanding report set | documentation |

## Top-Level Files

| File | Purpose | Status |
|---|---|---|
| `app.py` | Main Flask app, routes, startup, CV functions, email, payment, admin | active critical |
| `models.py` | SQLAlchemy model definitions | active critical |
| `requirements.txt` | Python dependencies | active |
| `migration_script.py` | standalone migration helper | possibly used manually |
| `fix_limits.py` | standalone subscription/limit helper | possibly used manually |
| `add_simple_admin.py` | standalone admin helper | possibly used manually |
| `folder.py` | folder/tree helper or inventory script | possibly unused |
| `webp.py` | image conversion helper | possibly unused |
| `robots.txt`, `sitemap.xml`, `yandex_*.html` | SEO/verification static files | active/mirrored by routes |

## Meaningful Tree

```text
ScanStory-main/
  app.py
  models.py
  requirements.txt
  migration_script.py
  fix_limits.py
  add_simple_admin.py
  folder.py
  webp.py
  static/
    sw.js
    js/
      opencv.js
      opencv_js.wasm
    videos/
      demo.mp4
      educ.mp4
      art.mp4
      card.mp4
    assets/
      landing/
      logos/
  templates/
    user/
      landing.html
      scanner.html
      user_create_project.html
      success.html
      projects.html
      edit_project.html
      subscribe.html
      register.html
      login.html
      verify_email.html
      ...
    admin/
      base.html
      dashboard.html
      users.html
      projects.html
      payments.html
      plans.html
      settings.html
      ...
  .codex/
    hooks.json
    logs/
  performance-audit/
  codex-skills/
  agent skills/
```

## Runtime-Created Folders

`app.py:217` to `app.py:232` creates:

- `data/images`
- `data/videos`
- `data/features`
- `data/qr_codes`
- `data_admin/images`
- `data_admin/videos`
- `data_admin/features`
- `data_admin/qr_codes`
- `static/uploads`, `static/uploads/logos`, `static/uploads/admin`

These folders are not listed in the root snapshot, but the app expects/creates them at startup.

## Generated Or Possibly Legacy Files

- `performance-audit/` and `project-understanding/` are generated documentation.
- `.codex/logs/` are tooling logs.
- `agent skills/` and `codex-skills/` are agent/tooling material, not part of Scan Story runtime.
- `migration_script.py`, `fix_limits.py`, `add_simple_admin.py`, `webp.py`, and `folder.py` are helper scripts; no route imports them during normal Flask startup.

