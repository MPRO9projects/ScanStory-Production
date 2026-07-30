# ScanStory Project Architecture

## Detected Stack

- App type: Flask monolith with Jinja templates and static assets.
- Backend: Python Flask, Flask-SQLAlchemy, PyMySQL, Razorpay, SMTP, OpenCV, NumPy, Pillow, qrcode, ffmpeg-python.
- Frontend: server-rendered HTML templates using CDN Tailwind, Font Awesome, AOS, Vanilla Tilt, inline CSS and inline JavaScript.
- Database: SQLAlchemy models targeting MySQL-compatible database via `DATABASE_URL`.
- Storage: local filesystem under `data/`, `data_admin/`, `static/uploads/`, and `static/videos/`.
- Scanner: browser camera plus OpenCV.js client load, Flask `/detect_init` and `/detect_track` server endpoints doing OpenCV work.
- Deployment hints: `gunicorn` exists in `requirements.txt`, but no Procfile, Dockerfile, systemd, nginx, or CI/CD config was found in repo.

## Entry Points

- Application object: `app.py:48`.
- DB initialization: `app.py:78`.
- Import-time table/bootstrap work: `app.py:239`.
- Main debug entry point: `app.py:5758` to `app.py:5768`.
- Production command is not defined in the repo. Likely intended: `gunicorn app:app`.

## Current Runtime Flow

```text
Browser
-> Flask app directly or reverse proxy not present in repo
-> Jinja-rendered pages
-> local static files and CDN scripts
-> Flask API routes
-> SQLAlchemy/MySQL via DATABASE_URL
-> local media/features/QR directories
-> external Google reCAPTCHA, SMTP, Razorpay
```

## Notes

No confirmed CDN, object storage, reverse proxy, process manager, log rotation, health check, or monitoring config exists in this repository.

