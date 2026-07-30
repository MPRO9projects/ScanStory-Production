# Current Server Audit

## Confirmed From Repo

- No nginx/apache config found.
- No systemd/supervisor/pm2 config found.
- No Dockerfile or compose file found.
- No CI/CD workflow found.
- `gunicorn` dependency exists in `requirements.txt:22`, but no production command exists.
- Flask debug entry exists at `app.py:5768`.

## Critical Unknowns

- Is production using `python app.py`, `flask run`, or `gunicorn`?
- Is there a reverse proxy?
- Are static files served by Flask, nginx, or CDN?
- Are gzip/Brotli enabled?
- Are videos streamed with range requests and cache headers?
- Is MySQL on same 2-core/4 GB server?
- Current CPU, RAM, swap, disk I/O, and load averages are unknown.

## Commands Needed On Server

```bash
ps aux | egrep 'gunicorn|python|flask|nginx|apache'
free -h
df -h
uptime
top -b -n1 | head -50
journalctl -u scanstory --since "24 hours ago"
nginx -T
```

