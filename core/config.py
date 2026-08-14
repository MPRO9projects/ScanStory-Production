import math
import os

from sqlalchemy.engine import make_url


def env_flag(name, default=False):
    """Parse a boolean-ish environment variable. Missing/blank -> default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def runtime_production_mode_flag_active():
    for key in ("SCANSTORY_PRODUCTION", "APP_ENV", "ENV"):
        value = (os.environ.get(key) or "").strip().lower()
        if value in ("1", "true", "yes", "production", "prod"):
            return True
    return (os.environ.get("FLASK_ENV") or "").strip().lower() in ("production", "prod")


def smtp_timeout_seconds():
    raw = (os.environ.get("SMTP_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return 10.0
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise RuntimeError("SMTP_TIMEOUT_SECONDS must be a positive finite number.") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("SMTP_TIMEOUT_SECONDS must be a positive finite number.")
    return timeout


def smtp_port():
    raw = (os.environ.get("SMTP_PORT") or "").strip()
    if not raw:
        raise RuntimeError("SMTP_PORT must be a positive integer.")
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be a positive integer.") from exc
    if port <= 0 or port > 65535:
        raise RuntimeError("SMTP_PORT must be a positive integer.")
    return port


def smtp_security_mode():
    mode = (os.environ.get("SMTP_SECURITY") or "starttls").strip().lower()
    aliases = {
        "tls": "starttls",
        "start_tls": "starttls",
        "starttls": "starttls",
        "ssl": "ssl",
        "smtps": "ssl",
        "none": "none",
        "plain": "none",
    }
    if mode not in aliases:
        raise RuntimeError("SMTP_SECURITY must be one of: starttls, ssl, none.")
    return aliases[mode]


def database_backend_name(database_url):
    try:
        return make_url(database_url).get_backend_name()
    except Exception as exc:
        raise RuntimeError("DATABASE_URL must be a valid SQLAlchemy database URL.") from exc
