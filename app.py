import os
import sys
import time
import shutil
import mimetypes
import threading
import json
import uuid
import razorpay
from functools import lru_cache, wraps
from datetime import datetime as dt, timedelta
from flask import (
    Flask, request, redirect, url_for, session,
    jsonify, flash, send_from_directory, render_template, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError
from flask_migrate import Migrate
from dotenv import load_dotenv
import cv2
import numpy as np
import qrcode
from qrcode.image.styledpil import StyledPilImage
from PIL import Image, ImageDraw, ImageFile, ImageFont, ImageOps
import ffmpeg
import secrets
import hashlib
import traceback
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from concurrent.futures import ThreadPoolExecutor
import logging
import requests
import click

from sqlalchemy import or_, desc, func, and_, case, text, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

# ✅ Import models
from models import (
    db, User, Admin, SubscriptionPlan, TrialDetails, OTPCode,
    Project, ProjectPair, PaymentOrder, ScanLog, SystemConfig,
    UserLoginActivity, AdminActivity, CapacityConfig, PaymentReservation
)
from upload_validation import UploadValidationError, validate_image, validate_video, _safe_remove
from rate_limit import limiter as request_limiter
request_limiter.clear()

from flask import render_template, abort


# --------------------------------------------------------------------------------------------
# Flask / DB config WITH POOLING
# --------------------------------------------------------------------------------------------
load_dotenv()
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SCANSTORY_TESTING = os.environ.get("SCANSTORY_TESTING") == "1"
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

logging.basicConfig(level=logging.DEBUG)
app.logger.setLevel(logging.DEBUG)

# CSRF protection is enabled globally (see P0B). Narrow, justified exemptions
# are applied per-route below via @csrf.exempt - see the route inventory in
# the P0B report for why each exemption exists.
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_CHECK_DEFAULT'] = True
app.config['WTF_CSRF_HEADERS'] = ["X-CSRFToken", "X-CSRF-Token"]


def _env_flag(name, default=False):
    """Parse a boolean-ish environment variable. Missing/blank -> default."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _validate_required_runtime_config():
    """Fail fast on missing required runtime-security configuration.

    Centralized so future required settings can be added here. Does not
    validate payment credentials or other unrelated production settings.
    """
    missing = []
    if not os.environ.get("FLASK_SECRET_KEY"):
        missing.append("FLASK_SECRET_KEY")
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Set them before starting the app (see .env.example)."
        )


_validate_required_runtime_config()

# Mandatory Flask secret key. No insecure fallback: an unset key fails
# startup loudly instead of silently signing sessions with a public,
# guessable value. Automated tests set their own isolated test secret.
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

# Debug/reloader are OFF by default and require an explicit opt-in via env.
# Production deployments must run behind a real WSGI server (gunicorn,
# waitress, etc.) and must never set FLASK_DEBUG=1.
FLASK_DEBUG_ENABLED = False if SCANSTORY_TESTING else _env_flag("FLASK_DEBUG", default=False)

# Session cookie baseline. SECURE defaults to False so local HTTP
# development keeps working; set SESSION_COOKIE_SECURE=1 in production
# (HTTPS) environments.
SESSION_COOKIE_SECURE_ENABLED = _env_flag("SESSION_COOKIE_SECURE", default=False)

# ✅ ADD DATABASE CONFIGURATION HERE
database_uri = os.environ.get("TEST_DATABASE_URL") if SCANSTORY_TESTING else os.environ.get("DATABASE_URL", "")
engine_options = {}
if database_uri and not database_uri.startswith("sqlite"):
    engine_options = {
        'pool_size': 10,
        'max_overflow': 20,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'pool_timeout': 30,
        'connect_args': {
            'connect_timeout': 10,
            'charset': 'utf8mb4'
        }
    }

app.config.update(
    TESTING=SCANSTORY_TESTING,
    DEBUG=FLASK_DEBUG_ENABLED,
    SQLALCHEMY_DATABASE_URI=database_uri,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS=engine_options,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE_ENABLED,
)

csrf = CSRFProtect(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    """Safe CSRF failure response - never leaks the internal reason string.

    JSON for API/detection/AJAX-style paths, a plain HTML page otherwise.
    Detailed reason is logged server-side only.
    """
    app.logger.warning(f"CSRF validation failed on {request.path}: {error.description}")
    # Routes whose own JS always expects a JSON response, even though they
    # aren't under /api or /detect and don't set an Accept/X-Requested-With
    # header (fetch() doesn't add either by default).
    _JSON_ONLY_PATHS = ("/create-razorpay-order", "/verify-payment", "/send-contact-email")
    wants_json = (
        request.path.startswith('/api') or request.path.startswith('/detect')
        or request.path in _JSON_ONLY_PATHS
        or request.accept_mimetypes.best == 'application/json'
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    )
    if wants_json:
        return jsonify({
            "error": True,
            "reason": "Your session has expired or the request could not be verified. Please refresh the page and try again.",
        }), 400
    return (
        "<h1>Request could not be verified</h1>"
        "<p>Your session may have expired. Please go back, refresh the page, and try again.</p>",
        400,
    )

# ✅ Initialize SQLAlchemy ONLY ONCE
db.init_app(app)

# Flask-Migrate/Alembic wiring (Phase 1 migration foundation). This only
# registers the `flask db ...` CLI commands against the existing db/app
# objects - it does not run anything at import time and does not change
# db.init_app's behavior or the bootstrap block below. ensure_marker_schema()
# and ensure_otp_security_schema() keep running exactly as before; Alembic
# does not own schema state yet in this phase (see migrations/README.md).
migrate = Migrate(
    app, db, directory=os.path.join(BASE_DIR, "migrations"),
    # Declared default only; migrations/env.py re-derives this per-connection
    # from the actual dialect name at run time (mirrors
    # _supports_row_level_locking()'s dialect-name gating below).
    render_as_batch=False,
)

from experience_creator import experience_creator_bp

app.register_blueprint(experience_creator_bp)

# Ensure correct MIME type for wasm
mimetypes.add_type("application/wasm", ".wasm")
ImageFile.LOAD_TRUNCATED_IMAGES = True

RECAPTCHA_SITE_KEY = os.environ.get("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET_KEY = os.environ.get("RECAPTCHA_SECRET_KEY", "")
RECAPTCHA_MIN_SCORE = float(os.environ.get("RECAPTCHA_MIN_SCORE", "0.5"))

RATE_LIMITS = {
    "scanner_init": (45, 60),
    "scanner_track": (240, 60),
    "scanner_session_end": (90, 60),
    "upload": (8, 3600),
    "login_ip": (80, 900),
    "register_ip": (30, 3600),
    "forgot_password_ip": (30, 3600),
    "resend_otp_ip": (20, 3600),
}


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.remote_addr or "0.0.0.0"


def _rate_limit_key(scope, *parts):
    clean = [scope, _client_ip()]
    clean.extend(str(part or "-")[:120] for part in parts)
    return ":".join(clean)


def _check_rate_limit(scope, key):
    limit, window = RATE_LIMITS[scope]
    return request_limiter.check(key, limit, window)


def _scanner_rate_limited_response(retry_after):
    response = jsonify({
        "error": True,
        "code": "RATE_LIMITED",
        "reason": "Too many scanner requests. Please wait briefly and try again.",
        "retry_after_seconds": retry_after,
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def _apply_public_immutable_cache(response):
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


def _apply_short_public_cache(response):
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def _log_scanner_latency(event, start_time, **fields):
    safe = {
        "event": event,
        "duration_ms": round((time.time() - start_time) * 1000, 2),
    }
    for key, value in fields.items():
        if key in {"project_id", "outcome", "stage", "pair_id", "scan_session_id"}:
            safe[key] = value
    app.logger.info("scanner_latency", extra={"scanner_latency": safe})

@app.context_processor
def inject_recaptcha_key():
    return {
        "RECAPTCHA_SITE_KEY": RECAPTCHA_SITE_KEY
    }


def verify_recaptcha_v3(expected_action):
    # Bypass when keys not configured (local dev or unconfigured deployment)
    if not RECAPTCHA_SITE_KEY or not RECAPTCHA_SECRET_KEY:
        app.logger.warning(f"reCAPTCHA keys not configured — bypassing verification for action={expected_action}")
        return True, "OK"

    token = request.form.get("g-recaptcha-response", "").strip()

    if not token:
        return False, "Security verification failed. Please try again."

    try:
        response = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={
                "secret": RECAPTCHA_SECRET_KEY,
                "response": token,
                "remoteip": request.headers.get("X-Forwarded-For", request.remote_addr),
            },
            timeout=5
        )

        result = response.json()

        if not result.get("success"):
            app.logger.warning(f"reCAPTCHA failed: {result}")
            return False, "Security verification failed. Please try again."

        score = float(result.get("score", 0))
        action = result.get("action")
        hostname = result.get("hostname")

        if action != expected_action:
            app.logger.warning(f"reCAPTCHA action mismatch: expected={expected_action}, got={action}")
            return False, "Invalid security action."

        if score < RECAPTCHA_MIN_SCORE:
            app.logger.warning(f"Low reCAPTCHA score: {score}, action={action}, hostname={hostname}")
            return False, "Security verification failed. Please try again."

        # localhost/127.0.0.1 allowed for local dev; production hosts for deployed site
        allowed_hosts = {"myscanstory.com", "www.myscanstory.com", "localhost", "127.0.0.1"}
        if hostname and hostname not in allowed_hosts:
            app.logger.warning(f"Invalid reCAPTCHA hostname: {hostname}")
            return False, "Invalid security hostname."

        return True, "OK"

    except Exception as e:
        app.logger.error(f"reCAPTCHA verification error: {e}")
        return False, "Security verification failed. Please try again."
    
INR_PER_USD = float(os.environ.get("INR_PER_USD", "95.11"))

@app.template_filter("inr_to_usd")
def inr_to_usd(amount):
    try:
        amount = float(amount or 0)
        return round(amount / INR_PER_USD, 2)
    except Exception:
        return 0





@app.before_request
def log_incoming_request():
    if request.path.startswith('/static'):
        return
    print(f"➡️ Incoming request: {request.method} {request.path} from {request.remote_addr}")
    sys.stdout.flush()


@app.after_request
def log_outgoing_response(response):
    if request.path.startswith('/static'):
        return response
    print(f"⬅️ Response: {request.method} {request.path} {response.status}")
    sys.stdout.flush()
    return response


# HSTS only ever applies when explicitly enabled AND the request is
# genuinely HTTPS (request.is_secure honors X-Forwarded-Proto via the
# ProxyFix middleware above) - never sent over ordinary local HTTP.
HSTS_ENABLED = _env_flag("SECURITY_HSTS_ENABLED", default=False)

# CSP staged rollout: the policy below has NOT been manually verified in a
# real browser against the scanner/OpenCV-WASM/Razorpay/reCAPTCHA/Bootstrap
# /Chart.js/fonts/inline-script surface, so it defaults to report-only
# (observe violations, block nothing) rather than enforcing mode.
#   SECURITY_CSP_ENABLED=0        -> send neither CSP header at all
#   SECURITY_CSP_ENABLED=1, ENFORCE=0 (default) -> Content-Security-Policy-Report-Only
#   SECURITY_CSP_ENABLED=1, ENFORCE=1           -> Content-Security-Policy (enforcing)
# Only flip SECURITY_CSP_ENFORCE=1 after browser + real-device QA confirms
# the policy below doesn't block anything real.
CSP_ENABLED = _env_flag("SECURITY_CSP_ENABLED", default=True)
CSP_ENFORCE = _env_flag("SECURITY_CSP_ENFORCE", default=False)

# Every external origin actually referenced by templates/static assets
# (Tailwind CDN, AOS, Font Awesome, Bootstrap/Chart.js CDN, Razorpay
# Checkout, reCAPTCHA, Google Fonts). OpenCV.js/its .wasm are self-hosted
# under /static, covered by 'self'. No wildcards.
_CSP_DIRECTIVES = {
    "default-src": ["'self'"],
    "script-src": [
        "'self'", "'unsafe-inline'", "'wasm-unsafe-eval'",
        "https://cdn.tailwindcss.com", "https://unpkg.com",
        "https://cdnjs.cloudflare.com", "https://cdn.jsdelivr.net",
        "https://checkout.razorpay.com",
        "https://www.google.com", "https://www.gstatic.com",
    ],
    "style-src": [
        "'self'", "'unsafe-inline'",
        "https://cdn.tailwindcss.com", "https://unpkg.com",
        "https://cdnjs.cloudflare.com", "https://cdn.jsdelivr.net",
        "https://fonts.googleapis.com",
    ],
    "font-src": ["'self'", "data:", "https://cdnjs.cloudflare.com", "https://fonts.gstatic.com"],
    "img-src": ["'self'", "data:", "blob:", "https://images.pexels.com"],
    "media-src": ["'self'", "blob:"],
    "connect-src": ["'self'", "https://api.razorpay.com", "https://lumberjack.razorpay.com", "https://www.google.com"],
    "frame-src": ["https://api.razorpay.com", "https://checkout.razorpay.com", "https://www.google.com"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'self'"],
}
CONTENT_SECURITY_POLICY = "; ".join(f"{directive} {' '.join(sources)}" for directive, sources in _CSP_DIRECTIVES.items())


def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Scanner needs its own camera; every other page gets no camera at all.
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"

    if CSP_ENABLED:
        # Never send both - enforcing mode wins when explicitly opted into,
        # otherwise the same policy is sent report-only.
        if CSP_ENFORCE:
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        else:
            response.headers["Content-Security-Policy-Report-Only"] = CONTENT_SECURITY_POLICY

    if HSTS_ENABLED and request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    if request.path == "/static/sw.js":
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache"

    if request.path in ("/static/js/opencv.js", "/static/js/opencv_js.wasm"):
        _apply_public_immutable_cache(response)

    return response


app.after_request(add_security_headers)


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


def _readiness_checks():
    db.session.execute(text("SELECT 1"))
    return {"database": "ok"}


@app.route("/ready", methods=["GET"])
def ready():
    try:
        checks = _readiness_checks()
        return jsonify({"status": "ready", "checks": checks}), 200
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning("readiness_check_failed", exc_info=True)
        return jsonify({"status": "not_ready", "checks": {"database": "unavailable"}}), 503


# --------------------------------------------------------------------------------------------
# Razorpay Configuration
# --------------------------------------------------------------------------------------------
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# Initialize Razorpay client with proper error handling
try:
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET:
        razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        print("✅ Razorpay client initialized successfully")
    else:
        razorpay_client = None
        print("⚠️ Razorpay keys not configured. Payments will not work.")
except Exception as e:
    razorpay_client = None
    print(f"❌ Razorpay initialization failed: {e}")

# Pre-existing latent bug fixed in passing (discovered while implementing
# Phase 2 area 5's order-creation-failure test): some installed
# razorpay-python versions don't define razorpay.errors.AuthenticationError
# at all. `except razorpay.errors.AuthenticationError:` then raises
# AttributeError the moment Python tries to evaluate that except clause -
# not "doesn't match", but a hard crash - for ANY exception raised out of
# razorpay_client.order.create(), masking the real error. Fall back to a
# class that can never actually be raised, so the except clause is just
# skipped (falling through to the generic `except Exception` handler)
# instead of crashing, on versions where the real class is missing.
_RAZORPAY_AUTH_ERROR = getattr(razorpay.errors, "AuthenticationError", type("_RazorpayAuthErrorUnavailable", (Exception,), {}))

# --------------------------------------------------------------------------------------------
# Storage paths
# --------------------------------------------------------------------------------------------
DATA_DIR = os.environ.get("SCANSTORY_DATA_DIR", "data")
IMAGES_DIR = os.path.join(DATA_DIR, "images")
VIDEOS_DIR = os.path.join(DATA_DIR, "videos")
FEATURES_DIR = os.path.join(DATA_DIR, "features")
QR_DIR = os.path.join(DATA_DIR, "qr_codes")
STATIC_UPLOADS_DIR = os.environ.get("SCANSTORY_STATIC_UPLOADS_DIR", os.path.join("static", "uploads"))
STATIC_JS_DIR = os.path.join("static", "js")
LOGOS_DIR = os.path.join(STATIC_UPLOADS_DIR, "logos")
ADMIN_UPLOADS_DIR = os.path.join(STATIC_UPLOADS_DIR, "admin")
# Ephemeral staging area: uploads are validated here before being moved to
# IMAGES_DIR/VIDEOS_DIR (see upload_validation.py) - never a permanent,
# trusted-media location.
TMP_UPLOADS_DIR = os.path.join(DATA_DIR, "tmp_uploads")

for d in (DATA_DIR, IMAGES_DIR, VIDEOS_DIR, FEATURES_DIR, QR_DIR, STATIC_UPLOADS_DIR, STATIC_JS_DIR, LOGOS_DIR, ADMIN_UPLOADS_DIR, TMP_UPLOADS_DIR):
    os.makedirs(d, exist_ok=True)

ADMIN_DATA_DIR = os.environ.get("SCANSTORY_ADMIN_DATA_DIR", os.path.join(BASE_DIR, "data_admin"))
ADMIN_IMAGES_DIR = os.path.join(ADMIN_DATA_DIR, "images")
ADMIN_VIDEOS_DIR = os.path.join(ADMIN_DATA_DIR, "videos")
ADMIN_FEATURES_DIR = os.path.join(ADMIN_DATA_DIR, "features")
ADMIN_QR_DIR = os.path.join(ADMIN_DATA_DIR, "qr_codes")
for d in [ADMIN_DATA_DIR, ADMIN_IMAGES_DIR, ADMIN_VIDEOS_DIR, ADMIN_FEATURES_DIR, ADMIN_QR_DIR]:
    os.makedirs(d, exist_ok=True)

PROJECT_PAIR_MARKER_COLUMNS = {
    "marker_mode": "VARCHAR(20) DEFAULT 'full_image'",
    "marker_crop_x": "FLOAT DEFAULT 0.0",
    "marker_crop_y": "FLOAT DEFAULT 0.0",
    "marker_crop_width": "FLOAT DEFAULT 1.0",
    "marker_crop_height": "FLOAT DEFAULT 1.0",
    "marker_rotation": "INTEGER DEFAULT 0",
    "marker_original_width": "INTEGER",
    "marker_original_height": "INTEGER",
    "marker_processed_width": "INTEGER",
    "marker_processed_height": "INTEGER",
    "marker_source_size_bytes": "INTEGER",
    "marker_processed_size_bytes": "INTEGER",
    "marker_display_orientation": "VARCHAR(20)",
}


def ensure_marker_schema():
    inspector = inspect(db.engine)
    if "project_pairs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("project_pairs")}
    for column_name, ddl in PROJECT_PAIR_MARKER_COLUMNS.items():
        if column_name not in existing:
            db.session.execute(text(f"ALTER TABLE project_pairs ADD COLUMN {column_name} {ddl}"))
    db.session.commit()


SCAN_LOG_SESSION_UNIQUE_INDEX_NAME = "uq_scan_logs_user_session"


def _scan_log_session_uniqueness_exists(inspector):
    indexes = inspector.get_indexes("scan_logs")
    unique_constraints = inspector.get_unique_constraints("scan_logs")
    return any(
        item.get("name") == SCAN_LOG_SESSION_UNIQUE_INDEX_NAME
        for item in [*indexes, *unique_constraints]
    )


def _scan_log_duplicate_report_for_rows(rows):
    row_count = len(rows)
    counted_rows = [row for row in rows if row.counted]
    successful_rows = [row for row in rows if row.is_successful]
    project_ids = {row.project_id for row in rows}
    pair_ids = {row.pair_id for row in rows}
    scan_types = {row.scan_type for row in rows}
    conflicting = len(project_ids) > 1 or len(pair_ids) > 1 or len(scan_types) > 1
    canonical = sorted(
        rows,
        key=lambda row: (
            not bool(row.counted),
            not bool(row.is_successful),
            row.pair_id is None,
            row.id,
        ),
    )[0]
    return {
        "user_id": rows[0].user_id,
        "scan_session_id": rows[0].scan_session_id,
        "count": row_count,
        "affected_rows": row_count,
        "redundant_rows": row_count - 1,
        "counted_rows": len(counted_rows),
        "multiple_counted": len(counted_rows) > 1,
        "successful_rows": len(successful_rows),
        "conflicting_scan_data": conflicting,
        "canonical_id": canonical.id,
        "redundant_ids": [row.id for row in rows if row.id != canonical.id],
        "project_ids": sorted(project_ids),
        "pair_ids": sorted(pair_id for pair_id in pair_ids if pair_id is not None),
        "scan_types": sorted(scan_type for scan_type in scan_types if scan_type is not None),
    }


def _scan_log_duplicate_reports():
    duplicate_keys = (
        db.session.query(
            ScanLog.user_id,
            ScanLog.scan_session_id,
            func.count(ScanLog.id).label("count"),
        )
        .group_by(ScanLog.user_id, ScanLog.scan_session_id)
        .having(func.count(ScanLog.id) > 1)
        .all()
    )
    reports = []
    for user_id, scan_session_id, _count in duplicate_keys:
        rows = (
            ScanLog.query.filter_by(user_id=user_id, scan_session_id=scan_session_id)
            .order_by(ScanLog.id.asc())
            .all()
        )
        reports.append(_scan_log_duplicate_report_for_rows(rows))
    return reports


def scan_log_session_uniqueness_report():
    inspector = inspect(db.engine)
    if "scan_logs" not in inspector.get_table_names():
        return {
            "table_exists": False,
            "constraint_exists": False,
            "duplicate_groups": 0,
            "affected_rows": 0,
            "multiple_counted_groups": 0,
            "conflicting_groups": 0,
            "duplicates": [],
        }

    duplicates = _scan_log_duplicate_reports()
    return {
        "table_exists": True,
        "constraint_exists": _scan_log_session_uniqueness_exists(inspector),
        "duplicate_groups": len(duplicates),
        "affected_rows": sum(item["affected_rows"] for item in duplicates),
        "multiple_counted_groups": sum(1 for item in duplicates if item["multiple_counted"]),
        "conflicting_groups": sum(1 for item in duplicates if item["conflicting_scan_data"]),
        "duplicates": duplicates,
    }


def _consolidate_scan_log_duplicate(report):
    rows = {
        row.id: row
        for row in ScanLog.query.filter(
            ScanLog.id.in_([report["canonical_id"], *report["redundant_ids"]])
        ).all()
    }
    canonical = rows[report["canonical_id"]]
    duplicate_rows = [rows[row_id] for row_id in report["redundant_ids"]]
    canonical.is_successful = any(row.is_successful for row in [canonical, *duplicate_rows])
    canonical.counted = any(row.counted for row in [canonical, *duplicate_rows])
    if canonical.pair_id is None:
        pair_ids = [row.pair_id for row in duplicate_rows if row.pair_id is not None]
        if pair_ids:
            canonical.pair_id = pair_ids[0]
    for duplicate in duplicate_rows:
        db.session.delete(duplicate)
    return {
        "user_id": report["user_id"],
        "scan_session_id": report["scan_session_id"],
        "canonical_id": canonical.id,
        "removed_ids": report["redundant_ids"],
        "preserved_successful": bool(canonical.is_successful),
        "preserved_counted": bool(canonical.counted),
    }


@app.cli.command("migrate-scanlog-session-uniqueness")
@click.option("--apply", "apply_change", is_flag=True, help="Create the unique index. Default is dry-run.")
def migrate_scanlog_session_uniqueness(apply_change):
    """Add the per-owner scan-session uniqueness guard after duplicate review."""
    report = scan_log_session_uniqueness_report()
    dialect = _database_dialect_name()
    click.echo("Mode: apply" if apply_change else "Mode: dry-run")
    click.echo(f"Database dialect: {dialect}")
    click.echo(f"Table exists: {report['table_exists']}")
    click.echo(f"Constraint exists: {report['constraint_exists']}")
    click.echo(f"Duplicate groups: {report['duplicate_groups']}")
    click.echo(f"Affected rows: {report['affected_rows']}")
    click.echo(f"Groups with multiple counted rows: {report['multiple_counted_groups']}")
    click.echo(f"Groups with conflicting scan/project data: {report['conflicting_groups']}")

    for duplicate in report["duplicates"]:
        click.echo(
            f"user_id={duplicate['user_id']} "
            f"scan_session_id={duplicate['scan_session_id']} "
            f"count={duplicate['count']} "
            f"counted_rows={duplicate['counted_rows']} "
            f"successful_rows={duplicate['successful_rows']} "
            f"conflicting_scan_data={duplicate['conflicting_scan_data']} "
            f"canonical_id={duplicate['canonical_id']} "
            f"redundant_ids={duplicate['redundant_ids']}"
        )

    if not apply_change or not report["table_exists"]:
        return
    if report["constraint_exists"] and not report["duplicates"]:
        click.echo("Unique index already present; no changes needed.")
        return
    if report["conflicting_groups"]:
        raise click.ClickException(
            "Refusing to consolidate conflicting scan log duplicates. "
            "Review the reported user_id/scan_session_id groups and resolve history manually."
        )

    changes = []
    try:
        for duplicate in report["duplicates"]:
            changes.append(_consolidate_scan_log_duplicate(duplicate))
        db.session.flush()
        if not report["constraint_exists"]:
            db.session.execute(text(
                f"CREATE UNIQUE INDEX {SCAN_LOG_SESSION_UNIQUE_INDEX_NAME} "
                "ON scan_logs (user_id, scan_session_id)"
            ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    for change in changes:
        click.echo(
            f"Consolidated user_id={change['user_id']} "
            f"scan_session_id={change['scan_session_id']} "
            f"canonical_id={change['canonical_id']} "
            f"removed_ids={change['removed_ids']} "
            f"preserved_successful={change['preserved_successful']} "
            f"preserved_counted={change['preserved_counted']}"
        )
    if not report["constraint_exists"]:
        click.echo(f"Created unique index {SCAN_LOG_SESSION_UNIQUE_INDEX_NAME}.")
    click.echo(
        "DDL transaction note: SQLite/PostgreSQL generally roll back this cleanup and CREATE INDEX together; "
        "MySQL/MariaDB may auto-commit DDL, so the command refuses conflicting duplicates before cleanup."
    )


BOOTSTRAP_ADMIN_MIN_PASSWORD_LENGTH = 8


def _resolve_bootstrap_admin_credentials():
    """Return (email, password) for the bootstrap admin, or None if bootstrap
    admin creation is not explicitly enabled.

    No default email/password are ever used - BOOTSTRAP_ADMIN_ENABLED=1
    must be set, and both BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD
    must be provided. Raises RuntimeError on incomplete/invalid explicit
    configuration. Never logs the password.
    """
    if not _env_flag("BOOTSTRAP_ADMIN_ENABLED", default=False):
        return None

    email = (os.environ.get("BOOTSTRAP_ADMIN_EMAIL") or "").strip()
    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or ""

    missing = []
    if not email:
        missing.append("BOOTSTRAP_ADMIN_EMAIL")
    if not password:
        missing.append("BOOTSTRAP_ADMIN_PASSWORD")
    if missing:
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_ENABLED=1 but missing required environment "
            "variable(s): " + ", ".join(missing) + "."
        )
    if len(password) < BOOTSTRAP_ADMIN_MIN_PASSWORD_LENGTH:
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_PASSWORD is too short - must be at least "
            f"{BOOTSTRAP_ADMIN_MIN_PASSWORD_LENGTH} characters."
        )
    return email.lower(), password


def _maybe_create_bootstrap_admin():
    """Create the initial superadmin only when explicitly enabled via env,
    and only when no admin exists yet. Never recreates or overwrites an
    existing administrator. Does not fail startup when bootstrap is simply
    not enabled - only when it's enabled with incomplete/invalid config.
    """
    credentials = _resolve_bootstrap_admin_credentials()
    if credentials is None:
        return
    if Admin.query.count() > 0:
        return
    email, password = credentials
    db.session.add(Admin(
        email=email,
        password_hash=generate_password_hash(password),
        name="Super Admin",
        role="superadmin",
        is_active=True,
    ))

# --------------------------------------------------------------------------------------------
# Bootstrap (tables + default plans + initial admin + system config)
# --------------------------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    ensure_marker_schema()
    
    # Create default trial plan
    if SubscriptionPlan.query.filter_by(is_trial_plan=True).first() is None:
        trial_plan = SubscriptionPlan(
            plan_name="Free Trial",
            plan_description="Free trial with limited features",
            plan_amount=0.0,
            offer_price=0.0,
            currency="INR",
            duration_type="time",
            duration_value=7,  # 7 days trial
            trial_days=7,
            total_project_limit=1,
            total_scan_limit=50,
            is_trial_plan=True,
            features_json='["1 project only", "50 scans limit", "Trial access for 7 days"]',
            is_active=True,
            display_order=0
        )
        db.session.add(trial_plan)
    
    # Create Basic and Pro plans
    if SubscriptionPlan.query.filter_by(plan_name="Basic").first() is None:
        basic_plan = SubscriptionPlan(
            plan_name="Basic",
            plan_description="Basic subscription plan",
            plan_amount=499.0,  # ₹499
            offer_price=399.0,  # ₹399 offer
            currency="INR",
            duration_type="time",
            duration_value=6,  # 6 months
            total_project_limit=5,
            total_scan_limit=500,
            is_popular=False,
            features_json='["5 projects", "500 scans", "6 months validity", "Basic support"]',
            is_active=True,
            display_order=1
        )
        db.session.add(basic_plan)
    
    if SubscriptionPlan.query.filter_by(plan_name="Pro").first() is None:
        pro_plan = SubscriptionPlan(
            plan_name="Pro",
            plan_description="Professional subscription plan",
            plan_amount=999.0,  # ₹999
            offer_price=799.0,  # ₹799 offer
            currency="INR",
            duration_type="time",
            duration_value=12,  # 1 year
            total_project_limit=20,
            total_scan_limit=2000,
            is_popular=True,
            features_json='["20 projects", "2000 scans", "1 year validity", "Priority support", "Advanced features"]',
            is_active=True,
            display_order=2
        )
        db.session.add(pro_plan)
    
    # Create initial admin - only when explicitly enabled via env, with no
    # default credentials. See _maybe_create_bootstrap_admin.
    _maybe_create_bootstrap_admin()
    
    # Create default system config
    if SystemConfig.query.count() == 0:
        default_configs = [
            ("free_trial_projects", "1", "integer", "Free trial project limit"),
            ("free_trial_scans", "50", "integer", "Free trial scan limit"),
            ("free_trial_days", "7", "integer", "Free trial duration in days"),
            ("razorpay_enabled", "true", "boolean", "Enable Razorpay payments"),
            ("currency", "INR", "string", "Default currency"),
        ]
        
        for key, value, config_type, description in default_configs:
            db.session.add(SystemConfig(
                config_key=key,
                config_value=value,
                config_type=config_type,
                description=description
            ))
    
    db.session.commit()

# --------------------------------------------------------------------------------------------
# Helper Functions
# --------------------------------------------------------------------------------------------

OTP_SCHEMA_COLUMNS = {
    "code_hash": "VARCHAR(255)",
    "challenge_id": "VARCHAR(64)",
    "invalidated_at": "DATETIME",
    "locked_until": "DATETIME",
    "attempt_count": "INTEGER DEFAULT 0 NOT NULL",
    "max_attempts": "INTEGER DEFAULT 5 NOT NULL",
    "resend_count": "INTEGER DEFAULT 0 NOT NULL",
    "first_sent_at": "DATETIME",
    "last_sent_at": "DATETIME",
}


OTP_CONFIG_LIMITS = {
    "SCANSTORY_OTP_EXPIRY_SECONDS": (60, 3600),
    "SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS": (1, 20),
    "SCANSTORY_OTP_LOCK_SECONDS": (60, 86400),
    "SCANSTORY_OTP_RESEND_MIN_INTERVAL_SECONDS": (0, 3600),
    "SCANSTORY_OTP_MAX_RESENDS": (0, 20),
    "SCANSTORY_OTP_RESEND_WINDOW_SECONDS": (60, 86400),
    "SCANSTORY_OTP_IP_ATTEMPT_LIMIT": (1, 500),
    "SCANSTORY_OTP_IP_RESEND_LIMIT": (1, 200),
}


def _otp_int_config(name, default):
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} must be an integer within the allowed OTP security range.")
    minimum, maximum = OTP_CONFIG_LIMITS[name]
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} is outside the allowed OTP security range.")
    return value


OTP_EXPIRY_SECONDS = _otp_int_config("SCANSTORY_OTP_EXPIRY_SECONDS", 120)
OTP_MAX_VERIFY_ATTEMPTS = _otp_int_config("SCANSTORY_OTP_MAX_VERIFY_ATTEMPTS", 5)
OTP_LOCK_SECONDS = _otp_int_config("SCANSTORY_OTP_LOCK_SECONDS", 900)
OTP_RESEND_MIN_INTERVAL_SECONDS = _otp_int_config("SCANSTORY_OTP_RESEND_MIN_INTERVAL_SECONDS", 60)
OTP_MAX_RESENDS = _otp_int_config("SCANSTORY_OTP_MAX_RESENDS", 3)
OTP_RESEND_WINDOW_SECONDS = _otp_int_config("SCANSTORY_OTP_RESEND_WINDOW_SECONDS", 900)
OTP_IP_ATTEMPT_LIMIT = _otp_int_config("SCANSTORY_OTP_IP_ATTEMPT_LIMIT", 30)
OTP_IP_RESEND_LIMIT = _otp_int_config("SCANSTORY_OTP_IP_RESEND_LIMIT", 10)


def _otp_security_schema_report():
    inspector = inspect(db.engine)
    if "otp_codes" not in inspector.get_table_names():
        return {
            "table_exists": False,
            "existing_columns": set(),
            "missing_columns": set(OTP_SCHEMA_COLUMNS),
            "challenge_index_exists": False,
            "duplicate_challenge_groups": [],
        }
    existing = {column["name"] for column in inspector.get_columns("otp_codes")}
    duplicate_challenges = []
    if "challenge_id" in existing:
        duplicate_challenges = [
            {"challenge_id": challenge_id, "count": int(count)}
            for challenge_id, count in db.session.query(
                OTPCode.challenge_id,
                func.count(OTPCode.id),
            )
            .filter(OTPCode.challenge_id.isnot(None))
            .group_by(OTPCode.challenge_id)
            .having(func.count(OTPCode.id) > 1)
            .all()
        ]
    return {
        "table_exists": True,
        "existing_columns": existing,
        "missing_columns": set(OTP_SCHEMA_COLUMNS) - existing,
        "challenge_index_exists": scan_otp_challenge_index_exists(),
        "duplicate_challenge_groups": duplicate_challenges,
    }


def ensure_otp_security_schema(apply_change=True):
    report = _otp_security_schema_report()
    if not report["table_exists"] or not apply_change:
        return report
    if report["duplicate_challenge_groups"]:
        raise click.ClickException("Duplicate non-null OTP challenge_id values exist; resolve them before applying migration.")

    existing = report["existing_columns"]
    for column_name, ddl in OTP_SCHEMA_COLUMNS.items():
        if column_name not in existing:
            db.session.execute(text(f"ALTER TABLE otp_codes ADD COLUMN {column_name} {ddl}"))

    refreshed = _otp_security_schema_report()
    if "challenge_id" in refreshed["existing_columns"]:
        if not refreshed["challenge_index_exists"]:
            db.session.execute(text(
                "CREATE UNIQUE INDEX uq_otp_codes_challenge_id "
                "ON otp_codes (challenge_id)"
            ))
    db.session.commit()
    return _otp_security_schema_report()


@app.cli.command("migrate-otp-security-schema")
@click.option("--apply", "apply_change", is_flag=True, help="Apply schema changes. Default is dry-run/inspection only.")
def migrate_otp_security_schema(apply_change):
    """Idempotently inspect or add OTP abuse-protection columns/indexes."""
    report = ensure_otp_security_schema(apply_change=apply_change)
    click.echo("Mode: apply" if apply_change else "Mode: dry-run")
    click.echo(f"otp_codes table exists: {report['table_exists']}")
    click.echo(f"columns present: {', '.join(sorted(report['existing_columns'].intersection(OTP_SCHEMA_COLUMNS)))}")
    click.echo(f"columns to add: {', '.join(sorted(report['missing_columns']))}")
    click.echo(f"unique challenge index present: {report['challenge_index_exists']}")
    click.echo(f"duplicate non-null challenge_id groups: {len(report['duplicate_challenge_groups'])}")
    for duplicate in report["duplicate_challenge_groups"]:
        click.echo(f"challenge_id=<redacted> count={duplicate['count']}")
    if apply_change:
        click.echo(
            "DDL note: SQLite and PostgreSQL generally roll back schema changes transactionally; "
            "MySQL/MariaDB may auto-commit DDL, so duplicate non-null challenge_id values are refused before changes."
        )


def scan_otp_challenge_index_exists():
    inspector = inspect(db.engine)
    if "otp_codes" not in inspector.get_table_names():
        return False
    return any(
        item.get("name") == "uq_otp_codes_challenge_id"
        for item in [*inspector.get_indexes("otp_codes"), *inspector.get_unique_constraints("otp_codes")]
    )


def _generate_otp() -> str:
    return f"{secrets.randbelow(1000000):06d}"

def _hash_otp(code: str) -> str:
    return generate_password_hash(code)


def _otp_matches(rec: OTPCode, code: str) -> bool:
    if rec.code_hash:
        return check_password_hash(rec.code_hash, code)
    return bool(rec.code and secrets.compare_digest(rec.code, code))


def _client_ip():
    return request.remote_addr or "unknown"


def _otp_log(event, rec=None, email=None, purpose=None):
    app.logger.info(
        "[OTP SECURITY] %s email=%s purpose=%s user_id=%s challenge_id=%s",
        event,
        email or (rec.email if rec else None),
        purpose or (rec.purpose if rec else None),
        rec.user_id if rec else None,
        rec.challenge_id if rec else None,
    )


def _latest_otp(email: str, purpose: str):
    return (
        OTPCode.query.filter_by(email=email, purpose=purpose)
        .order_by(OTPCode.created_at.desc(), OTPCode.id.desc())
        .first()
    )


def _active_otp(email: str, purpose: str, challenge_id: str = None):
    query = OTPCode.query.filter_by(email=email, purpose=purpose, is_used=False)
    query = query.filter(OTPCode.invalidated_at.is_(None))
    if challenge_id:
        query = query.filter_by(challenge_id=challenge_id)
    else:
        query = query.filter(OTPCode.challenge_id.is_(None), OTPCode.code_hash.is_(None))
    return query.order_by(OTPCode.created_at.desc(), OTPCode.id.desc()).first()


def _ip_otp_events_since(purpose: str, seconds: int):
    cutoff = dt.utcnow() - timedelta(seconds=seconds)
    return OTPCode.query.filter(
        OTPCode.purpose == purpose,
        OTPCode.ip_address == _client_ip(),
        OTPCode.created_at >= cutoff,
    ).count()


def _create_otp(
    email: str,
    purpose: str,
    minutes: int = None,
    user_id: int = None,
    challenge_id: str = None,
    invalidate_existing: bool = True,
) -> str:
    expiry_seconds = OTP_EXPIRY_SECONDS if minutes is None else int(minutes * 60)
    now = dt.utcnow()
    if invalidate_existing:
        OTPCode.query.filter_by(email=email, purpose=purpose, is_used=False).filter(
            OTPCode.invalidated_at.is_(None)
        ).update({OTPCode.invalidated_at: now}, synchronize_session=False)
    code = _generate_otp()
    challenge_id = challenge_id or secrets.token_urlsafe(24)
    otp = OTPCode(
        email=email,
        code="",
        code_hash=_hash_otp(code),
        purpose=purpose,
        challenge_id=challenge_id,
        expires_at=now + timedelta(seconds=expiry_seconds),
        max_attempts=OTP_MAX_VERIFY_ATTEMPTS,
        first_sent_at=now,
        last_sent_at=now,
        ip_address=_client_ip(),
        user_agent=request.headers.get("User-Agent"),
        user_id=user_id,
    )
    db.session.add(otp)
    db.session.commit()
    _otp_log("issued", otp)
    return code

def _verify_otp(email: str, purpose: str, code: str, challenge_id: str = None) -> bool:
    rec = _active_otp(email, purpose, challenge_id=challenge_id)
    if not rec:
        _otp_log("verification_missing", email=email, purpose=purpose)
        return False

    now = dt.utcnow()
    if rec.locked_until and now < rec.locked_until:
        _otp_log("verification_locked", rec)
        return False

    if now > rec.expires_at:
        rec.invalidated_at = now
        db.session.commit()
        _otp_log("verification_expired", rec)
        return False

    if _ip_otp_events_since(purpose, OTP_RESEND_WINDOW_SECONDS) > OTP_IP_ATTEMPT_LIMIT:
        _otp_log("verification_ip_throttled", rec)
        return False

    if not _otp_matches(rec, code):
        OTPCode.query.filter(
            OTPCode.id == rec.id,
            OTPCode.is_used == False,
            OTPCode.invalidated_at.is_(None),
        ).update(
            {OTPCode.attempt_count: func.coalesce(OTPCode.attempt_count, 0) + 1},
            synchronize_session=False,
        )
        db.session.flush()
        db.session.expire_all()
        rec = OTPCode.query.get(rec.id)
        if int(rec.attempt_count or 0) >= int(rec.max_attempts or OTP_MAX_VERIFY_ATTEMPTS):
            rec.locked_until = now + timedelta(seconds=OTP_LOCK_SECONDS)
            rec.invalidated_at = now
            _otp_log("verification_locked", rec)
        else:
            _otp_log("verification_failed", rec)
        db.session.commit()
        return False

    claimed = OTPCode.query.filter(
        OTPCode.id == rec.id,
        OTPCode.is_used == False,
        OTPCode.invalidated_at.is_(None),
    ).update(
        {OTPCode.is_used: True, OTPCode.used_at: now, OTPCode.invalidated_at: now},
        synchronize_session=False,
    )
    if claimed != 1:
        db.session.rollback()
        _otp_log("verification_race_lost", rec)
        return False
    if not rec.code_hash:
        legacy_rec = OTPCode.query.get(rec.id)
        if legacy_rec:
            db.session.delete(legacy_rec)
    db.session.commit()
    _otp_log("verification_success", rec)
    return True


def _resend_otp(email: str, purpose: str, send_func, minutes: int = None, user_id: int = None):
    now = dt.utcnow()
    latest = _latest_otp(email, purpose)
    claimed_source_id = None
    source_last_sent_at = None
    source_resend_count = None
    if latest and latest.last_sent_at:
        source_last_sent_at = latest.last_sent_at
        source_resend_count = int(latest.resend_count or 0)
        if now - latest.last_sent_at < timedelta(seconds=OTP_RESEND_MIN_INTERVAL_SECONDS):
            _otp_log("resend_throttled", latest)
            return False, "If a verification is available, a new code can be requested shortly."
        window_start = now - timedelta(seconds=OTP_RESEND_WINDOW_SECONDS)
        if latest.first_sent_at and latest.first_sent_at >= window_start:
            if source_resend_count >= OTP_MAX_RESENDS:
                _otp_log("resend_throttled", latest)
                return False, "If a verification is available, a new code can be requested later."
        if _ip_otp_events_since(purpose, OTP_RESEND_WINDOW_SECONDS) > OTP_IP_RESEND_LIMIT:
            _otp_log("resend_ip_throttled", latest)
            return False, "If a verification is available, a new code can be requested later."

        claimed = OTPCode.query.filter(
            OTPCode.id == latest.id,
            OTPCode.is_used == False,
            OTPCode.invalidated_at.is_(None),
            OTPCode.last_sent_at == latest.last_sent_at,
            func.coalesce(OTPCode.resend_count, 0) < OTP_MAX_RESENDS,
        ).update(
            {
                OTPCode.resend_count: func.coalesce(OTPCode.resend_count, 0) + 1,
                OTPCode.last_sent_at: now,
            },
            synchronize_session=False,
        )
        if claimed != 1:
            db.session.rollback()
            _otp_log("resend_throttled", latest)
            return False, "If a verification is available, a new code can be requested later."
        db.session.commit()
        claimed_source_id = latest.id

    code = _create_otp(email, purpose, minutes=minutes, user_id=user_id, invalidate_existing=False)
    new_rec = _latest_otp(email, purpose)
    new_rec.resend_count = (source_resend_count + 1) if latest else 0
    if latest and latest.first_sent_at and now - latest.first_sent_at < timedelta(seconds=OTP_RESEND_WINDOW_SECONDS):
        new_rec.first_sent_at = latest.first_sent_at
    db.session.commit()

    try:
        send_func(email, code, minutes=minutes or max(1, OTP_EXPIRY_SECONDS // 60))
        OTPCode.query.filter(
            OTPCode.email == email,
            OTPCode.purpose == purpose,
            OTPCode.id != new_rec.id,
            OTPCode.is_used == False,
            OTPCode.invalidated_at.is_(None),
        ).update({OTPCode.invalidated_at: dt.utcnow()}, synchronize_session=False)
        db.session.commit()
        _otp_log("resent", new_rec)
        return True, "If a verification is available, a new code has been sent."
    except Exception:
        _otp_log("resend_delivery_failed", new_rec)
        db.session.delete(new_rec)
        if claimed_source_id:
            source = OTPCode.query.get(claimed_source_id)
            if source:
                source.last_sent_at = source_last_sent_at
                source.resend_count = source_resend_count
        db.session.commit()
        return False, "Could not send email. Please try again later."

def get_system_config(key, default=None):
    """Get system configuration value"""
    config = SystemConfig.query.filter_by(config_key=key).first()
    if not config:
        return default
    
    if config.config_type == "integer":
        try:
            return int(config.config_value)
        except:
            return default
    elif config.config_type == "boolean":
        return config.config_value.lower() in ("true", "1", "yes", "t")
    elif config.config_type == "json":
        try:
            return json.loads(config.config_value)
        except:
            return default
    else:
        return config.config_value or default

def set_system_config(key, value, config_type="string", description=None):
    """Set system configuration value"""
    config = SystemConfig.query.filter_by(config_key=key).first()
    if config:
        config.config_value = str(value)
        config.config_type = config_type
        if description:
            config.description = description
    else:
        config = SystemConfig(
            config_key=key,
            config_value=str(value),
            config_type=config_type,
            description=description
        )
        db.session.add(config)
    db.session.commit()

ADMIN_LOGIN_LOCKOUT_MAX_ATTEMPTS = 5
ADMIN_LOGIN_LOCKOUT_MINUTES = 15
ADMIN_LOGIN_ATTEMPT_PREFIX = "admin_login_attempts:"
VALID_ADMIN_ROLES = {"admin", "superadmin"}
ADMIN_ROLE_PERMISSIONS = {
    "admin": {
        "admin.dashboard.view",
        "admin.users.view",
        "admin.users.manage",
        "admin.projects.view",
        "admin.projects.suspend",
        "admin.payments.view",
        "admin.processing.view",
    },
    "superadmin": {
        "admin.dashboard.view",
        "admin.users.view",
        "admin.users.manage",
        "admin.projects.view",
        "admin.projects.suspend",
        "admin.payments.view",
        "admin.processing.view",
        "superadmin.admins.manage",
        "superadmin.plans.manage",
        "superadmin.settings.manage",
        "superadmin.capacity.manage",
        "superadmin.audit.view",
        "superadmin.operations.view",
        "superadmin.repair.execute",
    },
}
HIGH_IMPACT_PERMISSIONS = {
    "superadmin.admins.manage",
    "superadmin.plans.manage",
    "superadmin.settings.manage",
    "superadmin.capacity.manage",
    "superadmin.audit.view",
    "superadmin.operations.view",
    "superadmin.repair.execute",
}


def _admin_login_attempt_key(email):
    return ADMIN_LOGIN_ATTEMPT_PREFIX + (email or "").strip().lower()


def _admin_login_attempt_state(email):
    state = get_system_config(_admin_login_attempt_key(email), {}) or {}
    if not isinstance(state, dict):
        return {"count": 0, "locked_until": None}
    return state


def _admin_login_locked(email, now=None):
    now = now or dt.utcnow()
    locked_until = _admin_login_attempt_state(email).get("locked_until")
    if not locked_until:
        return False
    try:
        return dt.fromisoformat(locked_until) > now
    except Exception:
        return False


def _record_admin_login_failure(email, admin=None):
    normalized = (email or "").strip().lower()
    if admin:
        log_admin_activity(admin.id, "login_failed", "Failed admin login attempt")
    state = _admin_login_attempt_state(normalized)
    count = int(state.get("count") or 0) + 1
    next_state = {"count": count, "locked_until": state.get("locked_until")}
    if count >= ADMIN_LOGIN_LOCKOUT_MAX_ATTEMPTS:
        next_state["locked_until"] = (dt.utcnow() + timedelta(minutes=ADMIN_LOGIN_LOCKOUT_MINUTES)).isoformat()
    set_system_config(_admin_login_attempt_key(normalized), json.dumps(next_state), "json", "Admin login lockout state")


def _clear_admin_login_failures(email):
    set_system_config(_admin_login_attempt_key(email), json.dumps({"count": 0, "locked_until": None}), "json", "Admin login lockout state")


def admin_page_size(default=25, max_size=100):
    return min(max(request.args.get("per_page", default, type=int), 1), max_size)


def _project_unavailable_response():
    return ("This project is currently suspended or unavailable.", 404)


def _project_is_available(project):
    return bool(project and project.is_active)


def _validate_admin_role(role):
    role = (role or "").strip().lower()
    if role not in VALID_ADMIN_ROLES:
        raise ValueError("Invalid admin role.")
    return role


def _active_superadmin_count():
    # with_for_update gives databases that support row locks a chance to serialize final
    # Super Admin transitions; SQLite ignores it, so the invariant is still rechecked in
    # the same transaction immediately before mutation.
    return Admin.query.filter_by(role="superadmin", is_active=True).with_for_update().count()


def _can_change_active_superadmin(target_admin, acting_admin, new_role=None, new_active=None, action="change"):
    old_role = target_admin.role
    old_active = bool(target_admin.is_active)
    next_role = old_role if new_role is None else new_role
    next_active = old_active if new_active is None else bool(new_active)
    if target_admin.id == acting_admin.id and old_role == "superadmin" and old_active and (
        next_role != "superadmin" or not next_active
    ):
        return False, f"You cannot {action} your own active super admin account."
    if old_role == "superadmin" and old_active and (next_role != "superadmin" or not next_active):
        if _active_superadmin_count() <= 1:
            return False, f"Cannot {action} the final active super admin."
    return True, None


def admin_has_permission(admin, permission):
    if not admin or not admin.is_active:
        return False
    role = (admin.role or "").strip().lower()
    if role not in VALID_ADMIN_ROLES:
        return False
    return permission in ADMIN_ROLE_PERMISSIONS.get(role, set())


def require_admin_permission(permission):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            admin = current_admin()
            if not admin:
                flash("Please login as admin to access this page.", "error")
                return redirect(url_for("admin_login_route"))
            if not admin_has_permission(admin, permission):
                if permission in HIGH_IMPACT_PERMISSIONS:
                    log_admin_activity(admin.id, "access_denied", f"Denied permission: {permission}")
                flash("Access denied. Super admin privileges required.", "error")
                return redirect(url_for("admin_dashboard"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def inject_admin_permission_helpers():
    return {
        "admin_can": lambda permission: admin_has_permission(current_admin(), permission)
    }


def _project_from_qr_filename(filename, admin_project=False):
    try:
        project_id = int(str(filename).split("_")[1])
    except Exception:
        return None
    project = Project.query.get(project_id)
    if not project:
        return None
    if admin_project and not project.owner_admin_id:
        return None
    if not admin_project and project.owner_admin_id:
        return None
    return project

def log_admin_activity(admin_id, activity_type, description):
    """Log admin activity"""
    activity = AdminActivity(
        admin_id=admin_id,
        activity_type=activity_type,
        description=description
    )
    db.session.add(activity)
    db.session.commit()    
# ============================================
# PROJECT DISPLAY NUMBER HELPER
# ============================================

def get_project_display_number(project):
    """Get sequential display number (1,2,3...) for user or admin"""
    # If a persisted per-owner index exists, prefer that (faster and stable)
    if getattr(project, 'user_project_index', None):
        return project.user_project_index
    if project.owner_user_id:
        # Count projects this user created before this one
        count = Project.query.filter(
            Project.owner_user_id == project.owner_user_id,
            Project.created_at < project.created_at
        ).count()
        return count + 1
    elif project.owner_admin_id:
        # Count projects this admin created before this one
        count = Project.query.filter(
            Project.owner_admin_id == project.owner_admin_id,
            Project.created_at < project.created_at
        ).count()
        return count + 1
    return project.id
# Add this after the helper function
@app.template_filter('project_display_number')
def project_display_number_filter(project):
    """Jinja2 filter to get display number"""
    return get_project_display_number(project)
# --------------------------------------------------------------------------------------------
# SMTP Email
# --------------------------------------------------------------------------------------------
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email_smtp(to_email: str, subject: str, html_body: str):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM", username)
    
    if not all([host, port, username, password, mail_from]):
        raise RuntimeError("SMTP env vars missing.")
    
    msg = MIMEMultipart("alternative")
    msg["From"] = mail_from
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(username, password)
        server.sendmail(mail_from, to_email, msg.as_string())

def send_email_verification_otp(to_email: str, code: str, minutes: int = 2):
    html = render_template("user/email_verification.html", code=code, minutes=minutes, year=dt.utcnow().year)
    send_email_smtp(to_email, "ScanStory - Email Verification OTP", html)

def send_reset_password_otp(to_email: str, code: str, minutes: int = 2):
    now = dt.utcnow()
    html = render_template(
        "user/email_verification.html",  # ✅ CORRECT! Use email template
        code=code,
        minutes=minutes,
        now=now,
        year=now.year,
        email=to_email,
    )
    send_email_smtp(to_email, "ScanStory - Password Reset OTP", html)

def send_payment_success_email(user, plan, order):
    """Send payment success email"""
    html = render_template(
        "user/payment_success_email.html",
        user=user,
        plan=plan,
        order=order,
        year=dt.utcnow().year
    )
    send_email_smtp(user.email, "ScanStory - Payment Successful", html)

def send_admin_password_reset_email(to_email: str, code: str, minutes: int = 2):
    """Send admin password reset email"""
    html = render_template(
        "admin/reset_password_email.html",
        code=code,
        minutes=minutes,
        email=to_email,
        year=dt.utcnow().year
    )
    send_email_smtp(to_email, "ScanStory Admin - Password Reset OTP", html)

# --------------------------------------------------------------------------------------------
# Session helpers
# --------------------------------------------------------------------------------------------
def login_user(user: User):
    session["user_id"] = user.id
    session["user_email"] = user.email

def _clear_otp_session_state():
    for key in (
        "pending_verify_email",
        "pending_verify_challenge_id",
        "pending_reset_email",
        "pending_reset_challenge_id",
        "pending_admin_reset_email",
        "pending_admin_reset_challenge_id",
    ):
        session.pop(key, None)


def logout_user():
    session.pop("user_id", None)
    session.pop("user_email", None)
    _clear_otp_session_state()

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            # An authenticated Admin/Super Admin has session["admin_id"], never
            # session["user_id"] - route them to their own dashboard instead of
            # the normal-user login page (see fix/admin-navigation-routing).
            if current_admin():
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("login"))
        if getattr(u, "is_blocked", False):
            logout_user()
            flash("Your account is blocked. Contact support.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped

def admin_login(admin: Admin):
    session["admin_id"] = admin.id
    session["admin_email"] = admin.email
    # Informational only. Authorization always reloads the current Admin row from the DB.
    session["admin_role"] = admin.role

def admin_logout():
    session.pop("admin_id", None)
    session.pop("admin_email", None)
    session.pop("admin_role", None)
    _clear_otp_session_state()

def current_admin():
    aid = session.get("admin_id")
    if not aid:
        return None
    admin = Admin.query.get(aid)
    if not admin or not admin.is_active or (admin.role or "").strip().lower() not in VALID_ADMIN_ROLES:
        admin_logout()
        return None
    return admin

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_admin():
            flash("Please login as admin to access this page.", "error")
            return redirect(url_for("admin_login_route"))
        return view(*args, **kwargs)
    return wrapped

def super_admin_required(view):
    return require_admin_permission("superadmin.admins.manage")(view)

# --------------------------------------------------------------------------------------------
# Subscription Enforcement Functions
# --------------------------------------------------------------------------------------------
def _limit_reached(limit_value, used_value):
    """
    None or 0 means unlimited.
    Number means enforce the limit.
    """
    if limit_value is None:
        return False
    try:
        if int(limit_value) == 0:
            return False
    except (TypeError, ValueError):
        return False
    return int(used_value or 0) >= int(limit_value)


def _database_dialect_name():
    try:
        return db.session.get_bind().dialect.name
    except Exception:
        return "unknown"


def _supports_row_level_locking():
    return _database_dialect_name() in {"postgresql", "mysql", "mariadb"}


def _atomic_increment_user_counter(user_id, counter_column, limit_column):
    """Atomically increment a user counter if its effective limit allows it.

    NULL or 0 limits are treated as unlimited, matching _limit_reached().
    Returns True when one user row was updated.
    """
    query = User.query.filter(User.id == user_id).filter(
        or_(
            limit_column.is_(None),
            limit_column == 0,
            func.coalesce(counter_column, 0) < limit_column,
        )
    )
    updated = query.update(
        {counter_column: func.coalesce(counter_column, 0) + 1},
        synchronize_session=False,
    )
    return updated == 1


def _reserve_project_quota_atomic(user):
    if has_dev_test_entitlement(user):
        return True
    reserved = _atomic_increment_user_counter(
        user.id,
        User.projects_used,
        User.subscribed_project_limit,
    )
    if not reserved:
        user.subscription_status = "limit_reached"
        db.session.flush()
    return reserved


def _consume_scan_quota_atomic(user):
    if has_dev_test_entitlement(user):
        return True
    consumed = _atomic_increment_user_counter(
        user.id,
        User.scans_used,
        User.subscribed_scan_limit,
    )
    if consumed and _limit_reached(user.subscribed_scan_limit, (user.scans_used or 0) + 1):
        user.subscription_status = "limit_reached"
        db.session.flush()
    elif not consumed:
        user.subscription_status = "limit_reached"
        db.session.flush()
    return consumed


def _lock_project_for_pair_quota(project_id):
    query = Project.query.filter(Project.id == project_id)
    if _supports_row_level_locking():
        query = query.with_for_update()
    return query.one()


def _reserve_pair_slots_for_project(project_id, requested_pairs, max_pairs):
    if max_pairs is None:
        return True, None
    project = _lock_project_for_pair_quota(project_id)
    existing_pairs = ProjectPair.query.filter_by(project_id=project.id).count()
    if existing_pairs + requested_pairs > int(max_pairs):
        return False, f"Your current plan allows maximum {max_pairs} pairs per project."
    return True, None


# ---------------------------------------------------------------------------
# V1 paid-account capacity gate (Phase 2). See models.py CapacityConfig /
# PaymentReservation docstrings for the counter invariant and lifecycle.
# ---------------------------------------------------------------------------
CAPACITY_DEFAULT_LIMIT = int(os.environ.get("SCANSTORY_INITIAL_CAPACITY_LIMIT", "25"))
CAPACITY_RESERVATION_TTL_MINUTES = int(os.environ.get("SCANSTORY_CAPACITY_RESERVATION_TTL_MINUTES", "30"))


def _get_or_create_capacity_config():
    """Get the singleton capacity_config row (id=1), creating it with the
    default limit/enabled state on first use. This is a data seed, not a
    schema change - the table itself is created by an Alembic migration."""
    config = CapacityConfig.query.get(1)
    if config:
        return config
    config = CapacityConfig(id=1, configured_limit=CAPACITY_DEFAULT_LIMIT, enabled=True, consumed_count=0)
    db.session.add(config)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        config = CapacityConfig.query.get(1)
    return config


def _reserve_capacity_slot_atomic(user):
    """Atomically reserve one paid-account capacity slot for `user`.

    Mirrors _atomic_increment_user_counter exactly: a single
    `UPDATE capacity_config SET consumed_count = consumed_count + 1 WHERE
    id=1 AND enabled=1 AND consumed_count < configured_limit` either updates
    exactly one row (slot reserved) or zero rows (full/disabled). There is no
    separate COUNT(*) read before the write, so two concurrent callers can
    never both observe "room" and both proceed past the limit - the DB
    engine's own atomic handling of a single UPDATE statement (row lock on
    MySQL/Postgres, whole-database lock on SQLite) is what makes this safe,
    exactly as it already does for _atomic_increment_user_counter (which also
    never calls with_for_update()).

    Returns the new PaymentReservation on success, or None if capacity is
    full or paused. The reservation row is only ever created in the same
    transaction as the successful counter increment, so the two can never
    drift apart.
    """
    _get_or_create_capacity_config()

    updated = CapacityConfig.query.filter(
        CapacityConfig.id == 1,
        CapacityConfig.enabled.is_(True),
        CapacityConfig.consumed_count < CapacityConfig.configured_limit,
    ).update(
        {CapacityConfig.consumed_count: CapacityConfig.consumed_count + 1},
        synchronize_session=False,
    )
    if updated != 1:
        db.session.rollback()
        return None

    reservation = PaymentReservation(
        user_id=user.id,
        status="reserved",
        expires_at=dt.utcnow() + timedelta(minutes=CAPACITY_RESERVATION_TTL_MINUTES),
    )
    db.session.add(reservation)
    db.session.commit()
    app.logger.info(f"capacity_reservation_created reservation_id={reservation.id} user_id={user.id}")
    return reservation


def _release_capacity_slot(reservation, new_status, reason):
    """Atomically transition a `reserved` PaymentReservation to `released` or
    `expired` and free its capacity slot back. Idempotent: if the reservation
    is not currently `reserved` (already activated/released/expired by
    another caller), this is a safe no-op returning False - never double
    frees a slot.
    """
    updated = PaymentReservation.query.filter(
        PaymentReservation.id == reservation.id,
        PaymentReservation.status == "reserved",
    ).update({PaymentReservation.status: new_status}, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        return False

    CapacityConfig.query.filter(
        CapacityConfig.id == 1,
        CapacityConfig.consumed_count > 0,
    ).update(
        {CapacityConfig.consumed_count: CapacityConfig.consumed_count - 1},
        synchronize_session=False,
    )
    db.session.commit()
    app.logger.info(f"capacity_reservation_{new_status} reservation_id={reservation.id} reason={reason}")
    return True


def _capacity_state_snapshot():
    """Read-only view of current capacity state for CLI/ops use."""
    config = _get_or_create_capacity_config()
    reserved_count = PaymentReservation.query.filter(
        PaymentReservation.status == "reserved",
        PaymentReservation.expires_at > dt.utcnow(),
    ).count()
    activated_count = PaymentReservation.query.filter_by(status="activated").count()
    active_user_count = User.query.filter_by(subscription_status="active").count()
    return {
        "configured_limit": config.configured_limit,
        "enabled": config.enabled,
        "consumed_count": config.consumed_count,
        "reserved_count": reserved_count,
        "activated_reservation_count": activated_count,
        "active_user_count": active_user_count,
    }


MARKER_MIN_PIXELS = int(os.environ.get("SCANSTORY_MARKER_MIN_PIXELS", "240"))
VIDEO_UPLOAD_WARNINGS = {
    "recommended_size_bytes": int(os.environ.get("SCANSTORY_VIDEO_RECOMMENDED_SIZE_BYTES", str(15 * 1024 * 1024))),
    "warning_size_bytes": int(os.environ.get("SCANSTORY_VIDEO_WARNING_SIZE_BYTES", str(30 * 1024 * 1024))),
    "recommended_duration_seconds": int(os.environ.get("SCANSTORY_VIDEO_RECOMMENDED_DURATION_SECONDS", "30")),
    "warning_duration_seconds": int(os.environ.get("SCANSTORY_VIDEO_WARNING_DURATION_SECONDS", "60")),
    "recommended_max_resolution_height": int(os.environ.get("SCANSTORY_VIDEO_RECOMMENDED_MAX_HEIGHT", "1080")),
}


def _upload_log(stage, upload_id, **fields):
    payload = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    print(
        f"[{stage}] ts={dt.utcnow().isoformat(timespec='milliseconds')}Z "
        f"upload_id={upload_id} pid={os.getpid()} thread_id={threading.get_ident()} {payload}".strip()
    )
    sys.stdout.flush()


def _video_log_fields(video_file=None, **fields):
    if video_file is not None:
        fields.setdefault("filename", video_file.filename)
        fields.setdefault("mime_type", video_file.mimetype)
        fields.setdefault("video_size", video_file.content_length)
    return fields


def _parse_marker_meta(index):
    def form_value(name, default=None):
        return request.form.get(f"marker_{index}_{name}", default)

    if form_value("mode") is None:
        return {
            "mode": "full_image",
            "crop_x": 0.0,
            "crop_y": 0.0,
            "crop_width": 1.0,
            "crop_height": 1.0,
            "rotation": 0,
            "original_width": None,
            "original_height": None,
            "processed_width": None,
            "processed_height": None,
            "source_size_bytes": None,
            "processed_size_bytes": None,
            "display_orientation": "legacy",
        }

    mode = (form_value("mode", "crop") or "crop").strip()
    if mode not in ("crop", "full_image"):
        raise ValueError("Invalid marker mode")

    if mode == "full_image":
        crop_x, crop_y, crop_w, crop_h = 0.0, 0.0, 1.0, 1.0
    else:
        try:
            crop_x = float(form_value("crop_x"))
            crop_y = float(form_value("crop_y"))
            crop_w = float(form_value("crop_width"))
            crop_h = float(form_value("crop_height"))
        except (TypeError, ValueError):
            raise ValueError("Invalid crop metadata")

    values = [crop_x, crop_y, crop_w, crop_h]
    if any(not np.isfinite(v) for v in values):
        raise ValueError("Invalid crop metadata")
    if crop_x < 0 or crop_y < 0 or crop_w <= 0 or crop_h <= 0 or crop_x + crop_w > 1.0001 or crop_y + crop_h > 1.0001:
        raise ValueError("Crop must stay inside image bounds")

    try:
        original_w = int(float(form_value("original_width", 0) or 0))
        original_h = int(float(form_value("original_height", 0) or 0))
        processed_w = int(float(form_value("processed_width", 0) or 0))
        processed_h = int(float(form_value("processed_height", 0) or 0))
        rotation = int(float(form_value("rotation", 0) or 0)) % 360
        source_bytes = int(float(form_value("source_size_bytes", 0) or 0))
        processed_bytes = int(float(form_value("processed_size_bytes", 0) or 0))
    except (TypeError, ValueError):
        raise ValueError("Invalid marker dimensions")

    if original_w <= 0 or original_h <= 0 or processed_w <= 0 or processed_h <= 0:
        raise ValueError("Marker dimensions are required")
    if mode == "crop" and (processed_w < MARKER_MIN_PIXELS or processed_h < MARKER_MIN_PIXELS):
        raise ValueError(f"Marker crop must be at least {MARKER_MIN_PIXELS}px on each side")

    return {
        "mode": mode,
        "crop_x": crop_x,
        "crop_y": crop_y,
        "crop_width": crop_w,
        "crop_height": crop_h,
        "rotation": rotation,
        "original_width": original_w,
        "original_height": original_h,
        "processed_width": processed_w,
        "processed_height": processed_h,
        "source_size_bytes": source_bytes,
        "processed_size_bytes": processed_bytes,
        "display_orientation": (form_value("display_orientation", "") or "").strip()[:20],
    }


def get_plan_pairs_limit(user):
    """Return the configured max pairs per project for the user's current plan."""
    if has_dev_test_entitlement(user):
        return None
    plan = getattr(user, "subscription_plan", None)
    if not plan:
        return None
    try:
        max_pairs = plan.max_pairs_per_project
        if max_pairs is None:
            return None
        max_pairs = int(max_pairs)
        return max_pairs if max_pairs > 0 else None
    except (TypeError, ValueError):
        return None


DEV_TEST_USER_EMAILS = tuple(f"scanstorytest{i:02d}@gmail.com" for i in range(1, 11))
DEV_TEST_CONFIG_KEY = "dev_test_user_identity"


def _production_mode_flag_active():
    for key in ("SCANSTORY_PRODUCTION", "APP_ENV", "ENV"):
        value = (os.environ.get(key) or "").strip().lower()
        if value in ("1", "true", "yes", "production", "prod"):
            return True
    return (os.environ.get("FLASK_ENV") or "").strip().lower() in ("production", "prod")


def scanner_diagnostics_enabled():
    """Dev/testing-only gate for the scanner diagnostics panel. Rendered server-side into
    the template — if this is False the panel's HTML never reaches the page, so the
    client-side ?scanner_debug=1 query flag alone can never surface it in production."""
    if _production_mode_flag_active():
        return False
    return bool(SCANSTORY_TESTING or app.debug or (os.environ.get("FLASK_ENV") or "").strip().lower() == "development")


def dev_test_entitlement_enabled():
    return (
        (os.environ.get("FLASK_ENV") or "").strip().lower() == "development"
        and os.environ.get("SCANSTORY_DEV_TESTING") == "1"
        and not _production_mode_flag_active()
    )


def _dev_test_identity_payload():
    config = SystemConfig.query.filter_by(config_key=DEV_TEST_CONFIG_KEY).first()
    if not config or not config.config_value:
        return {"user_ids": [], "emails": []}
    try:
        payload = json.loads(config.config_value)
    except Exception:
        return {"user_ids": [], "emails": []}
    return {
        "user_ids": [int(uid) for uid in payload.get("user_ids", []) if str(uid).isdigit()],
        "emails": [str(email).strip().lower() for email in payload.get("emails", [])],
    }


def _store_dev_test_identity(users):
    payload = {
        "user_ids": sorted({int(user.id) for user in users if user.id}),
        "emails": sorted({user.email.strip().lower() for user in users}),
    }
    config = SystemConfig.query.filter_by(config_key=DEV_TEST_CONFIG_KEY).first()
    if not config:
        config = SystemConfig(
            config_key=DEV_TEST_CONFIG_KEY,
            config_type="json",
            description="Development-only seeded ScanStory test user identity allowlist",
        )
        db.session.add(config)
    config.config_value = json.dumps(payload, sort_keys=True)
    return payload


def has_dev_test_entitlement(user):
    if not user or not dev_test_entitlement_enabled():
        return False
    payload = _dev_test_identity_payload()
    user_email = (getattr(user, "email", "") or "").strip().lower()
    entitled = int(user.id) in set(payload["user_ids"]) and user_email in set(payload["emails"])
    if entitled:
        print(f"[DEV TEST ENTITLEMENT] Unlimited local test access for user_id={user.id}")
    return entitled


def check_user_limits(user):
    """
    Single source of truth enforcement:
    - None / NULL limit means unlimited
    - Numeric limit is enforced
    """
    if user.is_blocked:
        return False, url_for("login"), "Account is blocked"

    if has_dev_test_entitlement(user):
        return True, None, None

    user.projects_used = int(user.projects_used or 0)
    user.scans_used = int(user.scans_used or 0)

    if user.subscription_status in ("trial", "limit_reached"):
        trial = TrialDetails.query.filter_by(user_id=user.id).first()

        if trial and not trial.is_active:
            user.subscription_status = "expired"
            db.session.commit()
            return False, url_for("subscribe_page"), "Trial period expired"

        if _limit_reached(user.subscribed_project_limit, user.projects_used):
            user.subscription_status = "limit_reached"
            db.session.commit()
            return False, url_for("subscribe_page"), f"Project limit reached ({user.subscribed_project_limit} projects)"

        if _limit_reached(user.subscribed_scan_limit, user.scans_used):
            user.subscription_status = "limit_reached"
            db.session.commit()
            return False, url_for("subscribe_page"), f"Scan limit reached ({user.subscribed_scan_limit} scans)"

        if user.subscription_status == "limit_reached":
            user.subscription_status = "trial"
            db.session.commit()

        return True, None, None

    if user.subscription_status == "active":
        if user.subscription_expires_at and user.subscription_expires_at < dt.utcnow():
            user.subscription_status = "expired"
            db.session.commit()
            return False, url_for("subscribe_page"), "Subscription expired"

        if _limit_reached(user.subscribed_project_limit, user.projects_used):
            user.subscription_status = "limit_reached"
            db.session.commit()
            return False, url_for("subscribe_page"), "Project limit reached"

        if _limit_reached(user.subscribed_scan_limit, user.scans_used):
            user.subscription_status = "limit_reached"
            db.session.commit()
            return False, url_for("subscribe_page"), "Scan limit reached"

        return True, None, None

    if user.subscription_status in ("expired",):
        return False, url_for("subscribe_page"), "Please upgrade your plan"

    return True, None, None

def enforce_subscription(view):
    """Decorator to enforce subscription limits before allowing access"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return redirect(url_for("login"))
        
        # Check subscription limits
        can_proceed, redirect_url, message = check_user_limits(user)
        
        if not can_proceed and redirect_url:
            flash(message, "error")
            return redirect(redirect_url)
        
        return view(*args, **kwargs)
    return wrapped

# --------------------------------------------------------------------------------------------
# Project delete helper
# --------------------------------------------------------------------------------------------
def _delete_project_files_and_rows(project: Project):
    pairs = ProjectPair.query.filter_by(project_id=project.id).all()
    for pair in pairs:
        img_path = os.path.join(IMAGES_DIR, pair.image_filename)
        vid_path = os.path.join(VIDEOS_DIR, pair.video_filename)
        npz_path = os.path.join(FEATURES_DIR, f"{project.id}_{pair.pair_index}.npz")
        for path in (img_path, vid_path, npz_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass
        db.session.delete(pair)
    
    if project.qr_code_path:
        qr_file = os.path.basename(project.qr_code_path)
        qr_path = os.path.join(QR_DIR, qr_file)
        if os.path.exists(qr_path):
            try:
                os.remove(qr_path)
            except Exception:
                pass
    
    db.session.delete(project)
    db.session.commit()
    load_features.cache_clear()


def _quota_counter_rows():
    users = User.query.order_by(User.id.asc()).all()
    rows = []
    for user in users:
        calculated_projects = Project.query.filter_by(owner_user_id=user.id).count()
        calculated_scans = (
            db.session.query(func.count(ScanLog.id))
            .join(Project, ScanLog.project_id == Project.id)
            .filter(
                ScanLog.user_id == user.id,
                ScanLog.counted == True,
                Project.owner_admin_id.is_(None),
            )
            .scalar()
            or 0
        )
        rows.append({
            "user": user,
            "stored_projects": int(user.projects_used or 0),
            "calculated_projects": int(calculated_projects or 0),
            "stored_scans": int(user.scans_used or 0),
            "calculated_scans": int(calculated_scans or 0),
        })
    return rows


@app.cli.command("reconcile-quota-counters")
@click.option("--repair", is_flag=True, help="Persist calculated counter values. Default is dry-run.")
def reconcile_quota_counters(repair):
    """Report or repair user quota counter drift."""
    rows = _quota_counter_rows()
    drift_rows = [
        row for row in rows
        if row["stored_projects"] != row["calculated_projects"]
        or row["stored_scans"] != row["calculated_scans"]
    ]
    click.echo("Mode: repair" if repair else "Mode: dry-run")
    click.echo(f"Users checked: {len(rows)}")
    click.echo(f"Users with drift: {len(drift_rows)}")

    for row in drift_rows:
        user = row["user"]
        click.echo(
            f"user_id={user.id} email={user.email} "
            f"projects_used stored={row['stored_projects']} calculated={row['calculated_projects']} "
            f"scans_used stored={row['stored_scans']} calculated={row['calculated_scans']}"
        )
        if repair:
            user.projects_used = row["calculated_projects"]
            user.scans_used = row["calculated_scans"]
            app.logger.info(
                f"Repaired quota counters for user_id={user.id}: "
                f"projects {row['stored_projects']}->{row['calculated_projects']}, "
                f"scans {row['stored_scans']}->{row['calculated_scans']}",
            )

    if repair and drift_rows:
        db.session.commit()
    elif repair:
        click.echo("No repairs needed.")


@app.cli.command("capacity-status")
def capacity_status():
    """Report current paid-account capacity state (read-only)."""
    snapshot = _capacity_state_snapshot()
    click.echo(f"Configured limit: {snapshot['configured_limit']}")
    click.echo(f"Enabled: {snapshot['enabled']}")
    click.echo(f"Consumed count (reserved+activated, gates new reservations): {snapshot['consumed_count']}")
    click.echo(f"Live reserved (pending checkout, not expired): {snapshot['reserved_count']}")
    click.echo(f"Activated reservations: {snapshot['activated_reservation_count']}")
    click.echo(f"Active users (User.subscription_status='active'): {snapshot['active_user_count']}")


@app.cli.command("expire-stale-reservations")
@click.option("--apply", "apply_changes", is_flag=True, help="Persist expirations. Default is dry-run.")
def expire_stale_reservations(apply_changes):
    """Expire `reserved` PaymentReservation rows whose TTL has passed,
    freeing their capacity slot. No background scheduler exists in this
    phase - run this periodically as an operator/cron task."""
    stale = PaymentReservation.query.filter(
        PaymentReservation.status == "reserved",
        PaymentReservation.expires_at < dt.utcnow(),
    ).all()
    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"Stale reservations found: {len(stale)}")
    expired_count = 0
    for reservation in stale:
        click.echo(f"reservation_id={reservation.id} user_id={reservation.user_id} expired_at={reservation.expires_at}")
        if apply_changes:
            if _release_capacity_slot(reservation, "expired", "cli-sweep"):
                expired_count += 1
    if apply_changes:
        click.echo(f"Expired: {expired_count}")


@app.cli.command("reconcile-capacity-reservations")
@click.option("--apply", "apply_changes", is_flag=True, help="Persist the repaired counter. Default is dry-run.")
def reconcile_capacity_reservations(apply_changes):
    """Detect drift between capacity_config.consumed_count and the actual
    row-state count it should equal, and drift between activated
    reservations and real active users (a signal something upstream is
    inconsistent - this command never touches User rows, only the
    capacity_config counter)."""
    config = _get_or_create_capacity_config()
    live_reserved_or_activated = PaymentReservation.query.filter(
        PaymentReservation.status.in_(("reserved", "activated"))
    ).count()
    activated_count = PaymentReservation.query.filter_by(status="activated").count()
    active_user_count = User.query.filter_by(subscription_status="active").count()

    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"capacity_config.consumed_count stored={config.consumed_count} calculated={live_reserved_or_activated}")
    click.echo(f"activated reservations={activated_count} vs active users={active_user_count}")
    if activated_count != active_user_count:
        click.echo(
            "NOTE: activated-reservation count and active-user count differ - "
            "expected for legacy orders that predate this phase's reservation "
            "table, or if a User's subscription later lapsed/was changed by "
            "other means. Investigate if unexpectedly large."
        )

    if config.consumed_count != live_reserved_or_activated:
        click.echo(f"DRIFT: consumed_count stored={config.consumed_count} should be {live_reserved_or_activated}")
        if apply_changes:
            CapacityConfig.query.filter(CapacityConfig.id == config.id).update(
                {CapacityConfig.consumed_count: live_reserved_or_activated}, synchronize_session=False
            )
            db.session.commit()
            app.logger.info(
                f"capacity_reconciliation_applied consumed_count {config.consumed_count}->{live_reserved_or_activated}"
            )
            click.echo("Repaired.")
    else:
        click.echo("No drift.")


def _desired_dev_test_user_values(plan):
    now = dt.utcnow()
    return {
        "first_name": "ScanStory",
        "last_name": "Dev Test",
        "company": "Development Test Account",
        "password_hash": generate_password_hash("123456"),
        "is_verified": True,
        "email_verified_at": now,
        "is_blocked": False,
        "subscription_id": plan.id if plan else None,
        "subscription_status": "trial",
        "subscription_taken_at": now,
        "subscription_expires_at": None,
        "subscribed_project_limit": int(plan.total_project_limit if plan else 1),
        "subscribed_scan_limit": int(plan.total_scan_limit if plan else 50),
        "projects_used": 0,
        "scans_used": 0,
        "razorpay_customer_id": None,
        "razorpay_subscription_id": None,
    }


def _ensure_dev_test_trial(user, plan):
    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    if not trial:
        trial = TrialDetails(user_id=user.id)
        db.session.add(trial)
    trial.trial_start = dt.utcnow()
    trial.trial_end = dt.utcnow() + timedelta(days=3650)
    trial.trial_project_limit = int(plan.total_project_limit if plan else 1)
    trial.trial_scan_limit = int(plan.total_scan_limit if plan else 50)
    trial.converted_to_paid = False
    trial.converted_at = None
    trial.converted_plan_id = None


def _seed_dev_test_users():
    if not dev_test_entitlement_enabled():
        raise click.ClickException("Refusing to seed: require FLASK_ENV=development and SCANSTORY_DEV_TESTING=1 with no production flag.")
    db.create_all()
    plan = SubscriptionPlan.query.filter_by(is_trial_plan=True, is_active=True).first()
    payload = _dev_test_identity_payload()
    known_ids = set(payload["user_ids"])
    known_emails = set(payload["emails"])
    created = updated = skipped = 0
    touched = []

    for email in DEV_TEST_USER_EMAILS:
        user = User.query.filter_by(email=email).first()
        if user and user.id not in known_ids and email not in known_emails:
            skipped += 1
            continue

        values = _desired_dev_test_user_values(plan)
        if not user:
            user = User(email=email, **values)
            db.session.add(user)
            db.session.flush()
            _ensure_dev_test_trial(user, plan)
            created += 1
        else:
            changed = False
            for key, value in values.items():
                if key in ("email_verified_at", "subscription_taken_at") and getattr(user, key):
                    continue
                if key == "password_hash":
                    if not check_password_hash(user.password_hash, "123456"):
                        setattr(user, key, value)
                        changed = True
                    continue
                if getattr(user, key) != value:
                    setattr(user, key, value)
                    changed = True
            _ensure_dev_test_trial(user, plan)
            if changed:
                updated += 1
            else:
                skipped += 1
        touched.append(user)

    db.session.flush()
    _store_dev_test_identity(touched)
    db.session.commit()
    return {"created": created, "updated": updated, "skipped": skipped}


def _dev_test_cleanup_candidates():
    payload = _dev_test_identity_payload()
    if not payload["user_ids"] or not payload["emails"]:
        return []
    allowed_emails = set(DEV_TEST_USER_EMAILS).intersection(payload["emails"])
    if not allowed_emails:
        return []
    return User.query.filter(User.id.in_(payload["user_ids"]), User.email.in_(allowed_emails)).all()


def _delete_dev_test_users(confirm=False, dry_run=False):
    if not dev_test_entitlement_enabled():
        raise click.ClickException("Refusing cleanup: require FLASK_ENV=development and SCANSTORY_DEV_TESTING=1 with no production flag.")
    if confirm == dry_run:
        raise click.ClickException("Choose exactly one: --dry-run or --confirm.")

    users = _dev_test_cleanup_candidates()
    summary = {"users": len(users), "projects": 0, "pairs": 0, "scan_logs": 0, "payment_orders": 0}
    for user in users:
        projects = Project.query.filter_by(owner_user_id=user.id).all()
        summary["projects"] += len(projects)
        summary["pairs"] += sum(ProjectPair.query.filter_by(project_id=project.id).count() for project in projects)
        summary["scan_logs"] += ScanLog.query.filter_by(user_id=user.id).count()
        summary["payment_orders"] += PaymentOrder.query.filter_by(user_id=user.id).count()

    if dry_run:
        return summary

    for user in users:
        for project in Project.query.filter_by(owner_user_id=user.id).all():
            _delete_project_files_and_rows(project)
        ScanLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        PaymentOrder.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        db.session.delete(user)

    config = SystemConfig.query.filter_by(config_key=DEV_TEST_CONFIG_KEY).first()
    if config:
        config.config_value = json.dumps({"user_ids": [], "emails": []})
    db.session.commit()
    return summary


@app.cli.command("seed-dev-test-users")
def seed_dev_test_users_command():
    result = _seed_dev_test_users()
    click.echo(f"Created: {result['created']}")
    click.echo(f"Updated: {result['updated']}")
    click.echo(f"Skipped: {result['skipped']}")


@app.cli.command("delete-dev-test-users")
@click.option("--dry-run", is_flag=True, help="Report what would be deleted without deleting.")
@click.option("--confirm", is_flag=True, help="Delete the seeded development test users and their data.")
def delete_dev_test_users_command(dry_run, confirm):
    result = _delete_dev_test_users(confirm=confirm, dry_run=dry_run)
    prefix = "Would delete" if dry_run else "Deleted"
    click.echo(f"{prefix} users: {result['users']}")
    click.echo(f"{prefix} projects: {result['projects']}")
    click.echo(f"{prefix} pairs: {result['pairs']}")
    click.echo(f"{prefix} scan logs: {result['scan_logs']}")
    click.echo(f"{prefix} payment orders: {result['payment_orders']}")

# --------------------------------------------------------------------------------------------
# CV/QR functions (same as before)
# --------------------------------------------------------------------------------------------
# Per-file upload limits (P0D) - env-overridable, same defaults as before.
MAX_IMAGE_SIZE = int(os.environ.get("MAX_IMAGE_UPLOAD_BYTES", 50 * 1024 * 1024))
MAX_VIDEO_SIZE = int(os.environ.get("MAX_VIDEO_UPLOAD_BYTES", 1 * 1024 * 1024 * 1024))
MAX_IMAGE_DIMENSION_PX = int(os.environ.get("MAX_IMAGE_DIMENSION_PX", 8000))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", 40_000_000))
# Optional; unset/0 disables the duration check entirely.
MAX_VIDEO_DURATION_SECONDS = int(os.environ.get("MAX_VIDEO_DURATION_SECONDS", "0") or "0") or None

# Whole-request body cap. Left unset by default (Flask's existing, unchanged
# behavior) since a single legitimate multi-pair upload can legitimately
# approach MAX_VIDEO_SIZE * pairs-per-project; only apply a cap when an
# operator explicitly opts in via env (see .env.example).
_max_content_length_env = os.environ.get("MAX_CONTENT_LENGTH")
if _max_content_length_env:
    app.config["MAX_CONTENT_LENGTH"] = int(_max_content_length_env)

MAX_WORKERS = min(8, (os.cpu_count() or 4))

ORB_MAX_DIM = 1200
DETECT_MAX_DIM = 960

# New: all stored target image variants
FEATURE_TAGS = ("n", "fx", "fy", "fxy", "r90", "r270")

# Mobile-friendly detection values
QUICK_TOPK = 5
QUICK_DESC_LIMIT = 500        # raised from 200 — more descriptors = better spatial coverage

MIN_TEST_KP = 10
MIN_GOOD_MATCHES = 8          # raised from 7 — need more matches before trusting homography
RANSAC_REPROJ = 5.0           # tightened from 8.0 — fewer false inliers
MIN_INLIERS_ABS = 8           # raised from 6 — 5 inliers produced degenerate H
MIN_INLIERS_RATIO = 0.30
# Cap on how high MIN_INLIERS_RATIO * total_good can push the required-inlier bar — a
# marker with a genuinely large unique-correspondence count (highly textured, well-lit)
# should not need more than this many inliers just because "total" is large. Without a
# cap, a false-positive-prone but well-textured marker could demand an inlier count that
# even its own best possible detection can't consistently clear.
MAX_INLIERS_REQUIRED = 40

_tls = threading.local()
_fast_bf = cv2.BFMatcher(cv2.NORM_HAMMING)

def _orb():
    o = getattr(_tls, "orb", None)
    if o is None:
        o = cv2.ORB_create(
            nfeatures=2400,
            fastThreshold=6,
            scaleFactor=1.2,
            nlevels=10,
            edgeThreshold=31,  # must match _orb_detect — descriptors are incompatible otherwise
            patchSize=31       # must match _orb_detect — ORB descriptor patch size
        )
        _tls.orb = o
    return o


def _orb_detect():
    """Lightweight ORB for fast live detection (used in detect routes).

    patchSize and edgeThreshold MUST match _orb() — ORB descriptors are only
    comparable when computed with the same patch geometry.
    """
    d = getattr(_tls, "orb_detect", None)
    if d is None:
        d = cv2.ORB_create(
            nfeatures=600,
            fastThreshold=10,  # slightly relaxed for mobile frames with varying lighting
            scaleFactor=1.2,
            nlevels=8,         # more pyramid levels = better scale tolerance
            edgeThreshold=31,  # matches _orb()
            patchSize=31       # matches _orb() — CRITICAL for descriptor compatibility
        )
        _tls.orb_detect = d
    return d

def _too_big(file_storage, max_bytes: int) -> bool:
    try:
        if file_storage.content_length is not None:
            return file_storage.content_length > max_bytes
        pos = file_storage.stream.tell()
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(pos, os.SEEK_SET)
        return size > max_bytes
    except Exception:
        return False


# --------------------------------------------------------------------------------------------
# Bootstrap Function (Called from main)
# --------------------------------------------------------------------------------------------
def bootstrap_database():
    """Initialize database with default data"""
    # Create default trial plan
    if SubscriptionPlan.query.filter_by(is_trial_plan=True).first() is None:
        trial_plan = SubscriptionPlan(
            plan_name="Free Trial",
            plan_description="Free trial with limited features",
            plan_amount=0.0,
            offer_price=0.0,
            currency="INR",
            duration_type="time",
            duration_value=7,  # 7 months (as per your image)
            trial_days=7,
            total_project_limit=1,
            total_scan_limit=50,
            is_trial_plan=True,
            features_json='["1 project only", "50 scans limit", "Trial access for 7 days"]',
            is_active=True,
            display_order=0
        )
        db.session.add(trial_plan)
    
    # Create Basic and Pro plans
    if SubscriptionPlan.query.filter_by(plan_name="Basic").first() is None:
        basic_plan = SubscriptionPlan(
            plan_name="Basic",
            plan_description="Basic subscription plan",
            plan_amount=499.0,
            offer_price=399.0,
            currency="INR",
            duration_type="time",
            duration_value=6,  # 6 months
            total_project_limit=5,
            total_scan_limit=500,
            is_popular=False,
            features_json='["5 projects", "500 scans", "6 months validity", "Basic support"]',
            is_active=True,
            display_order=1
        )
        db.session.add(basic_plan)
    
    if SubscriptionPlan.query.filter_by(plan_name="Pro").first() is None:
        pro_plan = SubscriptionPlan(
            plan_name="Pro",
            plan_description="Professional subscription plan",
            plan_amount=999.0,
            offer_price=799.0,
            currency="INR",
            duration_type="time",
            duration_value=12,  # 1 year
            total_project_limit=20,
            total_scan_limit=2000,
            is_popular=True,
            features_json='["20 projects", "2000 scans", "1 year validity", "Priority support", "Advanced features"]',
            is_active=True,
            display_order=2
        )
        db.session.add(pro_plan)
    
    # Create initial super admin - only when explicitly enabled via env, with
    # no default credentials. See _maybe_create_bootstrap_admin.
    _maybe_create_bootstrap_admin()
    
    # Create default system config
    if SystemConfig.query.count() == 0:
        default_configs = [
            ("free_trial_projects", "1", "integer", "Free trial project limit"),
            ("free_trial_scans", "50", "integer", "Free trial scan limit"),
            ("free_trial_days", "7", "integer", "Free trial duration in days"),
            ("razorpay_enabled", "true", "boolean", "Enable Razorpay payments"),
            ("currency", "INR", "string", "Default currency"),
            ("site_name", "ScanStory AR", "string", "Website name"),
            ("site_url", "https://scanstory.com", "string", "Website URL"),
            ("support_email", "support@scanstory.com", "string", "Support email"),
            ("max_login_attempts", "5", "integer", "Maximum login attempts"),
            ("session_timeout", "30", "integer", "Session timeout in minutes"),
        ]
        
        for key, value, config_type, description in default_configs:
            db.session.add(SystemConfig(
                config_key=key,
                config_value=value,
                config_type=config_type,
                description=description
            ))
    
    db.session.commit()
    print("✅ Database bootstrap completed successfully!")
def standardize_uploaded_image(image_path, target_size=1200):
    """Standardize image size and format before feature extraction"""
    try:
        from PIL import Image
        img = Image.open(image_path)
        
        # Convert to RGB (remove alpha channel)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Resize to target size (matching ORB_MAX_DIM)
        if max(img.size) > target_size:
            ratio = target_size / max(img.size)
            new_size = tuple(int(dim * ratio) for dim in img.size)
            img = img.resize(new_size, Image.Resampling.LANCZOS)
            print(f"📸 Resized from {img.size} to {new_size}")
        
        # Save as JPEG
        img.save(image_path, 'JPEG', quality=92)
        return True
    except Exception as e:
        print(f"❌ Image standardization failed: {e}")
        return False
# Logo cache
_logo_cache_lock = threading.Lock()
_logo_rgba = None
_logo_path = os.path.join(LOGOS_DIR, "logo.png")

def _get_logo_rgba():
    global _logo_rgba
    with _logo_cache_lock:
        if _logo_rgba is not None:
            return _logo_rgba
        if os.path.exists(_logo_path):
            try:
                _logo_rgba = Image.open(_logo_path).convert("RGBA")
            except Exception:
                _logo_rgba = None
        return _logo_rgba

@lru_cache(maxsize=64)
def _get_logo_resized(size: int):
    logo = _get_logo_rgba()
    if logo is None:
        return None
    try:
        return logo.resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        return None

def _add_project_name_to_qr_image(qr_image, project_name=None):
    project_name = str(project_name or "").strip()
    if not project_name:
        return qr_image

    if len(project_name) > 22:
        project_name = project_name[:19] + "..."

    qr_w, qr_h = qr_image.size
    header_height = 90
    final_image = Image.new("RGBA", (qr_w, qr_h + header_height), (255, 255, 255, 255))
    final_image.paste(qr_image, (0, header_height), qr_image)

    draw = ImageDraw.Draw(final_image)
    font = None
    for font_name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(font_name, 25)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), project_name, font=font)
    text_w = bbox[2] - bbox[0]
    text_x = max((qr_w - text_w) // 2, 0)

    draw.text((text_x, 18), project_name, font=font, fill=(0, 0, 0, 255))
    return final_image


def _build_qr_download_filename(project):
    raw_name = str(project.name or "project").strip()
    safe_name_chars = []
    last_was_separator = False

    for ch in raw_name:
        if ch.isalnum() or ch in ("-", "_"):
            safe_name_chars.append(ch)
            last_was_separator = False
        elif not last_was_separator:
            safe_name_chars.append("_")
            last_was_separator = True

    safe_name = "".join(safe_name_chars).strip("._-") or "project"
    return f"project_{project.id}_{safe_name}.png"


def generate_basic_qr(data, fill_color, back_color, save_path, project_name=None):
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=8,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color=fill_color, back_color=back_color).convert("RGBA")
        qr_img = _add_project_name_to_qr_image(qr_img, project_name)
        qr_img.save(save_path)
        return True
    except Exception as e:
        print(f"Basic QR generation failed: {e}")
        return False

def generate_custom_qr(data, save_path, project_name=None):
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=8,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        qr_image = qr.make_image(
            fill_color="black",
            back_color="white",
            image_factory=StyledPilImage,
        ).convert("RGBA")
        qr_w, qr_h = qr_image.size
        logo_size = int(min(qr_w, qr_h) * 0.22)
        logo = _get_logo_resized(logo_size)
        if logo is not None:
            pos = ((qr_w - logo_size) // 2, (qr_h - logo_size) // 2)
            plate = Image.new("RGBA", (logo_size + 18, logo_size + 18), (255, 255, 255, 235))
            qr_image.paste(plate, (pos[0] - 9, pos[1] - 9), plate)
            qr_image.paste(logo, pos, logo)
        qr_image = _add_project_name_to_qr_image(qr_image, project_name)
        qr_image.save(save_path)
        return True
    except Exception as e:
        print(f"QR generation failed: {e}")
        return False

def compress_video(video_path):
    try:
        info = ffmpeg.probe(video_path)
        video_streams = [s for s in info["streams"] if s.get("codec_type") == "video"]
        audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
        vcodec = video_streams[0].get("codec_name") if video_streams else None
        acodec = audio_streams[0].get("codec_name") if audio_streams else None
        
        output_path = video_path.replace(".mp4", "_stored.mp4")
        if vcodec == "h264" and (acodec is None or acodec == "aac"):
            (
                ffmpeg
                .input(video_path)
                .output(output_path, **{"c:v": "copy", "c:a": "copy"}, movflags="+faststart")
                .run(overwrite_output=True, quiet=True)
            )
            return output_path
        
        output_path = video_path.replace(".mp4", "_compressed.mp4")
        (
            ffmpeg
            .input(video_path)
            .output(
                output_path,
                vcodec="libx264",
                crf=18,
                preset="veryfast",
                movflags="+faststart",
                pix_fmt="yuv420p"
            )
            .run(overwrite_output=True, quiet=True)
        )
        return output_path
    except Exception as e:
        print(f"Video processing failed: {e}")
        return video_path

# Feature extraction functions
def _kp_to_xy(kp):
    return np.array([k.pt for k in kp], dtype=np.float32) if kp else np.zeros((0, 2), dtype=np.float32)

def _enhance_bgr_for_orb(img):
    """
    Improves ORB keypoints for mobile camera / laptop screen scanning.
    """
    try:
        enhanced = img.copy()

        yuv = cv2.cvtColor(enhanced, cv2.COLOR_BGR2YUV)
        yuv[:, :, 0] = cv2.equalizeHist(yuv[:, :, 0])
        enhanced = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

        kernel = np.array([
            [0, -0.5, 0],
            [-0.5, 3.0, -0.5],
            [0, -0.5, 0]
        ], dtype=np.float32)

        return cv2.filter2D(enhanced, -1, kernel)
    except Exception:
        return img

def _make_variants(gray):
    """
    Creates stored-image variants:
    n, fx, fy, fxy, r90, r270

    Important: variant keypoints are mapped back to the original target image coordinate system.
    """
    h, w = gray.shape[:2]

    def t_id(pts):
        return pts

    def t_fx(pts):
        out = pts.copy()
        out[:, 0] = (w - 1) - out[:, 0]
        return out

    def t_fy(pts):
        out = pts.copy()
        out[:, 1] = (h - 1) - out[:, 1]
        return out

    def t_fxy(pts):
        out = pts.copy()
        out[:, 0] = (w - 1) - out[:, 0]
        out[:, 1] = (h - 1) - out[:, 1]
        return out

    def t_r90(pts):
        out = pts.copy()
        x_r = pts[:, 0].copy()
        y_r = pts[:, 1].copy()
        out[:, 0] = y_r
        out[:, 1] = (h - 1) - x_r
        return out

    def t_r270(pts):
        out = pts.copy()
        x_r = pts[:, 0].copy()
        y_r = pts[:, 1].copy()
        out[:, 0] = (w - 1) - y_r
        out[:, 1] = x_r
        return out

    return [
        ("n", gray, t_id),
        ("fx", cv2.flip(gray, 1), t_fx),
        ("fy", cv2.flip(gray, 0), t_fy),
        ("fxy", cv2.flip(gray, -1), t_fxy),
        ("r90", cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE), t_r90),
        ("r270", cv2.rotate(gray, cv2.ROTATE_90_COUNTERCLOCKWISE), t_r270),
    ]

def extract_features_multi(image_path, save_path, max_dim=ORB_MAX_DIM):
    img = cv2.imread(image_path)
    if img is None:
        raise RuntimeError("Failed to read uploaded target image")

    img = _enhance_bgr_for_orb(img)
    gray0 = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    H0, W0 = gray0.shape[:2]
    scale = 1.0
    m = max(H0, W0)
    if m > max_dim:
        scale = max_dim / float(m)
        gray = cv2.resize(gray0, (int(W0 * scale), int(H0 * scale)), interpolation=cv2.INTER_AREA)
    else:
        gray = gray0
    orb = _orb()
    payload = {}
    for tag, g, to_orig in _make_variants(gray):
        kp, desc = orb.detectAndCompute(g, None)
        if desc is None or kp is None:
            desc = np.zeros((0, 32), dtype=np.uint8)
            kp_xy = np.zeros((0, 2), dtype=np.float32)
        else:
            desc = desc.astype(np.uint8)
            kp_xy = _kp_to_xy(kp)
            kp_xy = to_orig(kp_xy)
            kp_xy = (kp_xy / scale).astype(np.float32)
        payload[f"desc_{tag}"] = desc
        payload[f"kp_{tag}"] = kp_xy
    payload["w"] = np.int32(W0)
    payload["h"] = np.int32(H0)
    np.savez(save_path, **payload)

def _empty_features():
    payload = {"w": 0, "h": 0}

    for tag in FEATURE_TAGS:
        payload[tag] = (
            np.zeros((0, 32), dtype=np.uint8),
            np.zeros((0, 2), dtype=np.float32)
        )

    return payload

@lru_cache(maxsize=2048)
def load_features(project_id: int, pair_index: int = 0):
    try:
        project = None
        try:
            project = Project.query.get(project_id)
        except Exception:
            pass

        if project and project.owner_admin_id:
            npz = os.path.join(ADMIN_FEATURES_DIR, f"{project_id}_{pair_index}.npz")
        else:
            npz = os.path.join(FEATURES_DIR, f"{project_id}_{pair_index}.npz")

        if not os.path.exists(npz):
            return _empty_features()

        data = np.load(npz, allow_pickle=False)

        out = {
            "w": int(data["w"]),
            "h": int(data["h"]),
        }

        for tag in FEATURE_TAGS:
            desc_key = f"desc_{tag}"
            kp_key = f"kp_{tag}"

            if desc_key in data.files and kp_key in data.files:
                out[tag] = (
                    data[desc_key].astype(np.uint8),
                    data[kp_key].astype(np.float32)
                )
            else:
                out[tag] = (
                    np.zeros((0, 32), dtype=np.uint8),
                    np.zeros((0, 2), dtype=np.float32)
                )

        return out

    except Exception as e:
        print(f"❌ load_features error for project={project_id}, pair={pair_index}: {e}")
        return _empty_features()

def _filter_mutual_unique_matches(matches):
    """Enforce unique query/train correspondence (mutual-nearest-match filtering).

    Root cause investigated for the "50-75 good matches but only 6-13 inliers" pattern
    (real-device logs; see also the comment above the evaluate_homography_quality() call
    in detect_init referencing a prior "45 good matches, 6 inliers, required 13" case):
    match_best_variant()'s ratio test has no dedup — a single stored keypoint (trainIdx)
    can be the "good" match for several query keypoints (repetitive local texture), and a
    single query keypoint could likewise appear more than once if matched against more
    than one variant tag before this filter runs. These duplicate/many-to-one
    correspondences inflate the "good_matches" count without adding independent
    geometric evidence, which then inflates evaluate_homography_quality's
    required-inlier bar (MIN_INLIERS_RATIO * total) beyond what the genuinely unique
    correspondence set can ever satisfy — a geometrically sound detection gets rejected
    as "weak_inliers" purely because its own match count was self-inflated.

    Keeps, for each trainIdx, only its lowest-distance match; then, among survivors, for
    each queryIdx keeps only its lowest-distance match. Order-independent w.r.t. which
    pass runs first only when both are enforced fully, hence two passes.
    """
    if not matches:
        return matches
    best_by_train = {}
    for m in matches:
        prev = best_by_train.get(m.trainIdx)
        if prev is None or m.distance < prev.distance:
            best_by_train[m.trainIdx] = m
    best_by_query = {}
    for m in best_by_train.values():
        prev = best_by_query.get(m.queryIdx)
        if prev is None or m.distance < prev.distance:
            best_by_query[m.queryIdx] = m
    return list(best_by_query.values())


def match_best_variant(test_desc, feats, ratio=0.75, diag=None):
    """diag, if given a dict, is updated (only when a new best candidate is found) with
    raw_knn_matches, ratio_accepted_pre_dedup, unique_query_idx, unique_train_idx, and
    deduped_good — the match-count funnel for whichever tag/ratio ends up winning. Purely
    diagnostic (see _log_frame_diag in detect_init); never changes matching behavior."""
    best = ("", [], None)
    if test_desc is None or test_desc.size == 0:
        return best

    # Limit test descriptors only — use FULL stored descriptors for spatial coverage
    td = test_desc[:QUICK_DESC_LIMIT] if test_desc.shape[0] > QUICK_DESC_LIMIT else test_desc

    for tag in FEATURE_TAGS:
        stored_desc, stored_kp = feats.get(tag, (None, None))

        if stored_desc is None or stored_desc.size == 0:
            continue

        # Use all stored descriptors — truncating to 200 lost 90% of the stored image coverage
        sd = stored_desc
        skp = stored_kp[:sd.shape[0]] if stored_kp is not None and stored_kp.shape[0] >= sd.shape[0] else stored_kp

        try:
            # Compute knn ONCE per tag — ratio loop reuses same knn result
            knn = _fast_bf.knnMatch(td, sd, k=2)
        except Exception:
            continue

        for ratio_try in (ratio, min(ratio + 0.05, 0.95), min(ratio + 0.15, 0.95)):
            good_raw = []
            for m_n in knn:
                if len(m_n) != 2:
                    continue
                m, n = m_n
                if m.distance < ratio_try * n.distance:
                    good_raw.append(m)

            # Dedup BEFORE counting/comparing — see _filter_mutual_unique_matches(). Without
            # this, a repetitive-texture marker's duplicate/many-to-one correspondences
            # inflate "good" here, which is exactly the count evaluate_homography_quality's
            # required-inlier formula scales off of.
            good = _filter_mutual_unique_matches(good_raw)

            if len(good) > len(best[1]):
                best = (tag, good, skp if skp is not None else stored_kp)
                if diag is not None:
                    diag["raw_knn_matches"] = len(knn)
                    diag["ratio_accepted_pre_dedup"] = len(good_raw)
                    diag["unique_query_idx"] = len({m.queryIdx for m in good_raw})
                    diag["unique_train_idx"] = len({m.trainIdx for m in good_raw})
                    diag["deduped_good"] = len(good)
                    diag["winning_tag"] = tag
                    diag["winning_ratio"] = ratio_try

            if len(good) >= MIN_GOOD_MATCHES:
                break

    return best

def valid_corners(corners_xy, w, h):
    if corners_xy is None or len(corners_xy) != 4:
        return False
    pts = np.array(corners_xy, dtype=np.float32).reshape(4, 2)
    if not np.isfinite(pts).all():
        return False
    # Convexity — a degenerate homography produces non-convex or self-intersecting quads
    hull = cv2.convexHull(pts.reshape(-1, 1, 2))
    if len(hull) != 4:
        print(f"❌ valid_corners: not convex (hull pts={len(hull)})")
        return False
    area = cv2.contourArea(pts)
    if area < 1500:  # raised from 600 — too small = degenerate / noise homography
        print(f"❌ valid_corners: area too small ({area:.0f})")
        return False
    if area > 0.95 * (w * h):
        print(f"❌ valid_corners: area too large ({area:.0f} vs frame {w*h})")
        return False
    # All 4 corners must lie within a padded frame boundary (allow 15% overshoot)
    pad_x, pad_y = 0.15 * w, 0.15 * h
    for x, y in corners_xy:
        if x < -pad_x or x > w + pad_x or y < -pad_y or y > h + pad_y:
            print(f"❌ valid_corners: corner out of bounds ({x:.1f},{y:.1f}) frame=({w},{h})")
            return False
    return True

def _grid_coverage(points_xy, width, height, grid=3):
    if points_xy is None or len(points_xy) == 0 or width <= 0 or height <= 0:
        return 0, 0.0
    cells = set()
    for x, y in np.asarray(points_xy, dtype=np.float32).reshape(-1, 2):
        if not np.isfinite(x) or not np.isfinite(y):
            continue
        cx = min(grid - 1, max(0, int((float(x) / float(width)) * grid)))
        cy = min(grid - 1, max(0, int((float(y) / float(height)) * grid)))
        cells.add((cx, cy))
    occupied = len(cells)
    return occupied, occupied / float(grid * grid)

def _quad_metrics(corners_xy):
    pts = np.asarray(corners_xy, dtype=np.float32).reshape(4, 2)
    edges = [float(np.linalg.norm(pts[(i + 1) % 4] - pts[i])) for i in range(4)]
    diag1 = float(np.linalg.norm(pts[2] - pts[0]))
    diag2 = float(np.linalg.norm(pts[3] - pts[1]))
    min_edge = max(min(edges), 1.0)
    return {
        "area": float(cv2.contourArea(pts)),
        "edge_ratio": max(edges) / min_edge,
        "diagonal_ratio": max(diag1, diag2) / max(min(diag1, diag2), 1.0),
    }

def evaluate_homography_quality(src_arr, dst_arr, homography, mask, marker_w, marker_h, frame_w, frame_h, scale=1.0):
    """Reject high-match false poses caused by clustered inliers or unstable projection.

    ``reason`` is the original (test-stable) classification string. ``code`` is a
    finer-grained rejection code — distinguishes e.g. an absolute inlier-count
    shortfall from a low inlier *ratio*, or reference-image clustering from
    projected-ROI clustering — without changing any accept/reject threshold or
    the legacy ``reason`` values other call sites already depend on.
    """
    if homography is None or mask is None or src_arr is None or dst_arr is None:
        return False, {"reason": "missing_homography", "code": "missing_homography"}
    inlier_mask = mask.reshape(-1).astype(bool)
    total = int(len(src_arr))
    inliers = int(np.sum(inlier_mask))
    inlier_ratio = inliers / float(max(total, 1))
    min_inliers_needed = min(max(MIN_INLIERS_ABS, int(MIN_INLIERS_RATIO * total)), MAX_INLIERS_REQUIRED)
    if inliers < min_inliers_needed:
        return False, {
            "reason": "weak_inliers", "code": "insufficient_inliers",
            "inliers": inliers, "inlier_ratio": inlier_ratio, "required": min_inliers_needed,
        }
    if inlier_ratio < 0.30:
        return False, {"reason": "weak_inliers", "code": "low_inlier_ratio", "inliers": inliers, "inlier_ratio": inlier_ratio}

    src_in = np.asarray(src_arr[inlier_mask], dtype=np.float32).reshape(-1, 1, 2)
    dst_in = np.asarray(dst_arr[inlier_mask], dtype=np.float32).reshape(-1, 2)
    projected = cv2.perspectiveTransform(src_in, homography).reshape(-1, 2)
    errors = np.linalg.norm(projected - dst_in, axis=1)
    mean_error = float(np.mean(errors)) if len(errors) else float("inf")
    median_error = float(np.median(errors)) if len(errors) else float("inf")
    max_error = float(np.max(errors)) if len(errors) else float("inf")
    detect_frame_w = max(float(frame_w) * float(scale), 1.0)
    detect_frame_h = max(float(frame_h) * float(scale), 1.0)
    normalized_limit = max(8.0, min(detect_frame_w, detect_frame_h) * 0.018)
    if median_error > normalized_limit or mean_error > normalized_limit * 1.6 or max_error > normalized_limit * 4.0:
        return False, {
            "reason": "reprojection_error", "code": "high_reprojection_error",
            "mean_error": mean_error, "median_error": median_error, "max_error": max_error,
        }

    rect = np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32).reshape(-1, 1, 2)
    projected_corners = cv2.perspectiveTransform(rect, homography).reshape(4, 2)
    ref_cells, ref_score = _grid_coverage(src_arr[inlier_mask], marker_w, marker_h, grid=3)
    frame_cells, frame_score = _grid_coverage(dst_arr[inlier_mask], detect_frame_w, detect_frame_h, grid=3)
    try:
        roi_to_marker = cv2.getPerspectiveTransform(
            projected_corners.astype(np.float32),
            np.array([[0, 0], [marker_w, 0], [marker_w, marker_h], [0, marker_h]], dtype=np.float32),
        )
        dst_in_marker_roi = cv2.perspectiveTransform(dst_arr[inlier_mask].reshape(-1, 1, 2), roi_to_marker).reshape(-1, 2)
        roi_cells, roi_score = _grid_coverage(dst_in_marker_roi, marker_w, marker_h, grid=3)
    except Exception:
        roi_cells, roi_score = 0, 0.0
    if ref_cells < 3:
        return False, {
            "reason": "clustered_inliers", "code": "clustered_reference_points",
            "reference_grid_cells": ref_cells,
            "projected_roi_grid_cells": roi_cells,
            "frame_grid_cells": frame_cells,
        }
    if roi_cells < 3:
        return False, {
            "reason": "clustered_inliers", "code": "clustered_roi_points",
            "reference_grid_cells": ref_cells,
            "projected_roi_grid_cells": roi_cells,
            "frame_grid_cells": frame_cells,
        }
    corners = [(float(p[0] / scale), float(p[1] / scale)) for p in projected_corners]
    if not valid_corners(corners, frame_w, frame_h):
        return False, {
            "reason": "invalid_corners", "code": "invalid_quad",
            "corners": corners,
            "inliers": inliers,
            "inlier_ratio": inlier_ratio,
            "reference_grid_cells": ref_cells,
            "projected_roi_grid_cells": roi_cells,
            "frame_grid_cells": frame_cells,
        }
    metrics = _quad_metrics(corners)
    marker_aspect = max(float(marker_w) / max(float(marker_h), 1.0), float(marker_h) / max(float(marker_w), 1.0))
    if metrics["edge_ratio"] > 8.0 or metrics["diagonal_ratio"] > 6.0:
        return False, {"reason": "distorted_quad", "code": "excessive_perspective", **metrics}
    if metrics["edge_ratio"] > marker_aspect * 6.0:
        return False, {"reason": "aspect_distortion", "code": "implausible_scale", **metrics}

    return True, {
        "reason": "accepted",
        "code": "accepted",
        "inliers": inliers,
        "inlier_ratio": inlier_ratio,
        "mean_error": mean_error,
        "median_error": median_error,
        "max_error": max_error,
        "reference_grid_cells": ref_cells,
        "reference_grid_score": ref_score,
        "projected_roi_grid_cells": roi_cells,
        "projected_roi_grid_score": roi_score,
        "frame_grid_cells": frame_cells,
        "frame_grid_score": frame_score,
        "quad_area": metrics["area"],
        "edge_ratio": metrics["edge_ratio"],
        "diagonal_ratio": metrics["diagonal_ratio"],
        "corners": corners,
    }


CANDIDATE_MARGIN_MIN_ABS = 4
CANDIDATE_MARGIN_MIN_RATIO = 0.15


def resolve_candidate_margin(best_good, second_good):
    """Reject an ambiguous winner: two distinct candidate pairs both cleared
    MIN_GOOD_MATCHES but are too close in match count to trust a single pick.

    Does not change what counts as a "good" match for any individual candidate —
    only refuses to choose between two candidates that are both individually
    plausible. Returns (ok, code).
    """
    if second_good < MIN_GOOD_MATCHES:
        return True, None
    margin = best_good - second_good
    required_margin = max(CANDIDATE_MARGIN_MIN_ABS, int(best_good * CANDIDATE_MARGIN_MIN_RATIO))
    if margin < required_margin:
        return False, "candidate_margin_too_small"
    return True, None

def _resize_gray_for_detect(img_bgr, max_dim=DETECT_MAX_DIM):
    h, w = img_bgr.shape[:2]
    m = max(h, w)
    if m <= max_dim:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        return gray, 1.0, w, h
    scale = max_dim / float(m)
    new_w, new_h = int(w * scale), int(h * scale)
    small = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return gray, scale, w, h

def quick_score(test_desc, feats, ratio=0.84, max_checks=QUICK_DESC_LIMIT):
    if test_desc is None or test_desc.size == 0:
        return 0

    td = test_desc[:max_checks] if test_desc.shape[0] > max_checks else test_desc
    best_score = 0

    for tag in FEATURE_TAGS:
        stored_desc, _ = feats.get(tag, (None, None))

        if stored_desc is None or stored_desc.size == 0:
            continue

        try:
            knn = _fast_bf.knnMatch(td, stored_desc, k=2)
        except Exception:
            continue

        good = 0
        for m_n in knn:
            if len(m_n) != 2:
                continue

            m, n = m_n
            if m.distance < ratio * n.distance:
                good += 1
        
        if good > best_score:
            best_score = good

    return best_score

# A crop narrower or shorter than this (in pixels, post-clamp) is treated as degenerate —
# almost certainly a bad/empty selection, not a real marker photograph.
MIN_CROP_ROI_PIXELS = 32
# A stored file's dimensions within this fractional tolerance of marker_processed_width/
# height are treated as "the client already baked this crop into the pixels" — see
# extract_marker_roi().
CROP_ALREADY_APPLIED_TOLERANCE = 0.02


def extract_marker_roi(image_path, marker_meta):
    """Apply EXIF/orientation normalization, then — when marker_meta describes a crop —
    return ONLY the selected ROI as a BGR numpy array. This becomes the reference
    coordinate system feature extraction, homography, and returned marker corners must use
    from here on (see evaluate_homography_quality / detect_init, which already receive
    stored reference w/h from the .npz this function's caller writes).

    Never silently guesses: every decision this function makes (already-cropped detected,
    clamped, rejected as too small, invalid fraction, full_image mode) is recorded in the
    returned diagnostics dict — nothing here falls back to the full image without a logged
    reason.

    marker_meta is a plain dict with the same keys _parse_marker_meta()/ProjectPair use:
    mode, crop_x, crop_y, crop_width, crop_height, processed_width, processed_height
    (processed_width/height are optional — used only for the already-cropped check).

    Returns (bgr_array, diagnostics_dict). bgr_array is never None — callers always get a
    valid image back, cropped or not, per diagnostics['crop_applied'].
    """
    diag = {
        "crop_applied": False,
        "clamped": False,
        "fallback_reason": None,
        "marker_mode": (marker_meta or {}).get("mode", "full_image"),
    }
    with Image.open(image_path) as raw:
        diag["original_decoded_w"], diag["original_decoded_h"] = raw.size
        corrected = ImageOps.exif_transpose(raw)
        if corrected.mode != "RGB":
            corrected = corrected.convert("RGB")
        actual_w, actual_h = corrected.size
        diag["orientation_corrected_w"], diag["orientation_corrected_h"] = actual_w, actual_h
        bgr = np.array(corrected)[:, :, ::-1].copy()

    if diag["marker_mode"] != "crop":
        diag["fallback_reason"] = "mode_full_image"
        diag["final_w"], diag["final_h"] = actual_w, actual_h
        return bgr, diag

    processed_w = int((marker_meta or {}).get("processed_width") or 0)
    processed_h = int((marker_meta or {}).get("processed_height") or 0)
    if processed_w > 0 and processed_h > 0:
        w_tol = max(2, processed_w * CROP_ALREADY_APPLIED_TOLERANCE)
        h_tol = max(2, processed_h * CROP_ALREADY_APPLIED_TOLERANCE)
        if abs(actual_w - processed_w) <= w_tol and abs(actual_h - processed_h) <= h_tol:
            # The stored file's dimensions already match the client's post-crop canvas
            # output (see drawCroppedMarkerToCanvas/renderMarkerBlob in
            # user_create_project.html) — the crop is already baked into these pixels.
            # Cropping again with the same normalized fractions would double-crop.
            diag["fallback_reason"] = "already_cropped_client_side"
            diag["final_w"], diag["final_h"] = actual_w, actual_h
            return bgr, diag

    try:
        # `... or default` would silently turn an explicit, invalid 0.0 crop_width/height
        # into the 1.0 default — use "is None" so an explicit 0.0 is preserved and caught
        # by the crop_w <= 0 check below instead of being masked.
        meta = marker_meta or {}
        crop_x = float(meta["crop_x"] if meta.get("crop_x") is not None else 0.0)
        crop_y = float(meta["crop_y"] if meta.get("crop_y") is not None else 0.0)
        crop_w = float(meta["crop_width"] if meta.get("crop_width") is not None else 1.0)
        crop_h = float(meta["crop_height"] if meta.get("crop_height") is not None else 1.0)
    except (TypeError, ValueError):
        diag["fallback_reason"] = "invalid_crop_fraction"
        diag["final_w"], diag["final_h"] = actual_w, actual_h
        return bgr, diag

    if crop_w <= 0 or crop_h <= 0 or crop_x < 0 or crop_y < 0 or not all(
        np.isfinite(v) for v in (crop_x, crop_y, crop_w, crop_h)
    ):
        diag["fallback_reason"] = "invalid_crop_fraction"
        diag["final_w"], diag["final_h"] = actual_w, actual_h
        return bgr, diag

    px = int(round(crop_x * actual_w))
    py = int(round(crop_y * actual_h))
    pw = int(round(crop_w * actual_w))
    ph = int(round(crop_h * actual_h))
    diag["normalized_roi"] = [crop_x, crop_y, crop_w, crop_h]
    diag["calculated_pixel_roi"] = [px, py, pw, ph]

    clamped_px = max(0, min(px, actual_w - 1))
    clamped_py = max(0, min(py, actual_h - 1))
    clamped_pw = max(1, min(pw, actual_w - clamped_px))
    clamped_ph = max(1, min(ph, actual_h - clamped_py))
    if (clamped_px, clamped_py, clamped_pw, clamped_ph) != (px, py, pw, ph):
        diag["clamped"] = True
    diag["clamped_pixel_roi"] = [clamped_px, clamped_py, clamped_pw, clamped_ph]

    if clamped_pw < MIN_CROP_ROI_PIXELS or clamped_ph < MIN_CROP_ROI_PIXELS:
        diag["fallback_reason"] = f"crop_too_small ({clamped_pw}x{clamped_ph} < {MIN_CROP_ROI_PIXELS}px)"
        diag["final_w"], diag["final_h"] = actual_w, actual_h
        return bgr, diag

    roi = bgr[clamped_py:clamped_py + clamped_ph, clamped_px:clamped_px + clamped_pw]
    diag["crop_applied"] = True
    diag["final_w"], diag["final_h"] = clamped_pw, clamped_ph
    return roi, diag


def make_feature_working_jpeg(src_path: str, out_path: str, max_dim: int = ORB_MAX_DIM, jpeg_quality: int = 92, marker_meta=None) -> str:
    """marker_meta is optional — omit it (the default) to preserve exact prior behavior
    (whole-image ORB input). Passing marker_meta={"mode": "crop", ...} crops to the
    selected ROI (see extract_marker_roi()) before the resize below, so the crop is
    established before any resize/enhancement/ORB extraction happens, per the
    recognition-stability investigation into background/table clutter in stored features."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if marker_meta is not None:
        img, roi_diag = extract_marker_roi(src_path, marker_meta)
        print(f"🔲 ROI diagnostics ({src_path}): {roi_diag}")
    else:
        img = cv2.imread(src_path, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("Bad image for feature working jpeg")
    h, w = img.shape[:2]
    m = max(h, w)
    if m > max_dim:
        scale = max_dim / float(m)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(out_path, img, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
    return out_path


def _npz_reference_summary(npz_path):
    """previous/new reference dims + keypoint count for the rebuild report below — the
    'n' (non-mirrored, non-rotated) variant is the representative keypoint count."""
    if not os.path.exists(npz_path):
        return {"reference_w": None, "reference_h": None, "keypoint_count": None}
    try:
        data = np.load(npz_path, allow_pickle=True)
        return {
            "reference_w": int(data["w"]) if "w" in data else None,
            "reference_h": int(data["h"]) if "h" in data else None,
            "keypoint_count": int(data["desc_n"].shape[0]) if "desc_n" in data else None,
        }
    except Exception as e:
        return {"reference_w": None, "reference_h": None, "keypoint_count": None, "read_error": str(e)}


def rebuild_pair_features(project_id: int, pair_index: int, admin: bool = False, apply_legacy_roi: bool = False):
    """Safe, idempotent feature rebuild for ONE existing pair.

    Default (apply_legacy_roi=False): extracts from the EXACT stored image pixels,
    unchanged — this matches normal upload/reprocessing behavior. The normal upload path
    already renders the user-selected ROI into a canvas and uploads those pixels (see
    drawCroppedMarkerToCanvas/renderMarkerBlob in user_create_project.html); marker_crop_x/
    y/width/height describe a crop ALREADY baked into the stored image and must never be
    applied again — doing so double-crops (the real regression this rebuilt: project 40
    went from a genuine 641x1200 marker to a sliver of it, ~245x644, entirely from
    reapplying its own already-applied crop metadata).

    apply_legacy_roi=True is an explicit, narrow escape hatch for pairs confirmed (by
    inspection, not assumption) to predate the canvas-crop pipeline — where the stored
    image genuinely IS the full uncropped upload and marker_crop_* genuinely does still
    need applying once. Prints a loud warning every time it's used, since applying it to
    an already-cropped pair reproduces the exact regression this function exists to fix.

    Backs up the existing .npz before replacing it either way, and reports previous/new
    reference dimensions + keypoint count. Never touches the pair's uploaded source image
    (image_filename) or its video file. Rerunning with the SAME apply_legacy_roi value
    against the same stored image is deterministic (same inputs -> same outputs).
    """
    images_dir = ADMIN_IMAGES_DIR if admin else IMAGES_DIR
    features_dir = ADMIN_FEATURES_DIR if admin else FEATURES_DIR

    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=pair_index).first()
    if pair is None:
        raise ValueError(f"No ProjectPair found for project_id={project_id} pair_index={pair_index}")
    if not pair.image_filename:
        raise ValueError(f"ProjectPair project_id={project_id} pair_index={pair_index} has no stored image_filename")

    img_path = os.path.join(images_dir, pair.image_filename)
    if not os.path.exists(img_path):
        raise FileNotFoundError(f"Source image not found on disk: {img_path}")

    npz_path = os.path.join(features_dir, f"{project_id}_{pair_index}.npz")
    report = {
        "project_id": project_id,
        "pair_index": pair_index,
        "pair_id": pair.id,
        "image_filename": pair.image_filename,
        "npz_path": npz_path,
        "apply_legacy_roi": apply_legacy_roi,
    }
    report["previous"] = _npz_reference_summary(npz_path)

    backup_path = None
    if os.path.exists(npz_path):
        backup_path = f"{npz_path}.bak.{int(time.time())}"
        shutil.copy2(npz_path, backup_path)
    report["backup_path"] = backup_path

    marker_meta = None
    if apply_legacy_roi:
        print(
            f"⚠️⚠️⚠️ LEGACY ROI REPAIR REQUESTED for project_id={project_id} pair_index={pair_index} "
            f"— applying marker_crop_x/y/width/height to the STORED image. This is only correct if "
            f"this pair's stored image predates the client-side crop-baking pipeline (i.e. it is the "
            f"full, uncropped upload). Applying this to an ALREADY-cropped pair double-crops it — "
            f"verify before running this."
        )
        marker_meta = {
            "mode": pair.marker_mode,
            "crop_x": pair.marker_crop_x,
            "crop_y": pair.marker_crop_y,
            "crop_width": pair.marker_crop_width,
            "crop_height": pair.marker_crop_height,
            "processed_width": pair.marker_processed_width,
            "processed_height": pair.marker_processed_height,
        }
    report["marker_meta"] = marker_meta

    work_img_path = os.path.join(images_dir, f"{project_id}_{pair_index}_rebuild_work.jpg")
    try:
        make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92, marker_meta=marker_meta)
        extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
    finally:
        try:
            if os.path.exists(work_img_path):
                os.remove(work_img_path)
        except Exception:
            pass

    report["new"] = _npz_reference_summary(npz_path)
    load_features.cache_clear()  # drop the stale cached entry so the running server picks up the rebuild immediately
    return report


@app.cli.command("rebuild-pair-features")
@click.option("--project-id", type=int, required=True, help="Project ID owning the pair.")
@click.option("--pair-index", type=int, required=True, help="Pair index within the project (NOT the ProjectPair.id primary key).")
@click.option("--admin", is_flag=True, help="Use admin image/feature directories instead of user directories.")
@click.option(
    "--apply-legacy-roi", is_flag=True,
    help="DANGEROUS — only for pairs confirmed to predate the client-side crop-baking "
         "pipeline (stored image is genuinely the full uncropped upload). Applies "
         "marker_crop_x/y/width/height to the stored image. Omit this flag for normal "
         "rebuilds — the default extracts from the exact stored pixels, no crop applied, "
         "which is correct for every pair uploaded through the current upload flow.",
)
def rebuild_pair_features_command(project_id, pair_index, admin, apply_legacy_roi):
    """Rebuild ONE pair's ORB features from its stored image.

    Default: extracts from the exact stored image pixels — no crop coordinates applied.
    Use --apply-legacy-roi only for a pair you've confirmed still needs its stored crop
    metadata applied (an old upload that predates canvas-side crop baking); applying it to
    an already-cropped pair double-crops it (see: project 40 regression, 641x1200 ->
    245x644).

    Example (project 39, ProjectPair.id=49): first resolve pair_index via
    `ProjectPair.query.get(49).pair_index` — this command takes project_id + pair_index
    (the pair's position within the project), not the ProjectPair primary key.
    """
    report = rebuild_pair_features(project_id, pair_index, admin=admin, apply_legacy_roi=apply_legacy_roi)
    click.echo(f"Pair: project_id={report['project_id']} pair_index={report['pair_index']} pair_id={report['pair_id']}")
    click.echo(f"Image: {report['image_filename']}")
    click.echo(f"Legacy ROI applied: {report['apply_legacy_roi']}")
    click.echo(f"Marker meta: {report['marker_meta']}")
    click.echo(f"Backup: {report['backup_path'] or '(no existing npz to back up)'}")
    click.echo(f"Previous reference: {report['previous']}")
    click.echo(f"New reference:      {report['new']}")


def _process_pair_upload(project_id: int, i: int, image_file, video_file):
    img_filename = f"{project_id}_{i}.jpg"
    img_path = os.path.join(IMAGES_DIR, img_filename)
    image_file.save(img_path)
    
    vid_ext = os.path.splitext(video_file.filename or "")[1].lower() or ".mp4"
    vid_filename = f"{project_id}_{i}{vid_ext}"
    vid_path = os.path.join(VIDEOS_DIR, vid_filename)
    video_file.save(vid_path)
    
    # Generate .npz file for features extraction
    work_img_path = os.path.join(IMAGES_DIR, f"{project_id}_{i}_work.jpg")
    npz_path = os.path.join(FEATURES_DIR, f"{project_id}_{i}.npz")
    
    try:
        make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92)
        extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
    finally:
        try:
            if os.path.exists(work_img_path):
                os.remove(work_img_path)
        except Exception:
            pass
    
    return {
        "pair_index": i,
        "image_filename": img_filename,
        "video_filename": vid_filename,
        "image_path": f"/image/{project_id}/{i}"
    }

def _process_pair_upload_simple(project_id: int, i: int, image_file, video_file):
    """Simple version without database operations"""
    # Save image
    img_filename = f"{project_id}_{i}.jpg"
    img_path = os.path.join(IMAGES_DIR, img_filename)
    image_file.save(img_path)
    
    # Save video
    vid_ext = os.path.splitext(video_file.filename or "")[1].lower() or ".mp4"
    vid_filename = f"{project_id}_{i}{vid_ext}"
    vid_path = os.path.join(VIDEOS_DIR, vid_filename)
    video_file.save(vid_path)
    
    # Generate features file (.npz)
    work_img_path = os.path.join(IMAGES_DIR, f"{project_id}_{i}_work.jpg")
    npz_path = os.path.join(FEATURES_DIR, f"{project_id}_{i}.npz")
    
    try:
        make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92)
        extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
    finally:
        try:
            if os.path.exists(work_img_path):
                os.remove(work_img_path)
        except Exception:
            pass
    
    return {
        "pair_index": i,
        "image_filename": img_filename,
        "video_filename": vid_filename,
        "image_path": f"/image/{project_id}/{i}"
    }

# --------------------------------------------------------------------------------------------
# USER ROUTES
# --------------------------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def landing():
    # Fetch only active plans for landing page, ordered by display order
    plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.display_order.asc()).all()
    return render_template("user/landing.html", plans=plans)

@app.route("/terms")
def terms_page():
    """Terms and Conditions page"""
    return render_template("user/terms.html")

@app.route("/privacy")
def privacy_page():
    return render_template("user/privacy_policy.html")

_LANDING_VIDEOS = {
    "demo": "demo.mp4",
    "educ": "educ.mp4",
    "art": "art.mp4",
    "card": "card.mp4",
}

@app.route("/media/<name>")
def serve_landing_video(name):
    filename = _LANDING_VIDEOS.get(name)
    if not filename:
        abort(404)
    videos_path = os.path.join(app.root_path, "static", "videos")
    response = send_from_directory(videos_path, filename, mimetype="video/mp4")
    response.headers["Content-Disposition"] = "inline"
    return _apply_public_immutable_cache(response)



BLOG_ARTICLES = {
    "augmented-reality-transforming-marketing-customer-engagement": {
        "title": "How Augmented Reality Is Transforming Marketing and Customer Engagement",
        "meta_title": "Augmented Reality Marketing | How AR Improves Customer Engagement",
        "meta_description": "Learn how augmented reality marketing helps brands turn static ads, packaging, cards, and print materials into interactive customer experiences.",
        "category": "AR Marketing",
        "primary_keyword": "augmented reality marketing",
        "read_time": "12 min read",
        "date_published": "2026-06-08",
        "date_modified": "2026-06-08",
        "intro": "Augmented reality marketing helps brands move beyond static content by turning printed materials, packaging, posters, cards, and product visuals into interactive digital experiences.",
        "sections": [
            {
                "heading": "What is augmented reality marketing?",
                "body": "Augmented reality marketing uses AR technology to make brand communication more interactive. Instead of showing only a printed image or static advertisement, brands can allow users to scan a QR code and view a video overlay directly on top of a photo, card, brochure, poster, or product label."
            },
            {
                "heading": "Why AR increases customer engagement",
                "body": "AR creates participation. When a customer scans a QR code and sees a product story, founder message, event memory, or campaign video appear over a real image, the brand experience becomes more memorable."
            },
            {
                "heading": "How ScanStory helps",
                "body": "ScanStory works as a no-code WebAR platform. Users upload a photo and video, generate an AR QR code, and share or print it. Viewers scan the QR code and point their camera at the image to watch the AR video overlay."
            }
        ],
        "faqs": [
            {
                "question": "Do customers need to install an app?",
                "answer": "No. ScanStory runs inside the mobile browser, so users do not need to install an app."
            },
            {
                "question": "Can AR marketing be used for business cards?",
                "answer": "Yes. A business card can be linked with an introduction video, portfolio video, or brand message."
            }
        ],
        "related": [
            "interactive-qr-codes-future-digital-brand-experiences",
            "smart-packaging-ar-product-communication",
            "ai-ar-qr-codes-future-interactive-experiences"
        ]
    },

    "interactive-qr-codes-future-digital-brand-experiences": {
        "title": "Interactive QR Codes: The Future of Digital Brand Experiences",
        "meta_title": "Interactive QR Codes | Future of Digital Brand Experiences",
        "meta_description": "Understand how interactive QR codes are evolving from simple links into AR-powered brand experience gateways.",
        "category": "Interactive QR Codes",
        "primary_keyword": "interactive QR codes",
        "read_time": "11 min read",
        "date_published": "2026-06-08",
        "date_modified": "2026-06-08",
        "intro": "QR codes are no longer limited to opening websites or payment pages. With WebAR, a QR code can become the entry point to an interactive brand experience.",
        "sections": [
            {
                "heading": "What makes a QR code interactive?",
                "body": "A normal QR code only sends users to a link. An interactive QR code creates an experience after scanning. In ScanStory, the QR code opens the AR scanner page and lets the user scan the target image to see a video overlay."
            },
            {
                "heading": "QR code to AR video workflow",
                "body": "The workflow is simple: upload a photo, attach a video, generate the QR code, print or share the QR code, and allow viewers to scan and watch the video appear over the image."
            },
            {
                "heading": "Best use cases",
                "body": "Interactive QR codes can be used for product packaging, business cards, wedding invitations, posters, brochures, education cards, and event campaigns."
            }
        ],
        "faqs": [
            {
                "question": "What is a QR code to AR video?",
                "answer": "It is a QR code that opens an AR scanner page and lets users view a linked video overlay on a target image."
            },
            {
                "question": "Can interactive QR codes work without an app?",
                "answer": "Yes. ScanStory uses WebAR, so users can access the experience from a mobile browser."
            }
        ],
        "related": [
            "augmented-reality-transforming-marketing-customer-engagement",
            "smart-packaging-ar-product-communication",
            "ai-ar-qr-codes-future-interactive-experiences"
        ]
    },

    "smart-packaging-ar-product-communication": {
        "title": "Smart Packaging and AR: The Next Evolution of Product Communication",
        "meta_title": "Smart Packaging AR | Augmented Reality for Product Communication",
        "meta_description": "Discover how smart packaging and AR help product brands turn labels, boxes, and packaging into interactive communication channels.",
        "category": "Smart Packaging",
        "primary_keyword": "smart packaging augmented reality",
        "read_time": "11 min read",
        "date_published": "2026-06-08",
        "date_modified": "2026-06-08",
        "intro": "Smart packaging allows brands to use product labels, cartons, and printed packaging as interactive digital touchpoints.",
        "sections": [
            {
                "heading": "What is smart packaging with AR?",
                "body": "Smart packaging with AR means adding digital interaction to physical packaging. A customer scans a QR code, points the camera at the product label or packaging image, and sees a video overlay connected to that product."
            },
            {
                "heading": "Why packaging needs interaction",
                "body": "Product packaging has limited space. AR allows packaging to carry more information without making the physical design crowded."
            },
            {
                "heading": "How ScanStory supports packaging",
                "body": "ScanStory lets brands upload the packaging image and a video, generate a QR code, and attach that QR code to the package. When users scan and point at the product image, the video appears as an AR overlay."
            }
        ],
        "faqs": [
            {
                "question": "Can AR be used on product labels?",
                "answer": "Yes. Product labels can act as target images for AR video overlays using ScanStory."
            },
            {
                "question": "Is AR packaging useful for small businesses?",
                "answer": "Yes. Small brands can use AR packaging to explain products, show demos, and create stronger customer engagement."
            }
        ],
        "related": [
            "interactive-qr-codes-future-digital-brand-experiences",
            "augmented-reality-transforming-marketing-customer-engagement",
            "ai-ar-qr-codes-future-interactive-experiences"
        ]
    },

    "ar-revolutionizing-education-training-knowledge-sharing": {
        "title": "How AR Is Revolutionizing Education, Training, and Knowledge Sharing",
        "meta_title": "AR in Education | Augmented Reality for Learning and Training",
        "meta_description": "See how AR can turn textbooks, manuals, posters, flashcards, and training material into scan-to-learn interactive experiences.",
        "category": "AR in Education",
        "primary_keyword": "AR in education and training",
        "read_time": "10 min read",
        "date_published": "2026-06-08",
        "date_modified": "2026-06-08",
        "intro": "Augmented reality in education helps students understand concepts by connecting printed learning material with visual explanations.",
        "sections": [
            {
                "heading": "Why AR helps students learn better",
                "body": "Students often understand faster when they can see a concept in action. AR makes static learning materials more visual, interactive, and engaging."
            },
            {
                "heading": "How AR works in classrooms",
                "body": "Teachers can upload a page, diagram, card, or poster as the target image. Then they attach a video explanation and generate a QR code. Students scan the QR and point the camera at the image to view the video overlay."
            },
            {
                "heading": "Why WebAR is practical for education",
                "body": "WebAR avoids the need for app installation. Students can use a normal mobile browser, which is useful for schools, coaching centres, training institutes, workshops, and STEM learning environments."
            }
        ],
        "faqs": [
            {
                "question": "Can ScanStory be used for textbooks?",
                "answer": "Yes. Teachers can attach videos to textbook pages and allow students to scan and view AR explanations."
            },
            {
                "question": "Do students need special devices?",
                "answer": "No. A mobile phone with a browser and camera is enough."
            }
        ],
        "related": [
            "interactive-qr-codes-future-digital-brand-experiences",
            "augmented-reality-transforming-marketing-customer-engagement",
            "ai-ar-qr-codes-future-interactive-experiences"
        ]
    },

    "ai-ar-qr-codes-future-interactive-experiences": {
        "title": "AI + AR + QR Codes: Building the Future of Interactive Experiences",
        "meta_title": "AI AR QR Codes | Future of Interactive WebAR Experiences",
        "meta_description": "Explore how AI, AR, and QR codes are coming together to create intelligent interactive experiences.",
        "category": "AI + AR + QR",
        "primary_keyword": "AI AR QR code technology",
        "read_time": "10 min read",
        "date_published": "2026-06-08",
        "date_modified": "2026-06-08",
        "intro": "AI, AR, and QR codes are shaping the next generation of interactive digital experiences.",
        "sections": [
            {
                "heading": "Why QR codes are the entry point",
                "body": "QR codes are familiar to users and easy to print on any physical material. They work as a bridge between offline media and online experiences."
            },
            {
                "heading": "How AR adds visual interaction",
                "body": "AR turns the scanned image into an interactive surface. Instead of only opening a link, the user sees a video overlay on the actual image."
            },
            {
                "heading": "Where AI can support AR experiences",
                "body": "AI can help with content recommendations, analytics, campaign insights, image quality checks, user behavior understanding, and future personalization."
            }
        ],
        "faqs": [
            {
                "question": "How do AI, AR, and QR codes work together?",
                "answer": "QR codes open the experience, AR displays interactive content, and AI can support analytics, personalization, and optimization."
            },
            {
                "question": "Can small businesses use AI and AR experiences?",
                "answer": "Yes. No-code WebAR tools like ScanStory make interactive AR experiences easier for small businesses."
            }
        ],
        "related": [
            "interactive-qr-codes-future-digital-brand-experiences",
            "augmented-reality-transforming-marketing-customer-engagement",
            "smart-packaging-ar-product-communication"
        ]
    },

    "myscanstory-seo-aeo-geo-strategy": {
        "title": "SEO, AEO & GEO Strategy for AR and QR-Based Experiences",
        "meta_title": "SEO, AEO and GEO Strategy for WebAR and QR-Based AR Experiences",
        "meta_description": "A growth strategy for building search visibility across Google, Bing, ChatGPT, Gemini, Perplexity, and AI-powered search engines for AR and QR-based experiences.",
        "category": "Featured Strategy",
        "primary_keyword": "SEO AEO GEO strategy for AR",
        "read_time": "15 min read",
        "date_published": "2026-06-08",
        "date_modified": "2026-06-08",
        "intro": "Search is changing. Brands now need visibility not only on Google, but also inside AI-powered search engines and answer engines.",
        "sections": [
            {
                "heading": "What is SEO for ScanStory?",
                "body": "SEO helps ScanStory rank for keywords like WebAR platform, augmented reality platform, AR QR code generator, no-code AR platform, browser-based augmented reality, and QR code to AR video."
            },
            {
                "heading": "What is AEO?",
                "body": "AEO means Answer Engine Optimization. It helps the website answer clear questions such as what is WebAR, how to create AR without an app, and how QR code AR works."
            },
            {
                "heading": "What is GEO?",
                "body": "GEO means Generative Engine Optimization. It helps AI tools understand and mention ScanStory when users ask about AR platforms, WebAR tools, QR-based AR, and no-code AR experience builders."
            }
        ],
        "faqs": [
            {
                "question": "Why does ScanStory need blog articles?",
                "answer": "Blog articles help target long-tail keywords, educate users, and build topical authority for AR, QR code, WebAR, and no-code AR topics."
            },
            {
                "question": "What is the most important SEO step for ScanStory?",
                "answer": "The most important step is to create useful pages for each search intent: homepage for platform keywords, pricing for commercial keywords, blog articles for educational keywords, and use-case pages for industry keywords."
            }
        ],
        "related": [
            "augmented-reality-transforming-marketing-customer-engagement",
            "interactive-qr-codes-future-digital-brand-experiences",
            "ai-ar-qr-codes-future-interactive-experiences"
        ]
    }
}

@app.route("/blog")
def blog_page():
    return render_template("user/blog.html")


@app.route("/blog/<slug>")
def blog_article(slug):
    article = BLOG_ARTICLES.get(slug)

    if not article:
        abort(404)

    return render_template(
        "user/blog_articles/article.html",
        article=article,
        articles=BLOG_ARTICLES,
        slug=slug
    )

@app.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    try:
        # Check if admin is viewing a user's dashboard
        view_user_id = request.args.get("user_id", type=int)
        admin_view = request.args.get("admin_view") == "true"
        
        if admin_view and current_admin():
            # Admin viewing user's dashboard
            user = User.query.get_or_404(view_user_id)
            # Log admin activity
            log_admin_activity(
                current_admin().id,
                "view_user_dashboard",
                f"Viewed dashboard for user: {user.email}"
            )
            print(f"👤 Admin viewing user {user.id} dashboard")
        else:
            # Regular user viewing their own dashboard
            user_id = session.get("user_id")
            if not user_id:
                return redirect(url_for("login"))
            
            user = User.query.get(user_id)
            if not user:
                print(f"DEBUG: User not found in database for id: {user_id}")
                session.pop("user_id", None)
                flash("User account not found. Please register again.", "error")
                return redirect(url_for("login"))

        # Normalize usage counters to avoid None / invalid values
        try:
            user.projects_used = int(user.projects_used or 0)
        except (TypeError, ValueError):
            user.projects_used = 0
        try:
            user.scans_used = int(user.scans_used or 0)
        except (TypeError, ValueError):
            user.scans_used = 0

        if user.is_blocked:
            flash("Your account is blocked. Contact support.", "error")
            if not admin_view:
                session.pop("user_id", None)
            return redirect(url_for("login"))

        dev_test_entitled = has_dev_test_entitlement(user)

        trial = None
        changed = False

        # Handle TRIAL and LIMIT_REACHED users
        if user.subscription_status in ("trial", "limit_reached") and not dev_test_entitled:
            try:
                trial_plan = SubscriptionPlan.query.filter_by(
                    is_trial_plan=True,
                    is_active=True
                ).first()
            except Exception as e:
                print(f"❌ Error fetching trial plan: {e}")
                trial_plan = None

            # Ensure TrialDetails exists
            try:
                trial = TrialDetails.query.filter_by(user_id=user.id).first()
            except Exception as e:
                print(f"❌ Error fetching trial details: {e}")
                trial = None
            
            if not trial:
                try:
                    now = dt.utcnow()
                    days = int(
                        (trial_plan.trial_days if trial_plan else get_system_config("free_trial_days", 7)) or 7
                    )

                    trial = TrialDetails(
                        user_id=user.id,
                        trial_start=now,
                        trial_end=now + timedelta(days=days),
                        trial_project_limit=int(get_system_config("free_trial_projects", 1) or 1),
                        trial_scan_limit=int(get_system_config("free_trial_scans", 50) or 50),
                    )
                    db.session.add(trial)
                    changed = True
                except Exception as e:
                    print(f"❌ Error creating trial details: {e}")

            # Trial expired
            if trial and not trial.is_active and user.subscription_status != "active":
                try:
                    user.subscription_status = "expired"
                    db.session.commit()
                    flash("Your free trial has expired. Please upgrade to continue.", "warning")
                    return redirect(url_for("subscribe_page"))
                except Exception as e:
                    print(f"❌ Error updating trial status: {e}")

            # ✅ Sync limits from trial plan - Mirror exactly what's in plan
            if trial_plan:
                try:
                    # Use exact values from plan, no defaults
                    plan_projects = trial_plan.total_project_limit
                    plan_scans = trial_plan.total_scan_limit

                    if user.subscribed_project_limit != plan_projects:
                        user.subscribed_project_limit = plan_projects
                        changed = True

                    if user.subscribed_scan_limit != plan_scans:
                        user.subscribed_scan_limit = plan_scans
                        changed = True

                    # Keep subscription_id consistent
                    if user.subscription_id != trial_plan.id:
                        user.subscription_id = trial_plan.id
                        changed = True
                except Exception as e:
                    print(f"❌ Error syncing trial limits: {e}")

            # Ensure counters are not None
            try:
                if user.projects_used is None:
                    user.projects_used = 0
                    changed = True

                if user.scans_used is None:
                    user.scans_used = 0
                    changed = True
            except Exception as e:
                print(f"❌ Error checking user counters: {e}")

            # ✅ SAFE AUTO-UNLOCK from limit_reached
            if user.subscription_status == "limit_reached":
                try:
                    remaining_projects = None
                    remaining_scans = None

                    if user.subscribed_project_limit not in (None, 0):
                        remaining_projects = int(user.subscribed_project_limit) - int(user.projects_used or 0)
                    if user.subscribed_scan_limit not in (None, 0):
                        remaining_scans = int(user.subscribed_scan_limit) - int(user.scans_used or 0)

                    if remaining_projects is None or remaining_scans is None or remaining_projects > 0 or remaining_scans > 0:
                        user.subscription_status = "trial"
                        changed = True
                except Exception as e:
                    print(f"❌ Error in auto-unlock: {e}")

        # Handle PAID users
        elif user.subscription_status == "active":
            if user.subscription_expires_at and user.subscription_expires_at < dt.utcnow():
                user.subscription_status = "expired"
                changed = True
                flash("Your subscription has expired. Please renew to continue.", "warning")

        if changed:
            try:
                db.session.commit()
                print(f"DEBUG: Changes committed for user {user.id}")
            except Exception as e:
                print(f"❌ Error committing changes: {e}")

        # Get user projects with sequential display numbers
        try:
            # Get projects ordered by creation date (oldest first for sequential numbering)
            projects = Project.query.filter_by(
                owner_user_id=user.id
            ).order_by(Project.created_at.asc()).all()
            
            # Calculate scan count and display number for each project
            for idx, project in enumerate(projects, 1):
                # Calculate scan count for this project
                project.scan_count = ScanLog.query.filter_by(
                    project_id=project.id,
                    is_successful=True
                ).count()
                
                # Add sequential display number (1, 2, 3... for this user)
                project.display_number = idx
                
                # Also add pairs count for each project (useful in template)
                project.pairs_count = ProjectPair.query.filter_by(project_id=project.id).count()
                
            print(f"DEBUG: Found {len(projects)} projects for user {user.id}")
        except Exception as e:
            print(f"❌ Error fetching projects: {e}")
            projects = []

        return render_template(
            "user/dashboard.html",
            user=user,
            projects=projects,
            trial=trial,
            admin_view=admin_view,  # Pass this to template if needed
            dev_test_entitled=dev_test_entitled,
        )
        
    except Exception as e:
        print(f"❌ FATAL ERROR in dashboard route: {str(e)}")
        print(traceback.format_exc())
        return f"""
        <h1>Internal Server Error</h1>
        <p>Error: {str(e)}</p>
        <p>Please try again or contact support.</p>
        <a href="/">Go to Home</a>
        """
        
    
@app.route("/contact")
def contact_page():
    """Contact support page"""
    return render_template("user/contact.html")

@app.route('/send-contact-email', methods=['POST'])
def send_contact_email():
    try:
        # Get form data
        name = request.form.get('name')
        phone = request.form.get('phone')
        email = request.form.get('email')
        project = request.form.get('project', 'Not specified')
        message = request.form.get('message')
        enquiry_type = request.form.get('enquiry_type', 'general')

        enquiry_labels = {
            'support': 'Technical Support',
            'demo': 'Demo Request',
            'custom_plan': 'Custom Plan / Enterprise',
            'partnership': 'Partnership / Bulk Enquiry',
            'sales': 'Sales Enquiry',
            'other': 'General Enquiry',
        }
        enquiry_label = enquiry_labels.get(enquiry_type, enquiry_type.replace('_', ' ').title())

        # Validate required fields
        if not all([name, phone, email, message]):
            return jsonify({'success': False, 'error': 'Please fill in all required fields'}), 400

        recaptcha_ok, recaptcha_msg = verify_recaptcha_v3("contact")
        if not recaptcha_ok:
            return jsonify({
                "success": False,
                "error": recaptcha_msg
            }), 403    

        # Email content
        subject = f"[{enquiry_label}] Contact Form — {name}"

        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px; background: #f9f9f9;">
            <div style="max-width:600px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,0.08);">
                <div style="background:linear-gradient(135deg,#ff007a,#7000ff);padding:24px 28px;">
                    <h2 style="color:#fff;margin:0;font-size:20px;">New Contact Form Submission</h2>
                    <p style="color:rgba(255,255,255,0.8);margin:6px 0 0;font-size:13px;">ScanStory AR Platform</p>
                </div>
                <div style="padding:24px 28px;">
                    <table style="width:100%;border-collapse:collapse;font-size:14px;">
                        <tr><td style="padding:10px 12px;background:#fafafa;font-weight:700;width:140px;border-bottom:1px solid #eee;">Enquiry Type</td><td style="padding:10px 12px;border-bottom:1px solid #eee;color:#ff007a;font-weight:700;">{enquiry_label}</td></tr>
                        <tr><td style="padding:10px 12px;background:#fafafa;font-weight:700;border-bottom:1px solid #eee;">Name</td><td style="padding:10px 12px;border-bottom:1px solid #eee;">{name}</td></tr>
                        <tr><td style="padding:10px 12px;background:#fafafa;font-weight:700;border-bottom:1px solid #eee;">Phone</td><td style="padding:10px 12px;border-bottom:1px solid #eee;"><a href="tel:{phone}" style="color:#7000ff;">{phone}</a></td></tr>
                        <tr><td style="padding:10px 12px;background:#fafafa;font-weight:700;border-bottom:1px solid #eee;">Email</td><td style="padding:10px 12px;border-bottom:1px solid #eee;"><a href="mailto:{email}" style="color:#7000ff;">{email}</a></td></tr>
                        <tr><td style="padding:10px 12px;background:#fafafa;font-weight:700;border-bottom:1px solid #eee;">Company / Project</td><td style="padding:10px 12px;border-bottom:1px solid #eee;">{project}</td></tr>
                        <tr><td style="padding:10px 12px;background:#fafafa;font-weight:700;vertical-align:top;">Message</td><td style="padding:10px 12px;">{message}</td></tr>
                    </table>
                </div>
                <div style="padding:14px 28px;background:#f5f5f5;text-align:center;">
                    <p style="color:#999;font-size:11px;margin:0;">Sent from ScanStory contact form · myscanstory.com</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        # Send email using your existing SMTP function
        send_email_smtp(
            to_email="contact@myscanstory.com",
            subject=subject,
            html_body=html_body
        )
        
        # ✅ Return JSON success response
        return jsonify({'success': True, 'message': 'Email sent successfully'})
        
    except Exception as e:
        print(f"Contact form error: {e}")
        # ✅ Return JSON error response
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route("/profile")
@login_required
def user_profile():
    user = current_user()
    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    projects = Project.query.filter_by(owner_user_id=user.id).order_by(Project.created_at.desc()).all()
    
    return render_template(
        "user/profile.html",
        user=user,
        trial=trial,
        projects=projects,
        get_system_config=get_system_config
    )

@app.route("/projects", methods=["GET"])
@login_required
def projects_page():
    user = current_user()
    projects = (
        Project.query
        .filter_by(owner_user_id=user.id)
        .order_by(Project.created_at.asc())  # Changed to asc for sequential numbering
        .all()
    )
    
    # Attach pairs count and display number
    for idx, p in enumerate(projects, 1):
        p.pairs_count = ProjectPair.query.filter_by(project_id=p.id).count()
        p.display_number = idx  # Add sequential number
    
    return render_template(
        "user/projects.html",
        user=user,
        projects=projects
    )

@app.route("/projects/<int:project_id>/qr")
@login_required
def download_project_qr(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not project or project.owner_user_id != user.id:
        abort(404)
    if not project.qr_code_filename:
        abort(404)
    return send_from_directory(
        QR_DIR,
        project.qr_code_filename,
        as_attachment=True,
        download_name=_build_qr_download_filename(project)
    )

@app.route("/projects/delete/<int:project_id>", methods=["POST"])
@login_required
def user_delete_project(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not project or project.owner_user_id != user.id:
        abort(404)
    
    # Decrement projects count
    user.projects_used = max(0, (user.projects_used or 0) - 1)
    
    _delete_project_files_and_rows(project)
    db.session.commit()
    
    flash("Project deleted successfully.", "success")
    return redirect(url_for("projects_page"))


@app.route("/projects/<int:project_id>/edit", methods=["GET"])
@login_required
def user_edit_project_page(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not project or project.owner_user_id != user.id:
        abort(404)

    pairs = ProjectPair.query.filter_by(project_id=project_id).order_by(ProjectPair.pair_index).all()
    return render_template("user/edit_project.html", project=project, pairs=pairs, user=user)


@app.route("/projects/<int:project_id>/edit", methods=["POST"])
@login_required
def user_edit_project(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not project or project.owner_user_id != user.id:
        abort(404)

    pairs = ProjectPair.query.filter_by(project_id=project_id).order_by(ProjectPair.pair_index).all()
    updated = 0

    for pair in pairs:
        img_key = f"image_{pair.pair_index}"
        vid_key = f"video_{pair.pair_index}"
        new_image = request.files.get(img_key)
        new_video = request.files.get(vid_key)

        if new_image and new_image.filename:
            try:
                img_temp, _img_ext = validate_image(
                    new_image, TMP_UPLOADS_DIR, MAX_IMAGE_SIZE, MAX_IMAGE_DIMENSION_PX, MAX_IMAGE_PIXELS
                )
            except UploadValidationError as exc:
                app.logger.warning(f"Replacement image rejected (pair {pair.pair_index}): {exc.detail}")
                flash(f"Image for pair {pair.pair_index + 1}: {exc.safe_message}", "error")
                return redirect(url_for("user_edit_project_page", project_id=project_id))
            img_path = os.path.join(IMAGES_DIR, pair.image_filename)
            os.replace(img_temp, img_path)  # existing image only replaced after successful validation
            standardize_uploaded_image(img_path, target_size=1200)
            pair.is_processed = False
            pair.processing_status = "uploaded"
            pair.feature_extraction_status = "pending"
            pair.processing_error = None
            updated += 1

        if new_video and new_video.filename:
            try:
                vid_temp, _vid_ext = validate_video(
                    new_video, TMP_UPLOADS_DIR, MAX_VIDEO_SIZE, MAX_VIDEO_DURATION_SECONDS
                )
            except UploadValidationError as exc:
                app.logger.warning(f"Replacement video rejected (pair {pair.pair_index}): {exc.detail}")
                flash(f"Video for pair {pair.pair_index + 1}: {exc.safe_message}", "error")
                return redirect(url_for("user_edit_project_page", project_id=project_id))
            vid_path = os.path.join(VIDEOS_DIR, pair.video_filename)
            os.replace(vid_temp, vid_path)  # existing video only replaced after successful validation
            updated += 1

    db.session.commit()

    if updated == 0:
        flash("No files were replaced. Upload at least one new image or video.", "info")
        return redirect(url_for("user_edit_project_page", project_id=project_id))

    pairs_to_process = [p for p in pairs if not p.is_processed]

    def _reprocess_user_bg(project_id, pairs_data):
        with app.app_context():
            for pd in pairs_data:
                pair_index = pd["pair_index"]
                img_path = os.path.join(IMAGES_DIR, pd["image_filename"])
                work_img_path = os.path.join(IMAGES_DIR, f"{project_id}_{pair_index}_work.jpg")
                npz_path = os.path.join(FEATURES_DIR, f"{project_id}_{pair_index}.npz")
                success = False
                error_msg = None
                try:
                    make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=85)
                    extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
                    success = True
                except Exception as e:
                    error_msg = str(e)
                finally:
                    try:
                        if os.path.exists(work_img_path):
                            os.remove(work_img_path)
                    except Exception:
                        pass

                try:
                    db.engine.dispose()
                    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=pair_index).first()
                    if pair:
                        pair.is_processed = success
                        pair.processing_status = "completed" if success else "failed"
                        pair.feature_extraction_status = "extracted" if success else "failed"
                        pair.processing_error = None if success else error_msg
                        db.session.commit()
                except Exception as db_err:
                    print(f"[USER EDIT REPROCESS DB ERROR] {db_err}")

            load_features.cache_clear()

    if pairs_to_process:
        pairs_data = [
            {
                "pair_index": p.pair_index,
                "image_filename": p.image_filename,
                "video_filename": p.video_filename,
            }
            for p in pairs_to_process
        ]
        t = threading.Thread(target=_reprocess_user_bg, args=(project_id, pairs_data), daemon=True)
        t.start()

    flash("Changes saved. Your ScanStory will be ready in about a minute.", "success")
    return redirect(url_for("projects_page"))


@app.route("/projects/<int:project_id>/reprocess", methods=["POST"])
@login_required
def user_reprocess_project(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not project or project.owner_user_id != user.id:
        abort(404)

    pairs_to_reprocess = ProjectPair.query.filter_by(project_id=project_id).all()
    if not pairs_to_reprocess:
        flash("No pairs found to reprocess.", "error")
        return redirect(url_for("projects_page"))

    for pair in pairs_to_reprocess:
        pair.processing_status = "processing"
        pair.feature_extraction_status = "extracting"
        pair.processing_error = None
    db.session.commit()

    pairs_data = [{"pair_index": p.pair_index, "image_filename": p.image_filename} for p in pairs_to_reprocess]

    def _user_reprocess_bg(project_id, pairs_data):
        with app.app_context():
            for pd in pairs_data:
                pair_index = pd["pair_index"]
                img_path = os.path.join(IMAGES_DIR, pd["image_filename"])
                work_img_path = os.path.join(IMAGES_DIR, f"{project_id}_{pair_index}_work.jpg")
                npz_path = os.path.join(FEATURES_DIR, f"{project_id}_{pair_index}.npz")
                success = False
                error_msg = None
                try:
                    make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=85)
                    extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
                    success = True
                except Exception as e:
                    error_msg = str(e)
                finally:
                    try:
                        if os.path.exists(work_img_path):
                            os.remove(work_img_path)
                    except Exception:
                        pass

                try:
                    db.engine.dispose()
                    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=pair_index).first()
                    if pair:
                        pair.is_processed = success
                        pair.processing_status = "completed" if success else "failed"
                        pair.feature_extraction_status = "extracted" if success else "failed"
                        pair.processing_error = None if success else error_msg
                        db.session.commit()
                except Exception as db_err:
                    print(f"[USER REPROCESS DB ERROR] {db_err}")

            load_features.cache_clear()

    t = threading.Thread(target=_user_reprocess_bg, args=(project_id, pairs_data), daemon=True)
    t.start()

    flash("Reprocessing started. Your QR code stays the same - refresh in a minute to check.", "success")
    return redirect(url_for("projects_page"))


@app.route("/register", methods=["GET", "POST"])
@app.route("/register/", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        plan_id = request.args.get("plan_id", type=int)
        selected_plan = None
        if plan_id:
            selected_plan = SubscriptionPlan.query.get(plan_id)
        return render_template("user/register.html", selected_plan=selected_plan)

    ok, retry_after = _check_rate_limit("register_ip", _rate_limit_key("register"))
    if not ok:
        flash("Too many registration attempts from this network. Please wait and try again.", "error")
        return render_template("user/register.html"), 429

    try:
        email = (request.form.get("email") or "").strip().lower()
        first_name = (request.form.get("first_name") or "").strip()
        last_name = (request.form.get("last_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        password1 = request.form.get("password1") or ""
        password2 = request.form.get("password2") or ""

        recaptcha_ok, recaptcha_msg = verify_recaptcha_v3("register")
        if not recaptcha_ok:
            flash(recaptcha_msg, "error")
            return render_template("user/register.html")

        # Validation
        if not email:
            flash("Email is required.", "error")
            return render_template("user/register.html")
        if password1 != password2:
            flash("Passwords do not match.", "error")
            return render_template("user/register.html")
        if len(password1) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("user/register.html")
        if User.query.filter_by(email=email).first():
            flash("Email is already registered.", "error")
            return render_template("user/register.html")

        # Get trial plan
        trial_plan = SubscriptionPlan.query.filter_by(is_trial_plan=True).first()
        if not trial_plan:
            flash("System configuration error. Please contact support.", "error")
            return render_template("user/register.html")

        # Get values from trial plan - None means unlimited
        free_trial_days = trial_plan.trial_days if trial_plan.trial_days else 7
        free_trial_projects = trial_plan.total_project_limit
        free_trial_scans = trial_plan.total_scan_limit

        # Use one consistent timestamp
        now = dt.utcnow()

        # Create user
        user = User(
            email=email,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            password_hash=generate_password_hash(password1),
            is_verified=False,
            is_blocked=False,
            subscription_id=trial_plan.id,
            subscription_taken_at=now,
            subscription_status="trial",
            subscribed_project_limit=free_trial_projects,
            subscribed_scan_limit=free_trial_scans,
            projects_used=0,
            scans_used=0
        )

        db.session.add(user)
        db.session.commit()

        # Create trial details
        trial = TrialDetails(
            user_id=user.id,
            trial_start=now,
            trial_end=now + timedelta(days=free_trial_days),
            trial_project_limit=free_trial_projects,
            trial_scan_limit=free_trial_scans
        )

        db.session.add(trial)
        db.session.commit()

        # Send verification OTP
        code = _create_otp(email, "verify_email", minutes=2, user_id=user.id)
        otp_rec = _latest_otp(email, "verify_email")
        email_sent = False
        try:
            send_email_verification_otp(email, code, minutes=2)
            email_sent = True
            flash("OTP sent to your email. Please verify to continue.", "success")
        except Exception as e:
            if otp_rec:
                otp_rec.invalidated_at = dt.utcnow()
                db.session.commit()
            flash("Could not send verification email. Please try again later.", "error")

        session["pending_verify_email"] = email
        if otp_rec and email_sent:
            session["pending_verify_challenge_id"] = otp_rec.challenge_id
        return redirect(url_for("verify_email"))

    except Exception as e:
        print(f"❌ Registration error: {str(e)}")
        print(f"❌ Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        
        flash(f"Registration failed: {str(e)}", "error")
        
        return render_template("user/register.html")


@app.route("/verify-email/", methods=["GET", "POST"])
def verify_email():
    email = session.get("pending_verify_email")
    if not email:
        flash("No verification session found. Please register again.", "error")
        return redirect(url_for("register"))
    
    if request.method == "GET":
        return render_template("user/verify_email.html", email=email)
    
    otp = (request.form.get("otp") or "").strip()
    challenge_id = session.get("pending_verify_challenge_id")
    if not _verify_otp(email, "verify_email", otp, challenge_id=challenge_id):
        flash("Verification could not be completed. Please try again or request a new code.", "error")
        return render_template("user/verify_email.html", email=email)
    
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Account not found. Please register again.", "error")
        return redirect(url_for("register"))
    
    user.is_verified = True
    user.email_verified_at = dt.utcnow()
    db.session.commit()
    
    session.pop("pending_verify_email", None)
    session.pop("pending_verify_challenge_id", None)
    flash("Email verified successfully. You can now login.", "success")
    return redirect(url_for("login"))

@app.route("/resend-otp/", methods=["GET"])
def resend_otp():
    email = session.get("pending_verify_email")
    if not email:
        flash("No verification session found.", "error")
        return redirect(url_for("register"))

    ok, retry_after = _check_rate_limit("resend_otp_ip", _rate_limit_key("resend_otp"))
    if not ok:
        flash("Too many code requests from this network. Please wait and try again.", "error")
        return redirect(url_for("verify_email"))
    
    user = User.query.filter_by(email=email).first()
    sent, message = _resend_otp(
        email,
        "verify_email",
        send_email_verification_otp,
        minutes=2,
        user_id=user.id if user else None,
    )
    if sent:
        rec = _latest_otp(email, "verify_email")
        if rec:
            session["pending_verify_challenge_id"] = rec.challenge_id
        flash("A new verification code has been sent if available.", "success")
    else:
        flash(message, "error")
    
    return redirect(url_for("verify_email"))

@app.route("/login/", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        # Already authenticated - don't show a login form. An Admin/Super
        # Admin session must never be sent to the normal-user login page.
        if current_admin():
            return redirect(url_for("admin_dashboard"))
        if current_user():
            return redirect(url_for("dashboard"))
        return render_template("user/login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    ok, retry_after = _check_rate_limit("login_ip", _rate_limit_key("login"))
    if not ok:
        flash("Too many login attempts from this network. Please wait and try again.", "error")
        return render_template("user/login.html"), 429

    user = User.query.filter_by(email=email).first()

    if user:
        # Check temporary lockout from failed attempts
        _MAX_ATTEMPTS = 4
        _LOCKOUT_HOURS = 3
        _lockout_window = dt.utcnow() - timedelta(hours=_LOCKOUT_HOURS)
        _recent_failures = UserLoginActivity.query.filter_by(
            user_id=user.id, is_successful=False
        ).filter(UserLoginActivity.login_at >= _lockout_window).count()

        if _recent_failures >= _MAX_ATTEMPTS:
            _first_fail = UserLoginActivity.query.filter_by(
                user_id=user.id, is_successful=False
            ).filter(
                UserLoginActivity.login_at >= _lockout_window
            ).order_by(UserLoginActivity.login_at.asc()).first()
            _unlock_at = _first_fail.login_at + timedelta(hours=_LOCKOUT_HOURS)
            _remaining = _unlock_at - dt.utcnow()
            _total_mins = max(0, int(_remaining.total_seconds() // 60))
            _hrs = _total_mins // 60
            _mins = _total_mins % 60
            flash(
                f"Account temporarily locked due to multiple failed login attempts. "
                f"Please try again in {_hrs}h {_mins}m.",
                "error",
            )
            return render_template("user/login.html")

    if not user or not check_password_hash(user.password_hash, password):
        if user:
            _fail_entry = UserLoginActivity(
                user_id=user.id,
                ip_address=request.remote_addr or "0.0.0.0",
                user_agent=request.headers.get("User-Agent", ""),
                is_successful=False,
                login_at=dt.utcnow(),
            )
            db.session.add(_fail_entry)
            db.session.commit()
        flash("Invalid email or password.", "error")
        return render_template("user/login.html")

    if user.is_blocked:
        flash("Your account is blocked. Contact support.", "error")
        return render_template("user/login.html")

    # ✅ TRIAL SAFETY + DYNAMIC PLAN SYNC
    dev_test_entitled = has_dev_test_entitlement(user)

    if user.subscription_status == "trial" and not dev_test_entitled:
        changed = False

        # Ensure usage counters are sane
        if user.projects_used is None:
            user.projects_used = 0
            changed = True
        if user.scans_used is None:
            user.scans_used = 0
            changed = True

        # Ensure TrialDetails exists
        trial = TrialDetails.query.filter_by(user_id=user.id).first()
        if not trial:
            now = dt.utcnow()
            trial_plan = SubscriptionPlan.query.filter_by(is_trial_plan=True, is_active=True).first()
            days = int((trial_plan.trial_days if trial_plan else get_system_config("free_trial_days", 7)) or 7)

            trial = TrialDetails(
                user_id=user.id,
                trial_start=now,
                trial_end=now + timedelta(days=days),
                trial_project_limit=int(get_system_config("free_trial_projects", 1) or 1),
                trial_scan_limit=int(get_system_config("free_trial_scans", 50) or 50),
            )
            db.session.add(trial)
            changed = True

        # Trial expired → mark expired
        if not trial.is_active:
            user.subscription_status = "expired"
            db.session.commit()
            flash("Your free trial has expired. Please upgrade to continue.", "warning")
            return redirect(url_for("subscribe_page"))

        # 🔥 SYNC LIMITS FROM TRIAL PLAN (ADMIN EDIT FIX)
        trial_plan = SubscriptionPlan.query.filter_by(is_trial_plan=True, is_active=True).first()
        if trial_plan:
            plan_projects = trial_plan.total_project_limit
            plan_scans = trial_plan.total_scan_limit

            if user.subscribed_project_limit != plan_projects:
                user.subscribed_project_limit = plan_projects
                changed = True

            if user.subscribed_scan_limit != plan_scans:
                user.subscribed_scan_limit = plan_scans
                changed = True

            # Keep subscription_id consistent
            if user.subscription_id != trial_plan.id:
                user.subscription_id = trial_plan.id
                changed = True

        if changed:
            db.session.commit()

    # ✅ ADD THIS BLOCK - Login tracking update
    user.last_login_at = dt.utcnow()
    user.last_login_ip = request.remote_addr
    user.login_count = (user.login_count or 0) + 1
    db.session.commit()
    # ✅ END OF ADDED BLOCK

    # ✅ Login success
    session["user_id"] = user.id
    flash("Login successful.", "success")
    return redirect(url_for("dashboard"))



@app.route("/logout/", methods=["GET"])
def logout():
    logout_user()
    flash("Logged out successfully.", "success")
    return redirect(url_for("landing"))



@app.route("/forgot-password/", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("user/forgot_password.html")
    
    ok, retry_after = _check_rate_limit("forgot_password_ip", _rate_limit_key("forgot_password"))
    if not ok:
        flash("If the email exists, an OTP has been sent.", "success")
        return render_template("user/forgot_password.html"), 429

    email = (request.form.get("email") or "").strip().lower()
    user = User.query.filter_by(email=email).first()
    
    if user:
        try:
            code = _create_otp(email, "reset_password", minutes=2, user_id=user.id)
            otp_rec = _latest_otp(email, "reset_password")
            send_reset_password_otp(email, code, minutes=2)
            if otp_rec:
                session["pending_reset_challenge_id"] = otp_rec.challenge_id
            flash("If the email exists, an OTP has been sent.", "success")
        except Exception as e:
            print(f"❌ Forgot password email error: {e}")
            otp_rec = _latest_otp(email, "reset_password")
            if otp_rec:
                otp_rec.invalidated_at = dt.utcnow()
                db.session.commit()
            flash("If the email exists, an OTP has been sent.", "success")
    else:
        # For security, still show success message even if email doesn't exist
        flash("If the email exists, an OTP has been sent.", "success")
    
    session["pending_reset_email"] = email
    return redirect(url_for("reset_password"))

@app.route("/reset-password/", methods=["GET", "POST"])
def reset_password():
    email = session.get("pending_reset_email")
    if not email:
        flash("Please start from Forgot Password.", "error")
        return redirect(url_for("forgot_password"))
    
    if request.method == "GET":
        return render_template("user/reset_password.html", email=email)
    
    otp = (request.form.get("otp") or "").strip()
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""
    
    if new_password != confirm_password:
        flash("Passwords do not match.", "error")
        return render_template("user/reset_password.html", email=email)
    
    if len(new_password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return render_template("user/reset_password.html", email=email)
    
    if not _verify_otp(email, "reset_password", otp, challenge_id=session.get("pending_reset_challenge_id")):
        flash("Invalid or expired OTP. Password reset could not be completed.", "error")
        return render_template("user/reset_password.html", email=email)
    
    user = User.query.filter_by(email=email).first()
    if user:
        user.password_hash = generate_password_hash(new_password)
        OTPCode.query.filter_by(email=email, purpose="reset_password", is_used=False).filter(
            OTPCode.invalidated_at.is_(None)
        ).update({OTPCode.invalidated_at: dt.utcnow()}, synchronize_session=False)
        db.session.commit()
    
    session.pop("pending_reset_email", None)
    session.pop("pending_reset_challenge_id", None)
    flash("Password updated. Please login.", "success")
    return redirect(url_for("login"))

# --------------------------------------------------------------------------------------------
# Project Creation with Subscription Enforcement
# --------------------------------------------------------------------------------------------
@app.route("/create-project", methods=["GET"])
@login_required
@enforce_subscription
def user_create_project_page():
    user = current_user()
    dev_test_entitled = has_dev_test_entitlement(user)

    # enforce_subscription already checked.
    # This is just an extra safety check (optional)
    if not user.can_create_project and not dev_test_entitled:
        flash("Project limit reached. Please upgrade your plan.", "error")
        return redirect(url_for("subscribe_page"))

    max_pairs_per_project = get_plan_pairs_limit(user)
    if max_pairs_per_project is None and not dev_test_entitled:
        flash("Pairs allowed per project is not configured for your current plan. Please contact admin.", "error")
        return redirect(url_for("subscribe_page"))

    return render_template(
        "user/user_create_project.html",
        user=user,
        max_pairs_per_project=max_pairs_per_project,
        dev_test_entitled=dev_test_entitled,
        video_upload_warnings=VIDEO_UPLOAD_WARNINGS,
        crop_debug_enabled=(
            request.args.get("crop_debug") == "1"
            and (app.config.get("TESTING") or os.environ.get("FLASK_ENV") == "development")
        ),
        upload_debug_enabled=(app.config.get("TESTING") or os.environ.get("FLASK_ENV") == "development"),
    )



@app.route("/upload", methods=["POST"])
@login_required
@enforce_subscription
def handle_upload():
    """Optimized project creation with background processing for MULTIPLE PAIRS"""
    user = current_user()
    dev_test_entitled = has_dev_test_entitlement(user)
    ok, retry_after = _check_rate_limit("upload", _rate_limit_key("upload", user.id))
    if not ok:
        flash("Too many upload attempts. Please wait before starting another upload.", "error")
        return redirect(url_for("user_create_project_page"))

    upload_id = (request.form.get("upload_id") or str(uuid.uuid4())).strip()[:80]
    request_start = time.time()
    _upload_log("UPLOAD REQUEST ENTER", upload_id, user_id=user.id, content_length=request.content_length)
    _upload_log("VIDEO SERVER REQUEST ENTER", upload_id, user_id=user.id, content_length=request.content_length)

    if not user.can_create_project and not dev_test_entitled:
        flash("Project limit reached. Please upgrade your plan.", "error")
        return redirect(url_for("user_create_project_page"))

    t0 = time.time()
    upload_timing = {
        "files_persisted_at": None,
        "jobs_scheduled_at": None,
    }

    # Get project name and uploaded files
    name = request.form.get("name", "Untitled Project")
    images = request.files.getlist("images")
    videos = request.files.getlist("videos")
    _upload_log("UPLOAD BODY READY", upload_id, user_id=user.id, pair_count=len(images), duration_ms=round((time.time() - request_start) * 1000))
    body_ready_at = time.time()
    for i, video_file in enumerate(videos):
        _upload_log(
            "VIDEO SERVER BODY READY",
            upload_id,
            **_video_log_fields(
                video_file,
                user_id=user.id,
                pair_index=i,
                content_length=request.content_length,
                body_duration_ms=round((body_ready_at - request_start) * 1000),
            ),
        )

    # Validation
    _upload_log("UPLOAD VALIDATION START", upload_id, user_id=user.id, pair_count=len(images))
    if not images or not videos or len(images) != len(videos):
        flash("Error: Please upload equal number of images and videos", "error")
        return redirect(url_for("user_create_project_page"))

    # Get max pairs based on subscription plan only
    max_pairs = get_plan_pairs_limit(user)
    if max_pairs is None and not dev_test_entitled:
        flash("Pairs allowed per project is not configured for your current plan. Please contact admin.", "error")
        return redirect(url_for("user_create_project_page"))

    if max_pairs is not None and len(images) > max_pairs:
        flash(f"Your current plan allows maximum {max_pairs} pairs per project.", "error")
        return redirect(url_for("user_create_project_page"))

    try:
        marker_metadata = [_parse_marker_meta(i) for i in range(len(images))]
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("user_create_project_page"))

    # Validate every file from its actual content BEFORE any quota
    # reservation or DB row is created (P0D) - a rejected upload must never
    # consume project/pair quota. All-or-nothing: every pair in the request
    # must validate before any of them are persisted.
    validated_media = []
    try:
        for i, (image_file, video_file) in enumerate(zip(images, videos)):
            try:
                img_temp, img_ext = validate_image(
                    image_file, TMP_UPLOADS_DIR, MAX_IMAGE_SIZE, MAX_IMAGE_DIMENSION_PX, MAX_IMAGE_PIXELS
                )
            except UploadValidationError as exc:
                app.logger.warning(f"Upload rejected (image, pair {i}, upload_id={upload_id}): {exc.detail}")
                raise
            try:
                vid_temp, vid_ext = validate_video(
                    video_file, TMP_UPLOADS_DIR, MAX_VIDEO_SIZE, MAX_VIDEO_DURATION_SECONDS
                )
            except UploadValidationError as exc:
                _safe_remove(img_temp)
                app.logger.warning(f"Upload rejected (video, pair {i}, upload_id={upload_id}): {exc.detail}")
                raise
            validated_media.append({"image_temp": img_temp, "image_ext": img_ext, "video_temp": vid_temp, "video_ext": vid_ext})
    except UploadValidationError as exc:
        for item in validated_media:
            _safe_remove(item["image_temp"])
            _safe_remove(item["video_temp"])
        flash(exc.safe_message, "error")
        return redirect(url_for("user_create_project_page"))

    # STEP 1-3: reserve quota, create project/pairs, and commit as one unit.
    # If DB insert or file save fails, rollback releases the reserved quota and saved files are removed.
    saved_paths = []
    pairs_data = []
    project = None
    try:
        if not _reserve_project_quota_atomic(user):
            db.session.rollback()
            flash("Project limit reached. Please upgrade your plan.", "error")
            return redirect(url_for("user_create_project_page"))

        try:
            max_index = db.session.query(func.max(Project.user_project_index)).filter(
                Project.owner_user_id == user.id
            ).scalar()
            user_project_index = (int(max_index) if max_index and int(max_index) > 0 else 0) + 1
        except Exception:
            try:
                existing_count = Project.query.filter_by(owner_user_id=user.id).count()
            except Exception:
                existing_count = 0
            user_project_index = int(existing_count or 0) + 1

        project = Project(name=name, owner_user_id=user.id, user_project_index=user_project_index)
        _upload_log("UPLOAD PERSIST START", upload_id, user_id=user.id, pair_count=len(images))
        db.session.add(project)
        db.session.flush()

        pair_slots_ok, pair_slots_error = _reserve_pair_slots_for_project(project.id, len(images), max_pairs)
        if not pair_slots_ok:
            raise ValueError(pair_slots_error)

        for i, (image_file, video_file) in enumerate(zip(images, videos)):
            marker_meta = marker_metadata[i]
            media = validated_media[i]
            img_filename = f"{project.id}_{i}.jpg"
            vid_filename = f"{project.id}_{i}{media['video_ext']}"

            img_path = os.path.join(IMAGES_DIR, img_filename)
            os.replace(media["image_temp"], img_path)  # atomic move: already-validated content only
            saved_paths.append(img_path)

            vid_path = os.path.join(VIDEOS_DIR, vid_filename)
            video_persist_start = time.time()
            _upload_log(
                "VIDEO SERVER PERSIST START",
                upload_id,
                **_video_log_fields(
                    video_file,
                    user_id=user.id,
                    project_id=project.id,
                    pair_index=i,
                    content_length=request.content_length,
                ),
            )
            os.replace(media["video_temp"], vid_path)  # atomic move: already-validated content only
            saved_paths.append(vid_path)
            video_size = os.path.getsize(vid_path)
            _upload_log(
                "VIDEO SERVER PERSIST DONE",
                upload_id,
                **_video_log_fields(
                    video_file,
                    user_id=user.id,
                    project_id=project.id,
                    pair_index=i,
                    content_length=request.content_length,
                    video_size=video_size,
                    persistence_duration_ms=round((time.time() - video_persist_start) * 1000),
                ),
            )

            pair = ProjectPair(
                project_id=project.id,
                pair_index=i,
                image_filename=img_filename,
                video_filename=vid_filename,
                image_path=f"/image/{project.id}/{i}",
                original_image_name=image_file.filename,
                original_video_name=video_file.filename,
                image_size=marker_meta["processed_size_bytes"] or image_file.content_length,
                video_size=video_size,
                marker_mode=marker_meta["mode"],
                marker_crop_x=marker_meta["crop_x"],
                marker_crop_y=marker_meta["crop_y"],
                marker_crop_width=marker_meta["crop_width"],
                marker_crop_height=marker_meta["crop_height"],
                marker_rotation=marker_meta["rotation"],
                marker_original_width=marker_meta["original_width"],
                marker_original_height=marker_meta["original_height"],
                marker_processed_width=marker_meta["processed_width"],
                marker_processed_height=marker_meta["processed_height"],
                marker_source_size_bytes=marker_meta["source_size_bytes"],
                marker_processed_size_bytes=marker_meta["processed_size_bytes"],
                marker_display_orientation=marker_meta["display_orientation"],
                is_processed=False,
                processing_status="uploaded",
                feature_extraction_status="pending",
                processing_error=None,
            )
            db.session.add(pair)

            pairs_data.append({
                "pair_index": i,
                "image_filename": img_filename,
                "video_filename": vid_filename,
                "video_size": video_size,
                "original_video_name": video_file.filename,
                "video_mime_type": video_file.mimetype,
            })

        upload_timing["files_persisted_at"] = time.time()
        _upload_log("UPLOAD PERSIST DONE", upload_id, user_id=user.id, project_id=project.id, pair_count=len(pairs_data), duration_ms=round((upload_timing["files_persisted_at"] - request_start) * 1000))
        db.session.commit()
        _upload_log("UPLOAD DB COMMIT DONE", upload_id, user_id=user.id, project_id=project.id, pair_count=len(pairs_data))
    except Exception as exc:
        db.session.rollback()
        for saved_path in saved_paths:
            try:
                if saved_path and os.path.exists(saved_path):
                    os.remove(saved_path)
            except Exception:
                pass
        # Any pairs not yet reached in the loop above still have their
        # validated temp files sitting in TMP_UPLOADS_DIR - clean those up too.
        for media in validated_media:
            _safe_remove(media.get("image_temp"))
            _safe_remove(media.get("video_temp"))
        flash(str(exc) if isinstance(exc, ValueError) else "Project upload failed. Please try again.", "error")
        return redirect(url_for("user_create_project_page"))
    # ✅ STEP 4: Generate QR code (FAST)
    user_name = (user.first_name or user.email.split("@")[0]).strip()
    # Prefer a configured public host (useful for generating QR codes accessible from mobile)
    public_host = get_system_config('public_host')
    if public_host:
        base = public_host.rstrip('/')
        scanner_path = url_for("scanner", project_id=project.id, user_id=user.id, user_name=user_name)
        scanner_url = f"{base}{scanner_path}"
    else:
        # Fallback to request-based external URL. Ensure scheme matches current request.
        scheme = request.scheme or 'http'
        scanner_url = url_for(
            "scanner",
            project_id=project.id,
            user_id=user.id,
            user_name=user_name,
            _external=True,
            _scheme="https"
        )
    
    qr_filename = f"project_{project.id}_main.png"
    qr_path = os.path.join(QR_DIR, qr_filename)

    ok = generate_custom_qr(scanner_url, qr_path, project_name=project.name)
    if not ok or not os.path.exists(qr_path):
        generate_basic_qr(scanner_url, "black", "white", qr_path, project_name=project.name)

    # Update project
    project.scanner_url = scanner_url
    project.qr_code_filename = qr_filename
    project.qr_code_path = f"/qr/{qr_filename}"
    db.session.commit()

    # ✅ STEP 5: Start background processing for ALL PAIRS
    try:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        
        def process_single_pair_bg(project_id, pair_index, img_filename, upload_id, video_info=None):
            """Process ONE pair in background - YOUR EXACT LOGIC"""
            pair_start = time.time()
            video_info = video_info or {}
            _upload_log(
                "VIDEO BG START",
                upload_id,
                project_id=project_id,
                pair_index=pair_index,
                filename=video_info.get("original_video_name"),
                mime_type=video_info.get("video_mime_type"),
                video_size=video_info.get("video_size"),
            )
            try:
                # Get image path
                img_path = os.path.join(IMAGES_DIR, img_filename)

                # Mark as processing
                with app.app_context():
                    pair = ProjectPair.query.filter_by(
                        project_id=project_id,
                        pair_index=pair_index
                    ).first()
                    if pair:
                        pair.processing_status = "processing"
                        pair.feature_extraction_status = "extracting"
                        db.session.commit()

                # ✅ YOUR EXACT FEATURE EXTRACTION LOGIC
                work_img_path = os.path.join(IMAGES_DIR, f"{project_id}_{pair_index}_work.jpg")
                npz_path = os.path.join(FEATURES_DIR, f"{project_id}_{pair_index}.npz")

                # Process this single pair. Root-cause fix (real-device regression, projects
                # 39/40): the normal upload path already renders the user-selected ROI into a
                # canvas and uploads THOSE pixels (see drawCroppedMarkerToCanvas /
                # renderMarkerBlob in user_create_project.html) — the stored marker_crop_x/y/
                # width/height are provenance/UI metadata describing a crop ALREADY baked into
                # img_path, never an instruction to crop again. Passing marker_meta here
                # (removed) double-cropped every new project's marker down to a sliver of the
                # card (project 40: 641x1200 -> 245x644). The exact uploaded pixels are always
                # the authoritative marker image for normal processing; marker_meta is only
                # ever applied via the explicit legacy-repair path — see
                # rebuild_pair_features(..., apply_legacy_roi=True).
                standardize_uploaded_image(img_path, target_size=1200)
                make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92)
                extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
                
                # Clean up temporary file
                try:
                    if os.path.exists(work_img_path):
                        os.remove(work_img_path)
                except Exception:
                    pass
                
                # Mark as processed in database (need app context)
                with app.app_context():
                    pair = ProjectPair.query.filter_by(
                        project_id=project_id,
                        pair_index=pair_index
                    ).first()
                    if pair:
                        pair.is_processed = True
                        pair.processing_status = "completed"
                        pair.feature_extraction_status = "extracted"
                        pair.processing_error = None
                        db.session.commit()
                        proj = Project.query.get(project_id)
                        display_pid = proj.user_project_index if proj and proj.user_project_index else project_id
                        print(f"[BG] Processed pair {pair_index} for project {display_pid} (global {project_id})")
                        _upload_log("BG PAIR DONE", upload_id, project_id=project_id, pair_index=pair_index, duration_ms=round((time.time() - pair_start) * 1000), status="completed")
                        _upload_log(
                            "VIDEO BG DONE",
                            upload_id,
                            project_id=project_id,
                            pair_index=pair_index,
                            filename=video_info.get("original_video_name"),
                            mime_type=video_info.get("video_mime_type"),
                            video_size=video_info.get("video_size"),
                            background_duration_ms=round((time.time() - pair_start) * 1000),
                            status="completed",
                        )
                
                return True
                
            except Exception as e:
                print(f"[BG ERROR] Failed pair {pair_index}: {e}")
                _upload_log("BG PAIR DONE", upload_id, project_id=project_id, pair_index=pair_index, duration_ms=round((time.time() - pair_start) * 1000), status="failed", error=str(e)[:120])
                _upload_log(
                    "VIDEO BG DONE",
                    upload_id,
                    project_id=project_id,
                    pair_index=pair_index,
                    filename=video_info.get("original_video_name"),
                    mime_type=video_info.get("video_mime_type"),
                    video_size=video_info.get("video_size"),
                    background_duration_ms=round((time.time() - pair_start) * 1000),
                    status="failed",
                    error=str(e)[:120],
                )
                with app.app_context():
                    pair = ProjectPair.query.filter_by(
                        project_id=project_id,
                        pair_index=pair_index
                    ).first()
                    if pair:
                        pair.is_processed = False
                        pair.processing_status = "failed"
                        pair.feature_extraction_status = "failed"
                        pair.processing_error = str(e)
                        db.session.commit()
                return False
        
        def background_processing_all_pairs(project_id, all_pairs_data, upload_id):
            """Process ALL pairs in parallel"""
            bg_start = time.time()
            with app.app_context():
                try:
                    proj = Project.query.get(project_id)
                    display_pid = proj.user_project_index if proj and proj.user_project_index else project_id
                    print(f"[BG START] Processing {len(all_pairs_data)} pairs for project {display_pid} (global {project_id})")
                    _upload_log("BG START", upload_id, project_id=project_id, pair_count=len(all_pairs_data))
                    
                    # Process pairs in parallel for speed
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                        futures = []
                        for pair_data in all_pairs_data:
                            future = executor.submit(
                                process_single_pair_bg,
                                project_id,
                                pair_data["pair_index"],
                                pair_data["image_filename"],
                                upload_id,
                                pair_data,
                            )
                            futures.append(future)
                        
                        # Wait for all to complete
                        results = [f.result() for f in futures]
                        successful = sum(results)
                        
                        proj = Project.query.get(project_id)
                        display_pid = proj.user_project_index if proj and proj.user_project_index else project_id
                        print(f"[BG DONE] Project {display_pid} (global {project_id}): {successful}/{len(all_pairs_data)} pairs processed")
                        _upload_log("BG DONE", upload_id, project_id=project_id, pair_count=len(all_pairs_data), successful=successful, duration_ms=round((time.time() - bg_start) * 1000))
                    
                    # Clear feature cache
                    load_features.cache_clear()
                    
                except Exception as e:
                    print(f"[BG FATAL ERROR] {e}")
                    _upload_log("BG DONE", upload_id, project_id=project_id, pair_count=len(all_pairs_data), status="failed", error=str(e)[:120])
                    import traceback
                    traceback.print_exc()
        
        # Start background processing
        thread = threading.Thread(
            target=background_processing_all_pairs,
            args=(project.id, pairs_data, upload_id),
            daemon=True
        )
        thread.start()
        upload_timing["jobs_scheduled_at"] = time.time()
        _upload_log("UPLOAD BG SCHEDULED", upload_id, user_id=user.id, project_id=project.id, pair_count=len(pairs_data))
        
        print(f"[UPLOAD] Started background processing for {len(pairs_data)} pairs")
        
    except Exception as e:
        print(f"Failed to start background processing: {e}")

    display_pid = project.user_project_index if project and project.user_project_index else project.id
    total_time = time.time() - t0
    persist_time = (upload_timing["files_persisted_at"] or time.time()) - t0
    schedule_time = (upload_timing["jobs_scheduled_at"] or time.time()) - (upload_timing["files_persisted_at"] or t0)
    print(f"[UPLOAD COMPLETE] Project {display_pid} (global {project.id}) created in {total_time:.2f}s with {len(pairs_data)} pairs; persist={persist_time:.2f}s schedule={schedule_time:.2f}s")
    _upload_log("UPLOAD RESPONSE SENT", upload_id, user_id=user.id, project_id=project.id, pair_count=len(pairs_data), duration_ms=round(total_time * 1000))

    flash("Project created successfully! Features are processing in the background.", "success")
    return redirect(url_for("success_page", project_id=project.id))

@app.route("/project/<int:project_id>", methods=["GET"])
@login_required
def project_view(project_id):
    # Check if admin is viewing
    admin_view = request.args.get("admin_view") == "true"
    view_user_id = request.args.get("user_id", type=int)
    
    project = Project.query.get_or_404(project_id)
    
    # If admin viewing someone's project
    if admin_view and view_user_id and current_admin():
        # Admin is viewing - allow access
        user = User.query.get_or_404(view_user_id)
        print(f"👤 Admin viewing project {project_id} for user {user.id}")
    else:
        # Regular user viewing their own project
        user = current_user()
        if project.owner_user_id != user.id:
            abort(404)
    
    # Redirect to projects list or preview
    return redirect(url_for("project_preview", project_id=project_id, admin_view=admin_view, user_id=view_user_id))


# --------------------------------------------------------------------------------------------
# Subscription & Payment Routes
# --------------------------------------------------------------------------------------------
@app.route("/pricing")
def pricing_page():
    """Public pricing page — no login required. Passes user=None for guests."""
    plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.display_order.asc()).all()
    user = current_user()  # None for guests, User object if logged in
    return render_template("user/subscribe.html", plans=plans, user=user, get_system_config=get_system_config, dev_test_entitled=has_dev_test_entitlement(user))


@app.route("/subscribe", methods=["GET"])
@login_required
def subscribe_page():
    """Show subscription plans"""
    user = current_user()
    plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.display_order.asc()).all()
    
    return render_template("user/subscribe.html", 
                         plans=plans, 
                         user=user,
                         get_system_config=get_system_config,
                         dev_test_entitled=has_dev_test_entitlement(user))

def activate_payment(payment_order):
    """Idempotently activate a subscription for a PaymentOrder whose Razorpay
    signature has already been verified by the caller.

    This is the ONE shared activation service (area 1 of the Phase 2 spec):
    callable from the browser /verify-payment route, from a future Razorpay
    webhook (not built this phase - just kept webhook-ready by taking a
    plain PaymentOrder row and no request/session state), or from the
    `reconcile-payment-activations` CLI command. It re-derives plan
    price/limits from the stored PaymentOrder/SubscriptionPlan rows itself
    and never trusts any caller-supplied amount/plan value.

    Idempotency is DB-level, not a Python if-check after a plain SELECT: the
    activation gate is a single conditional `UPDATE payment_orders SET
    status='success', ... WHERE id=? AND status='pending'`. Exactly one
    concurrent caller can ever see `updated == 1` for a given order row (same
    guarantee as _atomic_increment_user_counter / _reserve_capacity_slot_atomic
    above), so replaying the same callback - or two callers racing on the
    same order - can never reset quotas / extend subscription_end / consume a
    second capacity slot twice.

    Returns {"success": True, "order_id":..., "plan_name":..., "replay": bool}
    or {"success": False, "error": ..., "code": ...}.
    """
    plan = SubscriptionPlan.query.get(payment_order.plan_id)
    if not plan:
        return {"success": False, "error": "Plan not found", "code": "PLAN_NOT_FOUND"}

    reservation = PaymentReservation.query.filter_by(payment_order_id=payment_order.id).first()

    if reservation and reservation.user_id != payment_order.user_id:
        # Defensive only - reservation and order are always created for the
        # same user in the same request. Never activate on a mismatch.
        app.logger.warning(f"payment_activation_reservation_owner_mismatch order_id={payment_order.id}")
        return {"success": False, "error": "Reservation does not match this order", "code": "RESERVATION_MISMATCH"}

    if reservation and reservation.status in ("released", "expired"):
        # Permanent, not just "expired right now": once a reservation has
        # been given up, a later retry of this same order must never sneak
        # through and activate for free without holding a capacity slot.
        return {
            "success": False,
            "error": "Your checkout session expired before payment was confirmed. Please start a new purchase.",
            "code": "RESERVATION_EXPIRED",
        }

    if reservation and reservation.status == "reserved" and reservation.expires_at < dt.utcnow():
        _release_capacity_slot(reservation, "expired", "expired-at-verification")
        return {
            "success": False,
            "error": "Your checkout session expired before payment was confirmed. Please start a new purchase.",
            "code": "RESERVATION_EXPIRED",
        }

    now = dt.utcnow()
    if plan.duration_type == "time":
        subscription_end = now + timedelta(days=plan.duration_value * 30)
    else:
        subscription_end = now + timedelta(days=365 * 10)  # count-based plans: far future date

    updated = PaymentOrder.query.filter(
        PaymentOrder.id == payment_order.id,
        PaymentOrder.status == "pending",
    ).update(
        {
            PaymentOrder.status: "success",
            PaymentOrder.payment_at: now,
            PaymentOrder.subscription_start: now,
            PaymentOrder.subscription_end: subscription_end,
        },
        synchronize_session=False,
    )

    if updated != 1:
        db.session.rollback()
        fresh_order = PaymentOrder.query.get(payment_order.id)
        if fresh_order and fresh_order.status == "success":
            app.logger.info(f"payment_activation_duplicate_callback_ignored order_id={payment_order.id}")
            return {"success": True, "order_id": fresh_order.order_id, "plan_name": plan.plan_name, "replay": True}
        return {"success": False, "error": "Payment order is not pending", "code": "ORDER_NOT_PENDING"}

    user = User.query.get(payment_order.user_id)
    user.subscription_id = plan.id
    user.subscription_taken_at = now
    user.subscription_expires_at = subscription_end
    user.subscription_status = "active"
    user.subscribed_project_limit = plan.total_project_limit
    user.subscribed_scan_limit = plan.total_scan_limit
    user.projects_used = 0
    user.scans_used = 0

    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    if trial:
        trial.trial_converted = True
        trial.converted_at = now
        trial.converted_plan_id = plan.id

    if reservation:
        # Single conditional UPDATE, same idempotent-no-op pattern as above:
        # if it's already 'activated' (shouldn't happen given the order-status
        # gate above already caught that case) this simply updates 0 rows.
        PaymentReservation.query.filter(
            PaymentReservation.id == reservation.id,
            PaymentReservation.status == "reserved",
        ).update({PaymentReservation.status: "activated"}, synchronize_session=False)

    db.session.commit()
    app.logger.info(f"payment_activated order_id={payment_order.id} user_id={user.id} plan_id={plan.id}")

    return {"success": True, "order_id": payment_order.order_id, "plan_name": plan.plan_name, "replay": False}


@app.route("/create-razorpay-order", methods=["POST"])
@login_required
def create_razorpay_order():
    """Create Razorpay order for subscription"""
    user = current_user()
    if has_dev_test_entitlement(user):
        return jsonify({
            "success": False,
            "error": "Development test accounts already have unlimited access. Payment is disabled."
        }), 403

    plan_id = request.form.get("plan_id", type=int)

    if not plan_id:
        return jsonify({"success": False, "error": "Plan ID required"})

    plan = SubscriptionPlan.query.get(plan_id)
    if not plan or not plan.is_active or plan.is_trial_plan:
        return jsonify({"success": False, "error": "Invalid plan"})

    # Check if Razorpay is configured
    if not razorpay_client:
        return jsonify({
            "success": False,
            "error": "Payment gateway not configured. Please contact support."
        })

    # Capacity gate: reject BEFORE any payment collection begins (area 7) -
    # no Razorpay order is created at all if the slot reservation fails.
    reservation = _reserve_capacity_slot_atomic(user)
    if reservation is None:
        app.logger.info(f"capacity_full_rejection user_id={user.id}")
        message = "ScanStory early-access capacity is currently full. Please check back soon."
        return jsonify({
            "success": False,
            "code": "CAPACITY_FULL",
            "message": message,
            "error": message,
        }), 503

    # Calculate amount in paise (Razorpay expects amount in smallest currency unit)
    try:
        amount_paise = int(plan.effective_price * 100)
        if amount_paise < 100:  # Minimum amount for Razorpay is 100 paise (₹1)
            amount_paise = 100
    except Exception as e:
        _release_capacity_slot(reservation, "released", "invalid-amount")
        return jsonify({"success": False, "error": f"Invalid amount: {str(e)}"})

    # Create Razorpay order
    try:
        order_data = {
            'amount': amount_paise,
            'currency': plan.currency,
            'payment_capture': 1,  # Auto-capture payment
            'notes': {
                'user_id': str(user.id),
                'plan_id': str(plan.id),
                'plan_name': plan.plan_name,
                'user_email': user.email
            }
        }
        app.logger.info(
            "Creating Razorpay order",
            extra={
                "payment_order": {
                    "user_id": user.id,
                    "plan_id": plan.id,
                    "amount": amount_paise,
                    "currency": plan.currency,
                }
            },
        )

        razorpay_order = razorpay_client.order.create(data=order_data)

        # Create payment order in database
        order_id = f"ORD_{user.id}_{int(time.time())}"
        payment_order = PaymentOrder(
            order_id=order_id,
            razorpay_order_id=razorpay_order['id'],
            user_id=user.id,
            plan_id=plan.id,
            amount=plan.plan_amount,
            offer_amount=plan.offer_price,
            total_amount=plan.effective_price,
            currency=plan.currency,
            status="pending",
            purchased_project_limit=plan.total_project_limit,
            purchased_scan_limit=plan.total_scan_limit
        )
        db.session.add(payment_order)
        db.session.flush()
        reservation.payment_order_id = payment_order.id
        db.session.commit()

        print(f"✅ Order created: {razorpay_order['id']}")

        return jsonify({
            "success": True,
            "order_id": razorpay_order['id'],
            "amount": amount_paise,
            "currency": plan.currency,
            "key": RAZORPAY_KEY_ID,
            "name": "ScanStory AR Platform",
            "description": f"Subscription: {plan.plan_name}",
            "prefill": {
                "name": user.full_name or user.email.split('@')[0],
                "email": user.email,
                "contact": user.phone or "9999999999"
            },
            "theme": {
                "color": "#ff007a"
            }
        })

    except razorpay.errors.BadRequestError as e:
        db.session.rollback()
        _release_capacity_slot(reservation, "released", "razorpay-bad-request")
        print(f"❌ Razorpay Bad Request: {e}")
        return jsonify({"success": False, "error": f"Invalid request to payment gateway: {str(e)}"})
    except _RAZORPAY_AUTH_ERROR as e:
        db.session.rollback()
        _release_capacity_slot(reservation, "released", "razorpay-auth-error")
        print(f"❌ Razorpay Authentication Error: {e}")
        return jsonify({"success": False, "error": "Payment gateway authentication failed. Please check API keys."})
    except Exception as e:
        db.session.rollback()
        _release_capacity_slot(reservation, "released", "razorpay-order-create-failed")
        print(f"❌ Razorpay order creation failed: {e}")
        return jsonify({"success": False, "error": f"Payment gateway error: {str(e)}"})

@app.route("/verify-payment", methods=["POST"])
@login_required
def verify_payment():
    """Verify Razorpay payment and activate subscription (idempotent - see
    activate_payment() above for the DB-level replay-safety guarantee)."""
    user = current_user()

    razorpay_payment_id = request.form.get("razorpay_payment_id")
    razorpay_order_id = request.form.get("razorpay_order_id")
    razorpay_signature = request.form.get("razorpay_signature")

    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        return jsonify({"success": False, "error": "Missing payment details"})

    # Verify signature
    params_dict = {
        'razorpay_order_id': razorpay_order_id,
        'razorpay_payment_id': razorpay_payment_id,
        'razorpay_signature': razorpay_signature
    }

    try:
        # Verify payment signature (unchanged - existing logic, kept, never weakened)
        razorpay_client.utility.verify_payment_signature(params_dict)
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"success": False, "error": "Invalid payment signature"})
    except Exception as e:
        print(f"Payment verification failed: {e}")
        return jsonify({"success": False, "error": str(e)})

    # Get payment order from database - must belong to the session user.
    payment_order = PaymentOrder.query.filter_by(razorpay_order_id=razorpay_order_id).first()
    if not payment_order or payment_order.user_id != user.id:
        return jsonify({"success": False, "error": "Invalid payment order"})

    # Defensive checks (area 6): never trust a client-submitted plan/amount -
    # these are OPTIONAL fields; if the caller sends them, they must match
    # the server-side stored PaymentOrder row exactly.
    claimed_plan_id = request.form.get("plan_id", type=int)
    if claimed_plan_id is not None and claimed_plan_id != payment_order.plan_id:
        app.logger.warning(f"payment_verification_plan_mismatch order_id={payment_order.id}")
        return jsonify({"success": False, "error": "Plan does not match this order"})

    claimed_amount = request.form.get("amount", type=float)
    if claimed_amount is not None and abs(claimed_amount - payment_order.total_amount) > 0.01:
        app.logger.warning(f"payment_verification_amount_mismatch order_id={payment_order.id}")
        return jsonify({"success": False, "error": "Amount does not match this order"})

    payment_order.razorpay_payment_id = razorpay_payment_id
    payment_order.razorpay_signature = razorpay_signature
    try:
        db.session.commit()
    except IntegrityError:
        # DB-enforced uniqueness on razorpay_payment_id (area 2): this
        # payment id is already attached to a different order row.
        db.session.rollback()
        return jsonify({"success": False, "error": "This payment has already been used for another order."}), 409

    result = activate_payment(payment_order)
    if not result["success"]:
        status_code = 409 if result.get("code") else 200
        return jsonify(result), status_code

    if not result.get("replay"):
        try:
            plan = SubscriptionPlan.query.get(payment_order.plan_id)
            send_payment_success_email(user, plan, payment_order)
        except Exception as e:
            print(f"Failed to send payment success email: {e}")

    return jsonify({
        "success": True,
        "message": "Payment verified successfully",
        "order_id": result["order_id"],
        "plan_name": result["plan_name"],
    })

@app.route("/payment-success")
@login_required
def payment_success():
    """Show payment success page"""
    order_id = request.args.get("order_id")
    if not order_id:
        return redirect(url_for("dashboard"))
    
    payment_order = PaymentOrder.query.filter_by(order_id=order_id, user_id=current_user().id).first()
    if not payment_order or payment_order.status != "success":
        flash("Invalid or pending payment", "error")
        return redirect(url_for("subscribe_page"))
    
    plan = SubscriptionPlan.query.get(payment_order.plan_id)
    user = current_user()
    
    return render_template("user/payment_success.html",
                         user=user,
                         plan=plan,
                         order=payment_order)

@app.route("/payment-failed")
@login_required
def payment_failed():
    """Show payment failed page"""
    flash("Payment failed. Please try again.", "error")
    return redirect(url_for("subscribe_page"))

# --------------------------------------------------------------------------------------------
# Success Page
# --------------------------------------------------------------------------------------------
@app.route("/success/<int:project_id>", methods=["GET"])
@login_required
def success_page(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    
    if not project or project.owner_user_id != user.id:
        abort(404)
    
    # ✅ Calculate display number for the project (use helper which prefers persisted index)
    project.display_number = get_project_display_number(project)
    
    pairs = ProjectPair.query.filter_by(project_id=project.id).order_by(ProjectPair.pair_index.asc()).all()
    
    return render_template(
        "user/success.html",
        project=project,
        pairs=pairs,
        user=user,
        is_admin=False,
        qr_download_url=url_for("download_project_qr", project_id=project.id),
        projects_url=url_for("projects_page"),
        test_scanner_url=url_for("scanner_test_entry", project_id=project.id)
    )

# --------------------------------------------------------------------------------------------
# Scanner Routes (Public)
# --------------------------------------------------------------------------------------------

@app.route("/video/<int:project_id>/<int:image_id>")
def serve_video(project_id, image_id):
    project = Project.query.get(project_id)
    if not project:
        return "Project not found"
    if not _project_is_available(project):
        return _project_unavailable_response()
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        return "Pair not found"
    
    response = send_from_directory(VIDEOS_DIR, pair.video_filename)
    response.headers["Content-Disposition"] = "inline"
    return _apply_short_public_cache(response)

@app.route("/image/<int:project_id>/<int:image_id>")
def serve_image(project_id, image_id):
    project = Project.query.get(project_id)
    if not project:
        return "Project not found"
    if not _project_is_available(project):
        return _project_unavailable_response()
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        return "Pair not found"
    
    response = send_from_directory(IMAGES_DIR, pair.image_filename)
    return _apply_short_public_cache(response)

@app.route("/qr/<filename>")
def serve_qr(filename):
    project = _project_from_qr_filename(filename, admin_project=False)
    if project and not _project_is_available(project):
        return _project_unavailable_response()
    response = send_from_directory(QR_DIR, filename)
    return _apply_short_public_cache(response)

SCANNER_TEST_TOKEN_MAX_AGE_SECONDS = 120


def _scanner_test_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="scanner-test-entry")


def _issue_scanner_test_token(project_id, ctx, **identity):
    """Minted only by the ownership-checked routes below (scanner_test_entry /
    admin_scanner_test_entry) — this is what proves a /scanner/ visit is a genuine,
    server-verified creator/admin test rather than a forged ?entry_context=/?mode= query
    param, which any public viewer could also send."""
    return _scanner_test_serializer().dumps({"project_id": project_id, "ctx": ctx, **identity})


def _read_scanner_test_token(token):
    """Returns (payload, None) on success, or (None, reason) on any failure — expired,
    tampered, or simply absent (the normal public-viewer case)."""
    if not token:
        return None, "no_test_token"
    try:
        payload = _scanner_test_serializer().loads(token, max_age=SCANNER_TEST_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return None, "expired_token"
    except BadSignature:
        return None, "invalid_token"
    return payload, None


def resolve_scanner_entry_context(project, test_token):
    """Server-verified only. Never infers creator_test/admin_test from session.user_id or
    session.admin_id alone — a public QR link's owner id would look identical to the
    owner's own session the instant they're logged in, and a viewer can freely fake
    ?entry_context=creator_test or ?mode=creator in the query string. The ONLY way to reach
    creator_test/admin_test is a short-lived signed token minted by the dedicated,
    ownership-checked entry routes (scanner_test_entry / admin_scanner_test_entry) below.

    Returns a dict: context, back_url, back_destination_reason, entry_route_type,
    entry_authorization_result. context is one of
    'public_viewer' | 'creator_test' | 'admin_test'.
    """
    default = {
        "context": "public_viewer",
        "back_url": url_for("landing"),
        "back_destination_reason": "public_viewer",
        "entry_route_type": "public_scanner_route",
        "entry_authorization_result": "n/a_public",
    }
    payload, err = _read_scanner_test_token(test_token)
    if err:
        result = dict(default)
        if err != "no_test_token":
            result["entry_authorization_result"] = err
        return result
    if payload.get("project_id") != project.id:
        result = dict(default)
        result["entry_authorization_result"] = "project_mismatch"
        return result
    ctx = payload.get("ctx")
    if ctx == "creator_test":
        session_user_id = session.get("user_id")
        if session_user_id and payload.get("user_id") == session_user_id and project.owner_user_id == session_user_id:
            return {
                "context": "creator_test",
                "back_url": url_for("project_preview", project_id=project.id),
                "back_destination_reason": "creator_test",
                "entry_route_type": "creator_test_route",
                "entry_authorization_result": "authorized",
            }
        result = dict(default)
        result["entry_route_type"] = "creator_test_route"
        result["entry_authorization_result"] = "not_owner"
        return result
    if ctx == "admin_test":
        session_admin_id = session.get("admin_id")
        if session_admin_id and payload.get("admin_id") == session_admin_id and project.owner_admin_id == session_admin_id:
            return {
                "context": "admin_test",
                "back_url": url_for("admin_project_preview", project_id=project.id),
                "back_destination_reason": "admin_test",
                "entry_route_type": "admin_test_route",
                "entry_authorization_result": "authorized",
            }
        result = dict(default)
        result["entry_route_type"] = "admin_test_route"
        result["entry_authorization_result"] = "not_owner"
        return result
    result = dict(default)
    result["entry_authorization_result"] = "invalid_token"
    return result


@app.route("/project/<int:project_id>/scanner-test")
@login_required
def scanner_test_entry(project_id):
    """The ONLY safe way to reach creator_test scanner context. Verifies real ownership via
    the actual authenticated session (never a query param), then mints a short-lived signed
    token so /scanner/ can prove this visit came from here."""
    user = current_user()
    project = Project.query.get_or_404(project_id)
    if project.owner_user_id != user.id:
        abort(404)
    token = _issue_scanner_test_token(project.id, "creator_test", user_id=user.id)
    return redirect(url_for("scanner", project_id=project.id, test_token=token))


@app.route("/admin/project/<int:project_id>/scanner-test")
@admin_required
def admin_scanner_test_entry(project_id):
    """Admin equivalent of scanner_test_entry — same signed-token pattern, admin ownership
    verified server-side before the token is ever minted."""
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    if project.owner_admin_id != admin.id:
        abort(404)
    token = _issue_scanner_test_token(project.id, "admin_test", admin_id=admin.id)
    return redirect(url_for("scanner", project_id=project.id, test_token=token))


@app.route("/scanner/<int:project_id>")
def scanner(project_id):
    """Public scanner - handles both user and admin projects.

    user_id/user_name/admin_id/admin_name query params are legacy artifacts of the original
    QR URL shape (see scanner_url generation above) — display-only historically, and now not
    read at all: they must never authenticate a viewer, mutate the session, or influence
    entry-context resolution. Project ownership/attribution is resolved from the Project
    record itself, never from the query string. A tokenized /s/<share_token> public URL that
    doesn't expose owner identity in the query string at all is a required next phase — see
    gate-jr/cross-device-test-matrix.md.
    """
    test_token = request.args.get("test_token")

    project = Project.query.get(project_id)

    if not project:
        return "Project not found"
    if not _project_is_available(project):
        return _project_unavailable_response()

    # Entry context is resolved purely server-side (signed token + real session ownership
    # check) — the session is never mutated by this route at all, for any project.
    entry = resolve_scanner_entry_context(project, test_token)

    # project_owner_id / project_owner_admin_id: resolved from the DB record, never from an
    # editable URL parameter. This is what used to be a single ambiguous `user_id` reused for
    # both "who owns this project" and "who is scanning it".
    project_owner_id = project.owner_user_id
    if project_owner_id:
        creator_type = "user"
        creator_name = project.owner_user.full_name if project.owner_user else "User"
    else:
        creator_type = "admin"
        creator_name = project.owner_admin.name if project.owner_admin else "Admin"

    return render_template(
        "user/scanner.html",
        project_id=project_id,
        project_name=project.name,
        qr_code_url=project.qr_code_path,
        creator_type=creator_type,
        creator_name=creator_name,
        scanner_diagnostics_enabled=scanner_diagnostics_enabled(),
        scanner_entry_context=entry["context"],
        resolved_back_destination=entry["back_url"],
        back_destination_reason=entry["back_destination_reason"],
        entry_route_type=entry["entry_route_type"],
        entry_authorization_result=entry["entry_authorization_result"],
    )
@app.route("/detect_init", methods=["POST"])
@csrf.exempt  # Public, unauthenticated scanner endpoint - no browser session/cookie to bind a CSRF token to.
def detect_init():
    """Public detection with multi-pair support"""
    try:
        print("\n" + "="*50)
        print("🔍 DETECT_INIT CALLED")
        print("="*50)
        import sys; sys.stdout.flush()
        t_start = time.time()
        
        project_id = request.form.get("project_id", type=int)
        scan_session_id = request.form.get("scan_session_id")
        ok, retry_after = _check_rate_limit(
            "scanner_init",
            _rate_limit_key("detect_init", project_id, scan_session_id),
        )
        if not ok:
            _log_scanner_latency("detect_init", t_start, project_id=project_id, outcome="rate_limited", stage="start", scan_session_id=scan_session_id)
            return _scanner_rate_limited_response(retry_after)

        test_file = request.files.get("test_image")
        scanner_generation = request.form.get("scanner_generation")
        source_frame_width = request.form.get("source_frame_width", type=int)
        source_frame_height = request.form.get("source_frame_height", type=int)
        orientation_revision = request.form.get("orientation_revision")
        detection_meta = {
            "scanner_generation": scanner_generation,
            "source_frame_width": source_frame_width,
            "source_frame_height": source_frame_height,
            "orientation_revision": orientation_revision,
        }

        # Client timing metadata (real-device gap investigation) — logged so a silent
        # multi-second gap between requests is explained by data (client scheduling state at
        # send time), not guessed from server response timestamps alone. All optional/best-
        # effort: an older client that doesn't send these simply logs None for each.
        client_timing = {
            "request_seq": request.form.get("request_seq"),
            "client_request_started_at": request.form.get("client_request_started_at"),
            "elapsed_since_previous_request_start_ms": request.form.get("elapsed_since_previous_request_start_ms"),
            "selected_delay_ms": request.form.get("selected_delay_ms"),
            "selected_delay_reason": request.form.get("selected_delay_reason"),
            "tracking_active": request.form.get("tracking_active"),
            "tracking_age_ms": request.form.get("tracking_age_ms"),
            "detect_in_flight_before_start": request.form.get("detect_in_flight_before_start"),
            "watchdog_triggered": request.form.get("watchdog_triggered"),
            "watchdog_abort_requested": request.form.get("watchdog_abort_requested"),
            "page_visible": request.form.get("page_visible"),
            "stream_healthy": request.form.get("stream_healthy"),
            "scan_loop_token": request.form.get("scan_loop_token"),
        }
        print(f"⏱ client_timing: {client_timing}")
        
        print(f"📌 project_id: {project_id}")
        print(f"📌 test_file: {test_file.filename if test_file else 'None'}")
        sys.stdout.flush()
        
        if not project_id or test_file is None:
            print("❌ Missing project_id or image")
            sys.stdout.flush()
            return jsonify({"detected": False, "reason": "Missing project_id or image"}), 400
        
        project = Project.query.get(project_id)
        if not project:
            print(f"❌ Project not found: {project_id}")
            sys.stdout.flush()
            return jsonify({"detected": False, "reason": "Project not found"}), 404
        if not _project_is_available(project):
            return jsonify({"detected": False, "reason": "Project is suspended or unavailable"}), 404
        
        # ✅ CRITICAL FIX: Check if this is an ADMIN project
        is_admin_project = project.owner_admin_id is not None
        print(f"🔧 Is admin project: {is_admin_project}")
        
        # Get only PROCESSED pairs
        processed_pairs = ProjectPair.query.filter_by(
            project_id=project_id,
            is_processed=True
        ).order_by(ProjectPair.pair_index.asc()).all()
        print(f"📦 Processed pairs: {len(processed_pairs)}")
        sys.stdout.flush()

        # Maps pair_index (0-based, used as file/cache key) → pair.id (DB primary key, used as FK)
        # Critical: scan_logs.pair_id is a FK to project_pairs.id, NOT pair_index
        pair_index_to_db_id = {p.pair_index: p.id for p in processed_pairs}
        print(f"🗺 pair_index→db_id map: {pair_index_to_db_id}")

        total_pairs = ProjectPair.query.filter_by(project_id=project_id).count()
        print(f"📊 Processed pairs: {len(processed_pairs)}/{total_pairs}")
        print(f"📊 Project type: {'ADMIN' if is_admin_project else 'USER'}")
        
        if not processed_pairs:
            if total_pairs == 0:
                return jsonify({"detected": False, "reason": "No image-video pairs found"}), 400
            
            unprocessed = total_pairs - len(processed_pairs)
            return jsonify({
                "detected": False, 
                "reason": f"Project is processing ({unprocessed}/{total_pairs} pairs remaining)",
                "progress": f"0/{total_pairs}",
                "total_pairs": total_pairs,
                "ready_pairs": 0,
                "scanner_generation": scanner_generation,
                "source_frame_width": source_frame_width,
                "source_frame_height": source_frame_height,
                "orientation_revision": orientation_revision,
            }), 200
        
        # Get scan session info. scan_attribution_owner_id is the PROJECT OWNER, resolved
        # from the DB record — never from session/query params. This is what quota/ScanLog
        # attribution has always meant here (a public viewer's scan counts against the
        # project owner's plan), it was just previously read off session['user_id'] after
        # that session had been force-mutated to the owner's id by the scanner() route (see
        # the removed "FORCE set user_id ... from QR code" line). It is NOT the authenticated
        # viewer's own identity, which is never touched by this endpoint.
        scan_attribution_owner_id = project.owner_user_id
        scan_session_id = request.form.get("scan_session_id")

        print(f"👤 scan_attribution_owner_id: {scan_attribution_owner_id}")
        print(f"🆔 scan_session_id from request: {scan_session_id}")
        
        # If no session_id provided, generate one
        if not scan_session_id:
            scan_session_id = str(uuid.uuid4())
            print(f"⚠️ Generated new session ID: {scan_session_id}")
        else:
            print(f"✅ Using provided session ID: {scan_session_id}")
        
        scan_log = None
        user = None
        
        # ✅ CHECK SCAN LIMITS - BUT ONLY FOR USER PROJECTS
        if scan_attribution_owner_id:
            user = User.query.get(scan_attribution_owner_id)
            print(f"👤 User found: {user is not None}")

            if user:
                # Check if a log already exists for this session
                existing_log = ScanLog.query.filter_by(
                    user_id=scan_attribution_owner_id,
                    scan_session_id=scan_session_id
                ).first()

                print(f"📝 Existing log for this session: {existing_log is not None}")

                if not existing_log:
                    scan_log = ScanLog(
                        project_id=project_id,
                        user_id=scan_attribution_owner_id,
                        scan_session_id=scan_session_id,
                        is_successful=False,
                        scan_type="admin" if is_admin_project else "user"
                    )
                    db.session.add(scan_log)
                    try:
                        db.session.commit()
                    except IntegrityError:
                        db.session.rollback()
                        scan_log = ScanLog.query.filter_by(
                            user_id=scan_attribution_owner_id,
                            scan_session_id=scan_session_id
                        ).first()
                    print(f"✅ Created NEW scan log for session {scan_session_id}")
                else:
                    scan_log = existing_log
                    print(f"✅ Using EXISTING scan log for session {scan_session_id}")
                
                # ✅ ONLY check scan limits for USER projects (not admin projects)
                if not is_admin_project:
                    if not user.can_scan and not has_dev_test_entitlement(user):
                        print(f"❌ User cannot scan - limit reached")
                        return jsonify({
                            "detected": False, 
                            "reason": "Scan limit reached. Please upgrade your plan.",
                            "scan_session_id": scan_session_id
                        }), 403
                else:
                    print(f"✅ Admin project - SKIPPING scan limit check")
            else:
                print(f"❌ User not found in database")
        
        # Read image (create a COPY for processing)
        print(f"📸 Reading image...")
        file_bytes = np.frombuffer(test_file.read(), np.uint8)
        original_img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        t_after_read = time.time()
        print(f"⏱ read_time={(t_after_read - t_start):.3f}s")
        
        if original_img is None:
            print(f"❌ Invalid image")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({"detected": False, "reason": "Invalid image", **detection_meta}), 400
        
        print(f"📸 Original image shape: {original_img.shape}")
        
        # Create a WORKING COPY for processing
        img = original_img.copy()
        
        # Use 1200px to match feature extraction (ORB_MAX_DIM)
        h, w = img.shape[:2]
        target_size = 1200
        if max(h, w) > target_size:
            scale = target_size / max(h, w)
            new_w, new_h = int(w * scale), int(h * scale)
            img = cv2.resize(img, (new_w, new_h))
            print(f"📸 Resized to: {new_w}x{new_h}")
        
        # Mobile enhancement - applied to COPY only
        h, w = img.shape[:2]
        if h < 1000 or w < 1000:
            enhanced = img.copy()
            yuv = cv2.cvtColor(enhanced, cv2.COLOR_BGR2YUV)
            yuv[:,:,0] = cv2.equalizeHist(yuv[:,:,0])
            enhanced = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
            kernel = np.array([[0, -0.5, 0],
                               [-0.5, 3, -0.5],
                               [0, -0.5, 0]])
            img = cv2.filter2D(enhanced, -1, kernel)
            print(f"📸 Applied mobile enhancement")
        t_after_prep = time.time()
        print(f"⏱ prep_time={(t_after_prep - t_after_read):.3f}s")
        
        # Pass BGR image, let function handle grayscale
        gray_small, scale, orig_w, orig_h = _resize_gray_for_detect(img)
        frame_w, frame_h = orig_w, orig_h
        print(f"📸 Gray scale: {gray_small.shape}, scale: {scale}")

        # Recognition-stability diagnostics (read-only — informational, never gates
        # accept/reject). blur_score is Laplacian variance (lower = blurrier); brightness_score
        # is mean pixel intensity 0-255. Collected here so every exit path below can attach
        # them to one compact frame-level diagnostic block (see _log_frame_diag()).
        blur_score = float(cv2.Laplacian(gray_small, cv2.CV_64F).var())
        brightness_score = float(np.mean(gray_small))
        frame_diag = {
            "blur_score": round(blur_score, 1),
            "brightness_score": round(brightness_score, 1),
            "likely_blurry": blur_score < 40.0,
            "likely_glare_or_dark": brightness_score > 235.0 or brightness_score < 25.0,
        }

        def _log_frame_diag(stage, **extra):
            frame_diag.update(extra)
            print(f"📋 frame_diag[{stage}]: {frame_diag}")

        orb = _orb_detect()
        test_kp, test_desc = orb.detectAndCompute(gray_small, None)
        t_after_detect = time.time()
        print(f"⏱ detect_time={(t_after_detect - t_after_prep):.3f}s (kp={len(test_kp) if test_kp else 0})")

        if test_kp is None or test_desc is None or len(test_kp) < MIN_TEST_KP:
            print(f"❌ Too few features: {len(test_kp) if test_kp else 0}")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({
                "detected": False, 
                "reason": f"Too few features ({len(test_kp) if test_kp else 0})", 
                "frame_width": frame_w, 
                "frame_height": frame_h,
                "scanner_generation": scanner_generation,
                "source_frame_width": source_frame_width,
                "source_frame_height": source_frame_height,
                "orientation_revision": orientation_revision,
            }), 200
        
        print(f"🔍 Found {len(test_kp)} features")

        # Limit keypoints/descriptors for live matching to improve speed
        N = min(len(test_kp), QUICK_DESC_LIMIT)
        if N < len(test_kp):
            test_kp = test_kp[:N]
            test_desc = test_desc[:N]
        
        # Quick scoring
        scored = []
        for pair in processed_pairs:
            feats = load_features(project_id, pair.pair_index)
            if feats is None:
                continue
            s = quick_score(test_desc, feats, ratio=0.78, max_checks=QUICK_DESC_LIMIT)
            print(f"  🔢 Pair {pair.pair_index} quick_score={s}")
            if s > 2:  # lowered from 4 — quick_score with 500 descriptors is reliable enough at >2
                scored.append((s, pair.pair_index))

        t_after_quick = time.time()
        print(f"⏱ quick_score_time={(t_after_quick - t_after_detect):.3f}s; scored_candidates={len(scored)}")

        print(f"📊 Quick scoring results: {len(scored)} pairs scored >4")
        
        scored.sort(reverse=True)
        top_ids = [pid for _, pid in scored[:QUICK_TOPK]]
        if not top_ids:
            top_ids = [p.pair_index for p in processed_pairs[:min(QUICK_TOPK, len(processed_pairs))]]
        
        print(f"🎯 Top candidate pair IDs: {top_ids}")
        
        # Find best match
        best_match = None
        best_match_id = -1
        best_good = 0
        second_good = 0

        for pid in top_ids:
            feats = load_features(project_id, pid)
            if feats is None:
                continue

            pid_diag = {}
            best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.75, diag=pid_diag)

            if not good_matches or len(good_matches) < MIN_GOOD_MATCHES:
                best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.80, diag=pid_diag)

            if not good_matches or len(good_matches) < MIN_GOOD_MATCHES:
                best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.90, diag=pid_diag)

            if good_matches and len(good_matches) > best_good:
                second_good = best_good
                best_good = len(good_matches)
                best_match = (best_tag, good_matches, stored_kp, feats)
                best_match_id = pid
                # Match-count funnel for the CURRENT winning candidate only — overwritten if
                # a later pid in top_ids wins instead, so this always reflects best_match_id.
                frame_diag["match_funnel"] = dict(pid_diag, pair_id=pid)
                print(f"  - Pair {pid}: {len(good_matches)} good matches")
            elif good_matches and len(good_matches) > second_good:
                second_good = len(good_matches)
        t_after_match = time.time()
        print(f"⏱ match_time={(t_after_match - t_after_quick):.3f}s; best_good={best_good}; second_good={second_good}")

        if not best_match or best_good < MIN_GOOD_MATCHES:
            print(f"❌ Detection failed: best_good={best_good}")
            _log_frame_diag("mobile_detection_failed", raw_keypoints=len(test_kp), quick_score_candidates=len(scored), best_good=best_good)
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({
                "detected": False,
                "reason": f"Mobile detection failed: Found {best_good} matches",
                "frame_width": frame_w,
                "frame_height": frame_h,
                "scanner_generation": scanner_generation,
                "source_frame_width": source_frame_width,
                "source_frame_height": source_frame_height,
                "orientation_revision": orientation_revision,
            }), 200

        margin_ok, margin_code = resolve_candidate_margin(best_good, second_good)
        if not margin_ok:
            print(f"❌ Ambiguous candidates: best_good={best_good} second_good={second_good}")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({
                "detected": False,
                "reason": "Ambiguous match: two candidates too close to trust",
                "code": margin_code,
                "best_good": best_good,
                "second_good": second_good,
                "frame_width": frame_w,
                "frame_height": frame_h,
                "scanner_generation": scanner_generation,
                "source_frame_width": source_frame_width,
                "source_frame_height": source_frame_height,
                "orientation_revision": orientation_revision,
            }), 200

        print(f"✅ Best match: pair {best_match_id} with {best_good} matches")
        t_after_homography = time.time()
        print(f"⏱ homography_time={(t_after_homography - t_after_match):.3f}s; total_time={(t_after_homography - t_start):.3f}s")
        
        # Process homography
        best_tag, good_matches, stored_kp, feats = best_match
        src_pts = []
        dst_pts = []
        
        for m in good_matches:
            tp = test_kp[m.queryIdx].pt
            sp = stored_kp[m.trainIdx]
            src_pts.append([float(sp[0]), float(sp[1])])
            dst_pts.append([float(tp[0]), float(tp[1])])
        
        src_arr = np.array(src_pts, dtype=np.float32)
        dst_arr = np.array(dst_pts, dtype=np.float32)
        
        # Spatial distribution of the (deduped) good matches BEFORE RANSAC — informational
        # only, does not gate anything. Lets a bad-frame log distinguish "matches scattered
        # across the marker" (likely genuine texture, RANSAC just found the geometry
        # inconsistent) from "matches clumped in one corner" (likely background clutter or a
        # repetitive local patch, hypothesis A/B/E).
        pre_ransac_cells, pre_ransac_score = _grid_coverage(dst_arr, frame_w * scale, frame_h * scale, grid=3)
        matched_pair = next((p for p in processed_pairs if p.pair_index == best_match_id), None)
        frame_diag["reference_crop"] = {
            "stored_w": int(feats["w"]), "stored_h": int(feats["h"]),
            "marker_mode": getattr(matched_pair, "marker_mode", None),
            "marker_crop_wh_fraction": [getattr(matched_pair, "marker_crop_width", None), getattr(matched_pair, "marker_crop_height", None)],
            # No server-side cropping ever happens — these crop_* fields are only what the
            # client reported at upload time (see _parse_marker_meta), never verified or
            # applied to pixels. If a client under-reports its own crop, background outside
            # the marker is baked into the stored ORB features with no way to detect that
            # from here alone (hypothesis A) — this field makes that visible for correlation,
            # not something this pass can independently confirm or refute.
        }
        frame_diag["pre_ransac_grid_cells"] = pre_ransac_cells
        frame_diag["pre_ransac_grid_score"] = pre_ransac_score

        H, mask = cv2.findHomography(src_arr, dst_arr, cv2.RANSAC, RANSAC_REPROJ)
        if H is None or mask is None:
            print(f"❌ Homography failed")
            _log_frame_diag("homography_failed", raw_keypoints=len(test_kp), quick_score_candidates=len(scored), good_matches=best_good)
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({"detected": False, "reason": "Homography failed", "code": "missing_homography"}), 200

        inliers = int(np.sum(mask))
        print(f"📐 Inliers: {inliers}/{len(src_arr)}")

        # NOTE: the inlier-count/ratio gate lives in evaluate_homography_quality() below —
        # it uses the exact same MIN_INLIERS_ABS/MIN_INLIERS_RATIO thresholds, so nothing is
        # weakened by not duplicating the check here. Duplicating it here used to short-circuit
        # BEFORE evaluate_homography_quality ran, which meant every weak-inlier rejection came
        # back as a bare "Weak homography" string instead of a structured reason/code (see e.g.
        # a real-device log of "45 good matches, 6 inliers, required 13" with no further detail
        # — now fixed at the source by deduping good_matches, see _filter_mutual_unique_matches).
        tw, th = feats["w"], feats["h"]
        quality_ok, homography_quality = evaluate_homography_quality(
            src_arr, dst_arr, H, mask, tw, th, frame_w, frame_h, scale=scale
        )
        print(f"Homography quality: {homography_quality}")
        # Homography condition/degeneracy: a near-singular H (huge condition number) means
        # the RANSAC fit is barely constrained — informational, does not gate.
        try:
            frame_diag["homography_condition"] = float(np.linalg.cond(H))
        except Exception:
            frame_diag["homography_condition"] = None
        frame_diag["visible_area_ratio"] = homography_quality.get("quad_area", 0) / max(frame_w * frame_h, 1) if homography_quality.get("quad_area") else None

        if not quality_ok:
            print(f"Rejected pose: {homography_quality.get('reason')} ({homography_quality.get('code')})")
            _log_frame_diag(
                "quality_rejected",
                raw_keypoints=len(test_kp), quick_score_candidates=len(scored), good_matches=best_good,
                inliers=inliers, inlier_ratio=(inliers / max(best_good, 1)),
                reject_reason=homography_quality.get("reason"), reject_code=homography_quality.get("code"),
            )
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({
                "detected": False,
                "reason": f"Rejected pose: {homography_quality.get('reason')}",
                "code": homography_quality.get("code"),
                "frame_width": frame_w,
                "frame_height": frame_h,
                "scanner_generation": scanner_generation,
                "source_frame_width": source_frame_width,
                "source_frame_height": source_frame_height,
                "orientation_revision": orientation_revision,
                "homography_quality": homography_quality,
            }), 200

        rect = np.array([[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32).reshape(-1, 1, 2)
        pts = cv2.perspectiveTransform(rect, H).reshape(4, 2)
        corners = [(float(p[0] / scale), float(p[1] / scale)) for p in pts]
        
        if not valid_corners(corners, frame_w, frame_h):
            print(f"❌ Bad corners")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({"detected": False, "reason": "Bad corners"}), 200
        
        # ✅ Mark scan as successful - ONLY count for USER projects
        # ✅ Mark scan as successful only.
        # Do NOT increment scans_used here.
        # Final scan counting is handled only in /api/scanner/session/end
        # to avoid double counting one scan session.
        if user and scan_log:
            scan_log.is_successful = True

            if best_match_id >= 0:
                if best_match_id in pair_index_to_db_id:
                    # pair_index_to_db_id maps the 0-based pair_index → project_pairs.id (PK)
                    # scan_logs.pair_id is a FK to project_pairs.id, NOT pair_index
                    real_pair_db_id = pair_index_to_db_id[best_match_id]
                    scan_log.pair_id = real_pair_db_id
                    print(f"🔗 scan_log.pair_id={real_pair_db_id} (pair_index={best_match_id})")
                else:
                    print(f"⚠️ pair_index={best_match_id} missing from map — pair_id left NULL")

            db.session.commit()
            print(f"✅ Marked scan successful for session {scan_session_id}. Counting will happen at session end.")
        
        gray_full = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        mask_img = np.zeros((frame_h, frame_w), dtype=np.uint8)
        cv2.fillConvexPoly(mask_img, np.array(corners, dtype=np.int32), 255)
        
        pts_track = cv2.goodFeaturesToTrack(
            gray_full,
            maxCorners=260,
            qualityLevel=0.01,
            minDistance=6,
            mask=mask_img
        )
        
        if pts_track is None:
            pts_track = np.zeros((0, 1, 2), dtype=np.float32)
        
        corners_out = [{"x": c[0], "y": c[1]} for c in corners]
        points_out = [{"x": float(p[0]), "y": float(p[1])} for p in pts_track.reshape(-1, 2)]
        matched_pair = next((p for p in processed_pairs if p.pair_index == best_match_id), None)
        marker_mode = getattr(matched_pair, "marker_mode", None) or "full_image"
        
        if project.owner_admin_id:
            matched_video_url = url_for("serve_admin_video", project_id=project_id, image_id=best_match_id, _external=True,_scheme="https")
        else:
            matched_video_url = url_for("serve_video", project_id=project_id, image_id=best_match_id, _external=True,_scheme="https")
        
        print(f"✅ Detection successful! Returning response")
        _log_scanner_latency("detect_init", t_start, project_id=project_id, outcome="accepted", stage="response", scan_session_id=scan_session_id)
        _log_frame_diag(
            "accepted",
            raw_keypoints=len(test_kp), quick_score_candidates=len(scored), good_matches=best_good,
            inliers=inliers, inlier_ratio=(inliers / max(best_good, 1)),
        )
        print("="*50 + "\n")

        return jsonify({
            "detected": True,
            "matched_pair_id": best_match_id,
            "video_url": matched_video_url,
            "corners": corners_out,
            "init_points": points_out,
            "frame_width": frame_w,
            "frame_height": frame_h,
            "scanner_generation": scanner_generation,
            "source_frame_width": source_frame_width,
            "source_frame_height": source_frame_height,
            "orientation_revision": orientation_revision,
            "variant": best_tag,
            "inliers": inliers,
            "good_matches": best_good,
            "keypoints": len(test_kp),
            "homography_quality": homography_quality,
            "marker_mode": marker_mode,
            "top_checked": top_ids,
            "scan_session_id": scan_session_id if scan_attribution_owner_id else None,
            "ready_pairs": len(processed_pairs),
            "total_pairs": total_pairs,
            "is_admin_project": is_admin_project  # Let frontend know
        }), 200
        
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"❌ FATAL ERROR in detect_init: {str(e)}")
        print(error_trace)
        
        return jsonify({
            "detected": False,
            "reason": "Detection service temporarily unavailable",
            "error_type": "server_error"
        }), 500

@app.route("/api/scanner/session/end", methods=["POST"])
@csrf.exempt  # Public, unauthenticated scanner endpoint - no browser session/cookie to bind a CSRF token to.
def scanner_session_end():
    """End scanner session - COUNT ONLY ONCE here"""
    try:
        t_start = time.time()
        print("\n" + "="*50)
        print("🔍 SESSION END CALLED")
        print("="*50)
        
        # Handle both JSON and form data
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form
        
        print(f"📦 Received data: {data}")
        
        if not data:
            return jsonify({"ok": False, "error": "Invalid request"}), 400
        
        project_id = data.get("project_id")
        session_id = data.get("session_id")
        ok, retry_after = _check_rate_limit(
            "scanner_session_end",
            _rate_limit_key("scanner_session_end", project_id, session_id),
        )
        if not ok:
            _log_scanner_latency("scanner_session_end", t_start, project_id=project_id, outcome="rate_limited", stage="start", scan_session_id=session_id)
            return _scanner_rate_limited_response(retry_after)

        project = Project.query.get(int(project_id)) if project_id else None

        if project and project.owner_admin_id:
            print("✅ Admin project session end - not counting scan")
            return jsonify({
                "ok": True,
                "counted": False,
                "reason": "Admin project - unlimited scans"
            })

        # scan_attribution_owner_id is the PROJECT OWNER (DB record), never the viewer's own
        # session — this endpoint must never read or depend on the calling browser's
        # authentication state, since a public viewer with no session at all still needs
        # their scan to count against the project owner's quota. See detect_init() above for
        # the same fix.
        scan_attribution_owner_id = project.owner_user_id if project else None

        print(f"📌 project_id: {project_id}")
        print(f"📌 session_id: {session_id}")
        print(f"📌 scan_attribution_owner_id: {scan_attribution_owner_id}")

        if not project_id or not session_id:
            return jsonify({"ok": False, "error": "Missing required fields"}), 400

        if not scan_attribution_owner_id:
            print("❌ No attributable project owner - not counting")
            return jsonify({"ok": True, "counted": False, "reason": "No attributable project owner"})

        user = User.query.get(scan_attribution_owner_id)
        if not user:
            print(f"❌ User {scan_attribution_owner_id} not found")
            return jsonify({"ok": False, "error": "User not found"}), 404

        print(f"👤 User found: {user.email}")
        print(f"📊 Current scans_used: {user.scans_used}")

        # Check if this session had ANY successful scan
        successful_scan = ScanLog.query.filter_by(
            user_id=scan_attribution_owner_id,
            scan_session_id=session_id,
            is_successful=True
        ).first()

        print(f"✅ Successful scan found: {successful_scan is not None}")

        if successful_scan:
            print(f"   Log ID: {successful_scan.id}")
            print(f"   Project ID: {successful_scan.project_id}")
            print(f"   Counted: {getattr(successful_scan, 'counted', False)}")

        if not successful_scan:
            # Check if there are ANY logs for this session
            any_log = ScanLog.query.filter_by(
                user_id=scan_attribution_owner_id,
                scan_session_id=session_id
            ).first()
            if any_log:
                print(f"📝 Found log but is_successful={any_log.is_successful}")
            else:
                print("📝 No logs found for this session")
            
            return jsonify({"ok": True, "counted": False, "reason": "No successful detection"})
        
        claim_updated = ScanLog.query.filter(
            ScanLog.id == successful_scan.id,
            ScanLog.counted == False,
        ).update({ScanLog.counted: True}, synchronize_session=False)

        if claim_updated != 1:
            db.session.rollback()
            print("Session already counted, skipping")
            return jsonify({"ok": True, "counted": False, "reason": "Already counted"})

        if has_dev_test_entitlement(user):
            db.session.commit()
            print("[DEV TEST ENTITLEMENT] Scan count bypassed for local test user")
            return jsonify({"ok": True, "counted": False, "reason": "Development test entitlement"})

        old_count = int(user.scans_used or 0)
        if not _consume_scan_quota_atomic(user):
            db.session.rollback()
            return jsonify({
                "ok": False,
                "counted": False,
                "error": "Scan limit reached. Please upgrade your plan.",
                "reason": "Scan limit reached. Please upgrade your plan.",
            }), 403

        db.session.commit()
        db.session.refresh(user)

        print(f"COUNTED: {old_count} -> {user.scans_used}")
        print("="*50 + "\n")

        _log_scanner_latency("scanner_session_end", t_start, project_id=project_id, outcome="counted", stage="response", scan_session_id=session_id)
        return jsonify({
            "ok": True,
            "counted": True,
            "user_total": user.scans_used
        })
    except Exception as e:
        print(f"❌ ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
@app.route("/detect_track", methods=["POST"])
@csrf.exempt  # Public, unauthenticated scanner endpoint - no browser session/cookie to bind a CSRF token to.
def detect_track():
    """Tracking endpoint - with scan counting"""
    try:
        t_start = time.time()
        project_id = request.form.get("project_id", type=int)
        pair_id = request.form.get("pair_id", type=int)
        scan_session_id = request.form.get("scan_session_id", "")
        ok, retry_after = _check_rate_limit(
            "scanner_track",
            _rate_limit_key("detect_track", project_id, scan_session_id, pair_id),
        )
        if not ok:
            _log_scanner_latency("detect_track", t_start, project_id=project_id, pair_id=pair_id, outcome="rate_limited", stage="start", scan_session_id=scan_session_id)
            return _scanner_rate_limited_response(retry_after)

        test_file = request.files.get("test_image")
        
        if project_id is None or pair_id is None or test_file is None:
            return jsonify({"ok": False, "reason": "Missing project_id/pair_id/image"}), 400

        project = Project.query.get(project_id)
        if not project:
            return jsonify({"ok": False, "reason": "Project not found"}), 404
        if not _project_is_available(project):
            return jsonify({"ok": False, "reason": "Project is suspended or unavailable"}), 404

        pair_record = ProjectPair.query.filter_by(project_id=project_id, pair_index=pair_id).first()
        
        feats = load_features(project_id, pair_id)
        if feats is None:
            return jsonify({"ok": False, "reason": "Features missing"}), 404
        
        file_bytes = np.frombuffer(test_file.read(), np.uint8)
        img = cv2.imdecode(file_bytes, cv2.IMREAD_COLOR)
        t_after_read = time.time()
        print(f"⏱ detect_track read={(t_after_read - t_start):.3f}s")
        
        if img is None:
            return jsonify({"ok": False, "reason": "Invalid image"}), 400
        
        gray_small, scale, orig_w, orig_h = _resize_gray_for_detect(img)
        t_after_prep = time.time()
        print(f"⏱ detect_track prep={(t_after_prep - t_after_read):.3f}s")
        frame_w, frame_h = orig_w, orig_h
        
        orb = _orb_detect()
        test_kp, test_desc = orb.detectAndCompute(gray_small, None)
        t_after_detect = time.time()
        print(f"⏱ detect_track detect={(t_after_detect - t_after_prep):.3f}s (kp={len(test_kp) if test_kp else 0})")

        if test_kp is None or test_desc is None or len(test_kp) < MIN_TEST_KP:
            return jsonify({"ok": False, "reason": "Too few features", "frame_width": frame_w, "frame_height": frame_h}), 200

        # Limit descriptors for speed, but more descriptors improve match robustness
        N = min(len(test_kp), QUICK_DESC_LIMIT)
        if N < len(test_kp):
            test_kp = test_kp[:N]
            test_desc = test_desc[:N]
        
        # match_best_variant already internally tries ratio 0.75 → 0.80 → 0.90
        best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.75)

        t_after_match = time.time()
        print(f"⏱ detect_track match={(t_after_match - t_after_detect):.3f}s; good_matches={len(good_matches) if good_matches else 0}")
        
        if not good_matches or len(good_matches) < MIN_GOOD_MATCHES:
            return jsonify({"ok": False, "reason": "Not enough matches", "frame_width": frame_w, "frame_height": frame_h}), 200
        
        src_pts = []
        dst_pts = []
        
        for m in good_matches:
            tp = test_kp[m.queryIdx].pt
            sp = stored_kp[m.trainIdx]
            src_pts.append([float(sp[0]), float(sp[1])])
            dst_pts.append([float(tp[0]), float(tp[1])])
        
        src_arr = np.array(src_pts, dtype=np.float32)
        dst_arr = np.array(dst_pts, dtype=np.float32)
        
        H, mask = cv2.findHomography(src_arr, dst_arr, cv2.RANSAC, RANSAC_REPROJ)
        t_after_homography = time.time()
        print(f"⏱ detect_track homography={(t_after_homography - t_after_match):.3f}s; total={(t_after_homography - t_start):.3f}s")
        if H is None or mask is None:
            return jsonify({"ok": False, "reason": "Homography failed", "frame_width": frame_w, "frame_height": frame_h}), 200
        
        inliers = int(np.sum(mask))
        if inliers < max(MIN_INLIERS_ABS, int(MIN_INLIERS_RATIO * len(src_arr))):
            return jsonify({"ok": False, "reason": "Weak homography", "frame_width": frame_w, "frame_height": frame_h}), 200
        
        tw, th = feats["w"], feats["h"]
        quality_ok, homography_quality = evaluate_homography_quality(
            src_arr, dst_arr, H, mask, tw, th, frame_w, frame_h, scale=scale
        )
        if not quality_ok:
            return jsonify({
                "ok": False,
                "reason": f"Rejected pose: {homography_quality.get('reason')}",
                "frame_width": frame_w,
                "frame_height": frame_h,
                "homography_quality": homography_quality,
            }), 200

        rect = np.array([[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32).reshape(-1, 1, 2)
        pts = cv2.perspectiveTransform(rect, H).reshape(4, 2)
        corners = [(float(p[0] / scale), float(p[1] / scale)) for p in pts]
        
        if not valid_corners(corners, frame_w, frame_h):
            return jsonify({"ok": False, "reason": "Bad corners", "frame_width": frame_w, "frame_height": frame_h}), 200
        
        corners_out = [{"x": c[0], "y": c[1]} for c in corners]
        
        _log_scanner_latency("detect_track", t_start, project_id=project_id, pair_id=pair_id, outcome="accepted", stage="response", scan_session_id=scan_session_id)
        return jsonify({
            "ok": True,
            "corners": corners_out,
            "frame_width": frame_w,
            "frame_height": frame_h,
            "variant": best_tag,
            "inliers": inliers,
            "homography_quality": homography_quality,
            "marker_mode": getattr(pair_record, "marker_mode", None) or "full_image"
        }), 200
        
    except Exception as e:
        import traceback
        print(f"❌ ERROR in detect_track: {str(e)}")
        print(traceback.format_exc())
        
        return jsonify({
            "ok": False,
            "reason": "Tracking service temporarily unavailable"
 
       }), 500

@app.route("/project/<int:project_id>/preview")
@login_required
def project_preview(project_id):
    # Check if admin is viewing
    admin_view = request.args.get("admin_view") == "true"
    view_user_id = request.args.get("user_id", type=int)
    
    project = Project.query.get_or_404(project_id)
    
    # If admin viewing someone's project
    if admin_view and view_user_id and current_admin():
        # Admin is viewing - allow access
        user = User.query.get_or_404(view_user_id)
        print(f"👤 Admin viewing project {project_id} for user {user.id}")
    else:
        # Regular user viewing their own project
        user = current_user()
        if project.owner_user_id != user.id:
            abort(404)
    
    pairs = ProjectPair.query.filter_by(project_id=project.id).order_by(ProjectPair.pair_index).all()
    
    return render_template("user/project_preview.html",
                         user=user,
                         project=project,
                         pairs=pairs,
                         admin_view=admin_view)

@app.route("/admin_panel", methods=["GET"])
def admin_panel_redirect():
    """Redirect to admin login"""
    return redirect(url_for("admin_login_route"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login_route():
    if current_admin():
        return redirect(url_for("admin_dashboard"))
    
    if request.method == "GET":
        return render_template("admin/login.html")
    
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    admin = Admin.query.filter_by(email=email).first()
    generic_error = "Invalid email or password."

    if _admin_login_locked(email):
        if admin:
            log_admin_activity(admin.id, "login_locked", "Blocked admin login attempt during lockout")
        flash(generic_error, "error")
        return render_template("admin/login.html"), 429
    
    if not admin or not check_password_hash(admin.password_hash, password):
        _record_admin_login_failure(email, admin)
        flash(generic_error, "error")
        return render_template("admin/login.html")
    
    if not admin.is_active:
        _record_admin_login_failure(email, admin)
        flash(generic_error, "error")
        return render_template("admin/login.html")

    try:
        _validate_admin_role(admin.role)
    except ValueError:
        _record_admin_login_failure(email, admin)
        flash(generic_error, "error")
        return render_template("admin/login.html")
    
    _clear_admin_login_failures(email)
    admin_login(admin)
    admin.last_login_at = dt.utcnow()
    admin.login_count = (admin.login_count or 0) + 1
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "login", "Admin logged in")
    
    flash("Login successful.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/forgot-password", methods=["GET", "POST"])
def admin_forgot_password():
    if request.method == "GET":
        return render_template("admin/forgot_password.html")
    
    email = (request.form.get("email") or "").strip().lower()
    admin = Admin.query.filter_by(email=email).first()
    
    if admin:
        code = _create_otp(email, "admin_reset_password", minutes=10)
        otp_rec = _latest_otp(email, "admin_reset_password")
        try:
            send_admin_password_reset_email(email, code, minutes=10)
            if otp_rec:
                session["pending_admin_reset_challenge_id"] = otp_rec.challenge_id
        except Exception as e:
            if otp_rec:
                otp_rec.invalidated_at = dt.utcnow()
                db.session.commit()
            print(f"Email sending failed: {e}")
    
    # Always show success message for security
    flash("If an admin account exists with this email, a password reset link has been sent.", "success")
    session["pending_admin_reset_email"] = email
    return redirect(url_for("admin_reset_password"))

@app.route("/admin/reset-password", methods=["GET", "POST"])
def admin_reset_password():
    email = session.get("pending_admin_reset_email")
    if not email:
        flash("Please start from Forgot Password.", "error")
        return redirect(url_for("admin_forgot_password"))
    
    if request.method == "GET":
        return render_template("admin/reset_password.html", email=email)
    
    otp = (request.form.get("otp") or "").strip()
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""
    
    if new_password != confirm_password:
        flash("Passwords do not match.", "error")
        return render_template("admin/reset_password.html", email=email)
    
    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return render_template("admin/reset_password.html", email=email)
    
    if not _verify_otp(email, "admin_reset_password", otp, challenge_id=session.get("pending_admin_reset_challenge_id")):
        flash("Invalid or expired OTP. Password reset could not be completed.", "error")
        return render_template("admin/reset_password.html", email=email)
    
    admin = Admin.query.filter_by(email=email).first()
    if admin:
        admin.password_hash = generate_password_hash(new_password)
        OTPCode.query.filter_by(email=email, purpose="admin_reset_password", is_used=False).filter(
            OTPCode.invalidated_at.is_(None)
        ).update({OTPCode.invalidated_at: dt.utcnow()}, synchronize_session=False)
        db.session.commit()
        
        # Log activity
        log_admin_activity(admin.id, "password_reset", "Admin reset password via OTP")
    
    session.pop("pending_admin_reset_email", None)
    session.pop("pending_admin_reset_challenge_id", None)
    flash("Password updated successfully. Please login.", "success")
    return redirect(url_for("admin_login_route"))

@app.route("/admin/logout")
def admin_logout_route():
    admin = current_admin()
    if admin:
        log_admin_activity(admin.id, "logout", "Admin logged out")
    admin_logout()
    flash("Logged out successfully.", "success")
    return redirect(url_for("admin_login_route"))

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 2: Manage Admins (Super Admin Only)
# --------------------------------------------------------------------------------------------
@app.route("/admin/admins", methods=["GET"])
@require_admin_permission("superadmin.admins.manage")
def admin_manage_admins():
    admin = current_admin()
    admins = Admin.query.order_by(Admin.created_at.desc()).all()
    return render_template("admin/manage_admins.html", admin=admin, admins=admins)

@app.route("/admin/admins/add", methods=["GET", "POST"])
@require_admin_permission("superadmin.admins.manage")
def admin_add_admin():
    admin = current_admin()
    
    if request.method == "GET":
        return render_template("admin/add_admin.html", admin=admin)
    
    # Get form data
    email = (request.form.get("email") or "").strip().lower()
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    try:
        role = _validate_admin_role(request.form.get("role", "admin"))
    except ValueError:
        flash("Invalid admin role.", "error")
        return render_template("admin/add_admin.html", admin=admin)
    password = request.form.get("password") or ""
    
    # Validation
    if not email or not name or not password:
        flash("All fields are required.", "error")
        return render_template("admin/add_admin.html", admin=admin)
    
    if Admin.query.filter_by(email=email).first():
        flash("Admin with this email already exists.", "error")
        return render_template("admin/add_admin.html", admin=admin)
    
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return render_template("admin/add_admin.html", admin=admin)
    
    # Create admin
    new_admin = Admin(
        email=email,
        name=name,
        phone=phone,
        role=role,
        password_hash=generate_password_hash(password),
        is_active=True,
        created_by=admin.id
    )
    
    db.session.add(new_admin)
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "admin_add", f"Added new admin: {email} ({role})")
    
    flash("Admin added successfully.", "success")
    return redirect(url_for("admin_manage_admins"))

@app.route("/admin/admins/<int:admin_id>/edit", methods=["GET", "POST"])
@require_admin_permission("superadmin.admins.manage")
def admin_edit_admin(admin_id):
    admin = current_admin()
    target_admin = Admin.query.get_or_404(admin_id)
    
    if request.method == "GET":
        return render_template("admin/edit_admin.html", admin=admin, target_admin=target_admin)
    
    # Get form data
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    try:
        role = _validate_admin_role(request.form.get("role", "admin"))
    except ValueError:
        flash("Invalid admin role.", "error")
        return render_template("admin/edit_admin.html", admin=admin, target_admin=target_admin)
    is_active = request.form.get("is_active") == "on"
    
    # Validation
    if not name:
        flash("Name is required.", "error")
        return render_template("admin/edit_admin.html", admin=admin, target_admin=target_admin)
    
    can_change, reason = _can_change_active_superadmin(
        target_admin,
        admin,
        new_role=role,
        new_active=is_active,
        action="change",
    )
    if not can_change:
        flash(reason, "error")
        return render_template("admin/edit_admin.html", admin=admin, target_admin=target_admin)

    old_role = target_admin.role
    old_active = bool(target_admin.is_active)

    # Update admin
    target_admin.name = name
    target_admin.phone = phone
    target_admin.role = role
    target_admin.is_active = is_active
    
    # Update password if provided
    new_password = request.form.get("new_password")
    if new_password and len(new_password) >= 8:
        target_admin.password_hash = generate_password_hash(new_password)
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "admin_edit", f"Edited admin: {target_admin.email}")
    if old_role != role:
        log_admin_activity(admin.id, "admin_role_change", f"Changed admin role for {target_admin.email}: {old_role} -> {role}")
    if old_active != is_active:
        status = "activated" if is_active else "deactivated"
        log_admin_activity(admin.id, "admin_toggle", f"{status} admin: {target_admin.email}")
    
    flash("Admin updated successfully.", "success")
    return redirect(url_for("admin_manage_admins"))

@app.route("/admin/admins/<int:admin_id>/delete", methods=["POST"])
@require_admin_permission("superadmin.admins.manage")
def admin_delete_admin(admin_id):
    """Delete an admin account"""
    admin = current_admin()
    target_admin = Admin.query.get_or_404(admin_id)
    
    # Prevent self-deletion
    if target_admin.id == admin.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_manage_admins"))
    
    can_change, reason = _can_change_active_superadmin(
        target_admin,
        admin,
        new_active=False,
        action="delete",
    )
    if not can_change:
        flash(reason, "error")
        return redirect(url_for("admin_manage_admins"))
    
    # Log activity before deletion
    log_admin_activity(admin.id, "admin_delete", f"Deleted admin: {target_admin.email}")
    
    db.session.delete(target_admin)
    db.session.commit()
    
    flash("Admin deleted successfully.", "success")
    return redirect(url_for("admin_manage_admins"))

@app.route("/admin/admins/<int:admin_id>/toggle-status", methods=["POST"])
@require_admin_permission("superadmin.admins.manage")
def admin_toggle_admin_status(admin_id):
    admin = current_admin()
    target_admin = Admin.query.get_or_404(admin_id)
    
    # Prevent self-deactivation
    if target_admin.id == admin.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin_manage_admins"))
    
    can_change, reason = _can_change_active_superadmin(
        target_admin,
        admin,
        new_active=not target_admin.is_active,
        action="deactivate",
    )
    if not can_change:
        flash(reason, "error")
        return redirect(url_for("admin_manage_admins"))
    
    # Toggle status
    target_admin.is_active = not target_admin.is_active
    db.session.commit()
    
    # Log activity
    status = "activated" if target_admin.is_active else "deactivated"
    log_admin_activity(admin.id, "admin_toggle", f"{status} admin: {target_admin.email}")
    
    flash(f"Admin {status} successfully.", "success")
    return redirect(url_for("admin_manage_admins"))

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 3: Admin Dashboard
# --------------------------------------------------------------------------------------------
@app.route("/admin/dashboard", methods=["GET"])
@require_admin_permission("admin.dashboard.view")
def admin_dashboard():
    admin = current_admin()
    
    # ✅ GET ADMIN'S OWN PROJECTS (ordered by creation for sequential numbers)
    admin_projects = Project.query.filter_by(
        owner_admin_id=admin.id
    ).order_by(Project.created_at.asc()).all()  # Changed to asc
    
    # Get pairs count, scan count, and display number for each project
    for idx, p in enumerate(admin_projects, 1):
        p.pairs_count = ProjectPair.query.filter_by(project_id=p.id).count()
        p.scan_count = ScanLog.query.filter_by(project_id=p.id).count()
        p.display_number = idx  # Add sequential number
    # Get statistics
    total_users = User.query.count()
    active_users = User.query.filter_by(is_blocked=False, is_verified=True).count()
    blocked_users = User.query.filter_by(is_blocked=True).count()
    
    total_plans = SubscriptionPlan.query.count()
    active_plans = SubscriptionPlan.query.filter_by(is_active=True).count()
    
    total_projects = Project.query.count()
    total_scans = ScanLog.query.count()
    
    # Revenue statistics
    total_revenue = db.session.query(func.sum(PaymentOrder.total_amount)).filter_by(status="success").scalar() or 0
    active_subscriptions = PaymentOrder.query.filter(
        PaymentOrder.status == "success",
        PaymentOrder.subscription_end > dt.utcnow()
    ).count()
    
    # Recent payments
    recent_payments = PaymentOrder.query.filter_by(status="success").order_by(PaymentOrder.created_at.desc()).limit(10).all()
    
    # Recent users
    recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()
    
    # Plan-wise user count
    plan_stats = []
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    for plan in plans:
        user_count = User.query.filter_by(subscription_id=plan.id).count()
        plan_stats.append({
            'plan_name': plan.plan_name,
            'user_count': user_count,
            'color': 'primary' if plan.is_popular else 'secondary'
        })
    
    return render_template("admin/dashboard.html",
                         admin=admin,
                         admin_projects=admin_projects,  # ✅ ADMIN'S PROJECTS
                         total_users=total_users,
                         active_users=active_users,
                         blocked_users=blocked_users,
                         total_plans=total_plans,
                         active_plans=active_plans,
                         total_projects=total_projects,
                         total_scans=total_scans,
                         total_revenue=total_revenue,
                         active_subscriptions=active_subscriptions,
                         recent_payments=recent_payments,
                         recent_users=recent_users,
                         plan_stats=plan_stats,
                         current_time=dt.utcnow())

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 4: User Management
#
@app.route("/admin/my-projects", methods=["GET"])
@admin_required
def admin_my_projects():
    """Legacy route, kept for backward compatibility (bookmarks/links).

    admin_projects (/admin/projects) is now the single canonical admin
    project-management list - it's a strict superset of this narrower
    "my own projects" view. Redirect directly rather than maintaining two
    separate project-list implementations/templates.
    """
    return redirect(url_for("admin_projects", owner_type="admin"))
@app.route("/admin/users", methods=["GET"])
@require_admin_permission("admin.users.view")
def admin_users():
    admin = current_admin()
    
    # Get filter parameters
    status = request.args.get("status", "all")
    plan_id = request.args.get("plan_id", type=int)
    search = request.args.get("search", "").strip()
    
    # Build query
    query = User.query
    
    if status == "active":
        query = query.filter_by(is_blocked=False, is_verified=True)
    elif status == "blocked":
        query = query.filter_by(is_blocked=True)
    elif status == "unverified":
        query = query.filter_by(is_verified=False)
    
    if plan_id:
        query = query.filter_by(subscription_id=plan_id)
    
    if search:
        query = query.filter(
            or_(
                User.email.ilike(f"%{search}%"),
                User.first_name.ilike(f"%{search}%"),
                User.last_name.ilike(f"%{search}%"),
                User.phone.ilike(f"%{search}%")
            )
        )
    
    per_page = admin_page_size()
    pagination = query.order_by(User.created_at.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=per_page,
        error_out=False,
    )
    users = pagination.items
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    
    return render_template("admin/users.html", 
                         admin=admin, 
                         users=users, 
                         plans=plans,
                         pagination=pagination,
                         per_page=per_page,
                         status=status,
                         selected_plan_id=plan_id,
                         search=search)

@app.route("/admin/users/<int:user_id>", methods=["GET"])
@require_admin_permission("admin.users.view")
def admin_view_user(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    # Get user projects
    projects = Project.query.filter_by(owner_user_id=user.id).order_by(Project.created_at.desc()).all()
    
    # Get user payments
    payments = PaymentOrder.query.filter_by(user_id=user.id).order_by(PaymentOrder.created_at.desc()).all()
    
    # Get scan history
    scan_history = ScanLog.query.filter_by(user_id=user.id).order_by(ScanLog.created_at.desc()).limit(50).all()
    
    # Get trial details if exists
    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    
    return render_template("admin/view_user.html",
                         admin=admin,
                         user=user,
                         projects=projects,
                         payments=payments,
                         scan_history=scan_history,
                         trial=trial)

@app.route("/admin/users/<int:user_id>/toggle-block", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_toggle_block_user(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    # Toggle block status
    user.is_blocked = not user.is_blocked
    user.blocked_at = dt.utcnow() if user.is_blocked else None
    user.blocked_by = admin.id if user.is_blocked else None
    
    if user.is_blocked:
        user.blocked_reason = request.form.get("reason", "Admin action")
        action = "blocked"
    else:
        user.blocked_reason = None
        action = "unblocked"
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "user_block", f"{action} user: {user.email}")
    
    flash(f"User {action} successfully.", "success")
    return redirect(url_for("admin_view_user", user_id=user_id))

@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_reset_user_password(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    new_password = request.form.get("new_password") or ""
    
    if len(new_password) < 6:
        flash("Password must be at least 6 characters.", "error")
        return redirect(url_for("admin_view_user", user_id=user_id))
    
    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "user_password_reset", f"Reset password for user: {user.email}")
    
    flash("User password reset successfully.", "success")
    return redirect(url_for("admin_view_user", user_id=user_id))

@app.route("/admin/users/<int:user_id>/extend-trial", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_extend_user_trial(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    if not trial:
        flash("User doesn't have trial details.", "error")
        return redirect(url_for("admin_view_user", user_id=user_id))
    
    extension_days = request.form.get("extension_days", type=int, default=7)
    
    trial.trial_end = trial.trial_end + timedelta(days=extension_days)
    trial.trial_extended = True
    trial.extended_days += extension_days
    trial.extended_by = admin.id
    trial.extended_at = dt.utcnow()
    trial.extended_reason = request.form.get("reason", "Admin extension")
    
    # Update user subscription status
    if user.subscription_status == "expired":
        user.subscription_status = "trial"
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "trial_extension", f"Extended trial for {extension_days} days for user: {user.email}")
    
    flash(f"Trial extended by {extension_days} days successfully.", "success")
    return redirect(url_for("admin_view_user", user_id=user_id))

@app.route("/admin/users/<int:user_id>/add-scans", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_add_user_scans(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    additional_scans = request.form.get("additional_scans", type=int, default=0)
    
    if additional_scans <= 0:
        flash("Please enter a positive number of scans.", "error")
        return redirect(url_for("admin_view_user", user_id=user_id))
    
    user.subscribed_scan_limit += additional_scans
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "scan_add", f"Added {additional_scans} scans to user: {user.email}")
    
    flash(f"Added {additional_scans} scans to user's limit.", "success")
    return redirect(url_for("admin_view_user", user_id=user_id))

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 5: Plan Management
# --------------------------------------------------------------------------------------------
@app.route("/admin/plans", methods=["GET"])
@require_admin_permission("superadmin.plans.manage")
def admin_plans():
    admin = current_admin()
    plans = SubscriptionPlan.query.order_by(SubscriptionPlan.display_order.asc()).all()
    return render_template("admin/plans.html", admin=admin, plans=plans)
@app.route("/admin/project/<int:project_id>/preview")
@admin_required
def admin_project_preview(project_id):
    """Admin project preview page"""
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    
    # Check if this project belongs to the logged-in admin
    if project.owner_admin_id != admin.id:
        abort(404)
    
    pairs = ProjectPair.query.filter_by(project_id=project.id).order_by(ProjectPair.pair_index).all()
    
    # Add display number to project
    for idx, p in enumerate(pairs, 1):
        p.display_number = idx
    
    return render_template("admin/project_preview.html",
                         admin=admin,
                         project=project,
                         pairs=pairs,
                         is_admin=True)
@app.route("/admin/plans/add", methods=["GET", "POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_add_plan():
    admin = current_admin()
    
    if request.method == "GET":
        return render_template("admin/add_plan.html", admin=admin)
    
    try:
        # Get form data with proper handling
        plan_name = (request.form.get("plan_name") or "").strip()
        
        # Handle description (optional)
        plan_description = (request.form.get("plan_description") or "").strip()
        
        # Handle amount (optional)
        plan_amount_str = request.form.get("plan_amount", "").strip()
        plan_amount = float(plan_amount_str) if plan_amount_str else 0
        
        # Handle offer price (optional)
        offer_price_str = request.form.get("offer_price", "").strip()
        offer_price = float(offer_price_str) if offer_price_str else None
        
        # Handle currency
        currency = request.form.get("currency", "INR")
        
        # Handle duration
        duration_type = request.form.get("duration_type", "time")
        duration_value_str = request.form.get("duration_value", "").strip()
        duration_value = int(duration_value_str) if duration_value_str else None
        
        # Handle trial days (NEW)
        trial_days_str = request.form.get("trial_days", "").strip()
        trial_days = int(trial_days_str) if trial_days_str else None
        
        # Handle project limit (optional) - Store None for unlimited
        project_limit_str = request.form.get("total_project_limit", "").strip()
        unlimited_project = request.form.get("unlimited_projects") == "on"
        
        if unlimited_project:
            total_project_limit = None  # NULL in database for unlimited
        elif project_limit_str and project_limit_str.lower() != "unlimited":
            total_project_limit = int(project_limit_str)
        else:
            total_project_limit = None  # NULL for unlimited
        
        # Handle scan limit (optional) - Store None for unlimited
        scan_limit_str = request.form.get("total_scan_limit", "").strip()
        unlimited_scan = request.form.get("unlimited_scans") == "on"
        
        if unlimited_scan:
            total_scan_limit = None  # NULL in database for unlimited
        elif scan_limit_str and scan_limit_str.lower() != "unlimited":
            total_scan_limit = int(scan_limit_str)
        else:
            total_scan_limit = None  # NULL for unlimited

        # Handle pairs allowed per project (required)
        pairs_limit_str = request.form.get("max_pairs_per_project", "").strip()
        if not pairs_limit_str:
            flash("Pairs allowed per project is required and must be a positive integer.", "error")
            return render_template("admin/add_plan.html", admin=admin)

        try:
            max_pairs_per_project = int(pairs_limit_str)
            if max_pairs_per_project < 1:
                raise ValueError()
        except ValueError:
            flash("Pairs allowed per project must be a positive integer.", "error")
            return render_template("admin/add_plan.html", admin=admin)
        
        # Handle features
        features = request.form.get("features", "").strip()
        if features:
            features_list = [f.strip() for f in features.split("\n") if f.strip()]
        else:
            features_list = []
        
        # Handle checkboxes
        is_popular = request.form.get("is_popular") == "on"
        is_active = request.form.get("is_active") == "on"
        
        # Handle display order
        display_order_str = request.form.get("display_order", "").strip()
        display_order = int(display_order_str) if display_order_str else 0
        
        # Create plan
        plan = SubscriptionPlan(
            plan_name=plan_name,
            plan_description=plan_description,
            max_pairs_per_project=max_pairs_per_project,
            plan_amount=plan_amount,
            offer_price=offer_price,
            currency=currency,
            duration_type=duration_type,
            duration_value=duration_value,
            trial_days=trial_days,
            total_project_limit=total_project_limit,
            total_scan_limit=total_scan_limit,
            features_json=json.dumps(features_list),
            is_popular=is_popular,
            is_active=is_active,
            display_order=display_order,
            created_by=admin.id
        )
        
        db.session.add(plan)
        db.session.commit()
        
        # Log activity
        log_admin_activity(admin.id, "plan_add", f"Added new plan: {plan_name}")
        
        flash("Plan created successfully.", "success")
        return redirect(url_for("admin_plans"))
        
    except Exception as e:
        print(f"❌ Error in admin_add_plan: {e}")
        import traceback
        traceback.print_exc()
        db.session.rollback()
        flash(f"Error creating plan: {str(e)}", "error")
        return render_template("admin/add_plan.html", admin=admin)


@app.route("/admin/plans/<int:plan_id>/edit", methods=["GET", "POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_edit_plan(plan_id):
    try:
        admin = current_admin()
        plan = SubscriptionPlan.query.get_or_404(plan_id)
        
        if request.method == "GET":
            return render_template("admin/edit_plan.html", admin=admin, plan=plan)
        
        # Get form data with proper handling of empty values
        plan_name = (request.form.get("plan_name") or "").strip()
        if plan_name:
            plan.plan_name = plan_name
        
        description = (request.form.get("plan_description") or "").strip()
        if description:
            plan.plan_description = description
        
        # Handle amount (optional)
        plan_amount_str = request.form.get("plan_amount", "").strip()
        if plan_amount_str:
            try:
                plan.plan_amount = float(plan_amount_str)
            except ValueError:
                pass
        
        # Handle offer price (optional)
        offer_price_str = request.form.get("offer_price", "").strip()
        if offer_price_str:
            try:
                plan.offer_price = float(offer_price_str)
            except ValueError:
                pass
        
        # Handle duration type and value
        duration_type = request.form.get("duration_type")
        if duration_type:
            plan.duration_type = duration_type
        duration_value_str = request.form.get("duration_value", "").strip()
        if duration_value_str:
            try:
                plan.duration_value = int(duration_value_str)
            except ValueError:
                pass
        
        # Handle trial days
        trial_days_str = request.form.get("trial_days", "").strip()
        if trial_days_str:
            try:
                plan.trial_days = int(trial_days_str)
            except ValueError:
                pass
        
        # Handle project limit (optional) - Store None for unlimited
        project_limit_str = request.form.get("total_project_limit", "").strip()
        unlimited_project = request.form.get("unlimited_projects") == "on"
        
        if unlimited_project:
            plan.total_project_limit = None  # NULL for unlimited
        elif project_limit_str and project_limit_str.lower() != "unlimited":
            try:
                plan.total_project_limit = int(project_limit_str)
            except ValueError:
                pass
        
        # Handle scan limit (optional) - Store None for unlimited
        scan_limit_str = request.form.get("total_scan_limit", "").strip()
        unlimited_scan = request.form.get("unlimited_scans") == "on"
        
        if unlimited_scan:
            plan.total_scan_limit = None  # NULL for unlimited
        elif scan_limit_str and scan_limit_str.lower() != "unlimited":
            try:
                plan.total_scan_limit = int(scan_limit_str)
            except ValueError:
                pass

        # Handle pairs allowed per project (required)
        pairs_limit_str = request.form.get("max_pairs_per_project", "").strip()
        if not pairs_limit_str:
            flash("Pairs allowed per project is required and must be a positive integer.", "error")
            return redirect(url_for("admin_edit_plan", plan_id=plan.id))

        try:
            parsed_pairs = int(pairs_limit_str)
            if parsed_pairs < 1:
                raise ValueError()
            plan.max_pairs_per_project = parsed_pairs
        except ValueError:
            flash("Pairs allowed per project must be a positive integer.", "error")
            return redirect(url_for("admin_edit_plan", plan_id=plan.id))
        
        # Handle features (optional)
        features = request.form.get("features", "").strip()
        if features:
            features_list = [f.strip() for f in features.split("\n") if f.strip()]
            plan.features_json = json.dumps(features_list)
        
        # Handle checkboxes
        plan.is_popular = request.form.get("is_popular") == "on"
        plan.is_active = request.form.get("is_active") == "on"
        
        # Handle display order
        display_order_str = request.form.get("display_order", "").strip()
        if display_order_str:
            try:
                plan.display_order = int(display_order_str)
            except ValueError:
                pass
        
        db.session.commit()
        
        # Log activity
        log_admin_activity(admin.id, "plan_edit", f"Edited plan: {plan.plan_name}")
        
        flash("Plan updated successfully.", "success")
        return redirect(url_for("admin_plans"))
        
    except Exception as e:
        print(f"❌ Error in admin_edit_plan: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error updating plan: {str(e)}", "error")
        return redirect(url_for("admin_plans"))

@app.route("/admin/plans/<int:plan_id>/delete", methods=["POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_delete_plan(plan_id):
    try:
        print(f"🔍 DELETE ROUTE CALLED for plan_id: {plan_id}")
        admin = current_admin()
        plan = SubscriptionPlan.query.get_or_404(plan_id)
        print(f"🔍 Plan found: {plan.plan_name}")
        
        # Check if plan is in use
        user_count = User.query.filter_by(subscription_id=plan.id).count()
        if user_count > 0:
            flash(f"Cannot delete plan. It is currently used by {user_count} users.", "error")
            return redirect(url_for("admin_plans"))
        
        # Log activity before deletion
        log_admin_activity(admin.id, "plan_delete", f"Deleted plan: {plan.plan_name}")
        
        db.session.delete(plan)
        db.session.commit()
        
        flash("Plan deleted successfully.", "success")
        return redirect(url_for("admin_plans"))
    except Exception as e:
        print(f"❌ Error in delete route: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error deleting plan: {str(e)}", "error")
        return redirect(url_for("admin_plans"))

@app.route("/admin/plans/<int:plan_id>/toggle-status", methods=["POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_toggle_plan_status(plan_id):
    admin = current_admin()
    plan = SubscriptionPlan.query.get_or_404(plan_id)
    
    plan.is_active = not plan.is_active
    db.session.commit()
    
    # Log activity
    status = "activated" if plan.is_active else "deactivated"
    log_admin_activity(admin.id, "plan_toggle", f"{status} plan: {plan.plan_name}")
    
    flash(f"Plan {status} successfully.", "success")
    return redirect(url_for("admin_plans"))

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 6: Subscription Management
# --------------------------------------------------------------------------------------------
@app.route("/admin/subscriptions", methods=["GET"])
@require_admin_permission("superadmin.operations.view")
def admin_subscriptions():
    admin = current_admin()
    
    # Get filter parameters
    status = request.args.get("status", "all")
    plan_id = request.args.get("plan_id", type=int)
    search = request.args.get("search", "").strip()
    
    # Build query
    query = PaymentOrder.query.filter_by(status="success")
    
    if status == "active":
        query = query.filter(PaymentOrder.subscription_end > dt.utcnow())
    elif status == "expired":
        query = query.filter(PaymentOrder.subscription_end <= dt.utcnow())
    
    if plan_id:
        query = query.filter_by(plan_id=plan_id)
    
    if search:
        query = query.join(User).filter(
            or_(
                User.email.ilike(f"%{search}%"),
                User.first_name.ilike(f"%{search}%"),
                User.last_name.ilike(f"%{search}%")
            )
        )
    
    per_page = admin_page_size()
    pagination = query.order_by(PaymentOrder.created_at.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=per_page,
        error_out=False,
    )
    subscriptions = pagination.items
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    
    # Calculate remaining projects and scans for each subscription
    for sub in subscriptions:
        user = User.query.get(sub.user_id)
        sub.user = user
        sub.remaining_projects = user.remaining_projects if user else 0
        sub.remaining_scans = user.remaining_scans if user else 0
        sub.expiry_status = "active" if sub.subscription_end and sub.subscription_end > dt.utcnow() else "expired"
    
    return render_template("admin/subscriptions.html",
                         admin=admin,
                         subscriptions=subscriptions,
                         plans=plans,
                         status=status,
                         selected_plan_id=plan_id,
                         pagination=pagination,
                         per_page=per_page,
                         search=search) 

@app.route("/admin/subscriptions/<int:order_id>/extend", methods=["POST"])
@require_admin_permission("superadmin.settings.manage")
def admin_extend_subscription(order_id):
    admin = current_admin()
    payment_order = PaymentOrder.query.get_or_404(order_id)
    
    extension_months = request.form.get("extension_months", type=int, default=1)
    
    if extension_months <= 0:
        flash("Please enter a positive number of months.", "error")
        return redirect(url_for("admin_subscriptions"))
    
    # Extend subscription
    if payment_order.subscription_end:
        payment_order.subscription_end = payment_order.subscription_end + timedelta(days=30 * extension_months)
    else:
        payment_order.subscription_end = dt.utcnow() + timedelta(days=30 * extension_months)
    
    # Update user subscription
    user = User.query.get(payment_order.user_id)
    if user:
        user.subscription_expires_at = payment_order.subscription_end
        user.subscription_status = "active"
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "subscription_extend", 
                      f"Extended subscription by {extension_months} months for order: {payment_order.order_id}")
    
    flash(f"Subscription extended by {extension_months} months.", "success")
    return redirect(url_for("admin_subscriptions"))

@app.route("/admin/subscriptions/<int:order_id>/increase-limits", methods=["POST"])
@require_admin_permission("superadmin.settings.manage")
def admin_increase_subscription_limits(order_id):
    admin = current_admin()
    payment_order = PaymentOrder.query.get_or_404(order_id)
    
    additional_projects = request.form.get("additional_projects", type=int, default=0)
    additional_scans = request.form.get("additional_scans", type=int, default=0)
    
    if additional_projects <= 0 and additional_scans <= 0:
        flash("Please enter positive values for projects or scans.", "error")
        return redirect(url_for("admin_subscriptions"))
    
    # Update purchase limits
    if additional_projects > 0:
        payment_order.purchased_project_limit += additional_projects
    
    if additional_scans > 0:
        payment_order.purchased_scan_limit += additional_scans
    
    # Update user limits
    user = User.query.get(payment_order.user_id)
    if user:
        if additional_projects > 0:
            user.subscribed_project_limit += additional_projects
        
        if additional_scans > 0:
            user.subscribed_scan_limit += additional_scans
        
        user.subscription_status = "active"
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "limits_increase",
                      f"Increased limits for order {payment_order.order_id}: +{additional_projects} projects, +{additional_scans} scans")
    
    flash("Subscription limits increased successfully.", "success")
    return redirect(url_for("admin_subscriptions"))

@app.route("/admin/subscriptions/<int:order_id>/deactivate", methods=["POST"])
@require_admin_permission("superadmin.settings.manage")
def admin_deactivate_subscription(order_id):
    admin = current_admin()
    payment_order = PaymentOrder.query.get_or_404(order_id)
    
    # Mark subscription as expired
    payment_order.subscription_end = dt.utcnow() - timedelta(days=1)
    
    # Update user status
    user = User.query.get(payment_order.user_id)
    if user:
        user.subscription_status = "expired"
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "subscription_deactivate",
                      f"Deactivated subscription for order: {payment_order.order_id}")
    
    flash("Subscription deactivated successfully.", "success")
    return redirect(url_for("admin_subscriptions"))

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 7: Payment Management
# --------------------------------------------------------------------------------------------
@app.route("/admin/payments", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_payments():
    admin = current_admin()
    
    # Get filter parameters
    status = request.args.get("status", "all")
    method = request.args.get("method", "all")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    search = request.args.get("search", "").strip()
    
    # Build query
    query = PaymentOrder.query
    
    if status != "all":
        query = query.filter_by(status=status)
    
    if method != "all":
        query = query.filter_by(payment_method=method)
    
    if start_date:
        start_dt = dt.strptime(start_date, "%Y-%m-%d")
        query = query.filter(PaymentOrder.created_at >= start_dt)
    
    if end_date:
        end_dt = dt.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(PaymentOrder.created_at < end_dt)
    
    if search:
        query = query.join(User).filter(
            or_(
                PaymentOrder.order_id.ilike(f"%{search}%"),
                PaymentOrder.razorpay_order_id.ilike(f"%{search}%"),
                PaymentOrder.razorpay_payment_id.ilike(f"%{search}%"),
                User.email.ilike(f"%{search}%"),
                User.first_name.ilike(f"%{search}%"),
                User.last_name.ilike(f"%{search}%")
            )
        )
    
    per_page = admin_page_size()
    pagination = query.order_by(PaymentOrder.created_at.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=per_page,
        error_out=False,
    )
    payments = pagination.items
    
    # Calculate totals
    total_amount = sum(p.total_amount for p in payments)
    success_count = sum(1 for p in payments if p.status == "success")
    
    return render_template("admin/payments.html",
                         admin=admin,
                         payments=payments,
                         status=status,
                         method=method,
                         start_date=start_date,
                         end_date=end_date,
                         search=search,
                         pagination=pagination,
                         per_page=per_page,
                         total_amount=total_amount,
                         success_count=success_count)

@app.route("/admin/payments/<int:payment_id>", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_view_payment(payment_id):
    admin = current_admin()
    payment = PaymentOrder.query.get_or_404(payment_id)
    
    user = User.query.get(payment.user_id)
    plan = SubscriptionPlan.query.get(payment.plan_id)
    
    return render_template("admin/view_payment.html",
                         admin=admin,
                         payment=payment,
                         user=user,
                         plan=plan)

def _safe_display_filename(value):
    if not value:
        return "Not stored"
    return os.path.basename(str(value))

def _owner_display(project, owner_user=None, owner_admin=None):
    if project.owner_user_id:
        return {
            "type": "User",
            "id": project.owner_user_id,
            "name": owner_user.full_name if owner_user else "Missing user",
            "email": owner_user.email if owner_user else "Owner record missing",
        }
    if project.owner_admin_id:
        return {
            "type": "Admin",
            "id": project.owner_admin_id,
            "name": owner_admin.name if owner_admin else "Missing admin",
            "email": owner_admin.email if owner_admin else "Owner record missing",
        }
    return {"type": "Unknown", "id": None, "name": "No owner recorded", "email": "-"}

def _project_readiness_summary(pair_count, ready_pairs, failed_pairs, processing_pairs=0):
    pair_count = int(pair_count or 0)
    ready_pairs = int(ready_pairs or 0)
    failed_pairs = int(failed_pairs or 0)
    processing_pairs = int(processing_pairs or 0)
    if pair_count == 0:
        return "No pairs"
    if failed_pairs:
        return f"{failed_pairs} failed"
    if ready_pairs == pair_count:
        return "Ready"
    if processing_pairs:
        return f"{processing_pairs} processing"
    if ready_pairs == 0:
        return "Pending"
    return f"{ready_pairs}/{pair_count} ready"

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 8: Project Monitoring
# --------------------------------------------------------------------------------------------
@app.route("/admin/user-profiles", methods=["GET"])
@require_admin_permission("admin.users.view")
def admin_user_profiles():
    """Display all user profiles."""
    admin = current_admin()
    
    # Get filter parameters
    status = request.args.get("status", "all")
    plan_id = request.args.get("plan_id", type=int)
    search = request.args.get("search", "").strip()
    
    # Build query for users only
    query = User.query
    
    # Apply status filters
    if status == "active":
        query = query.filter_by(is_blocked=False)
    elif status == "blocked":
        query = query.filter_by(is_blocked=True)
    elif status == "trial":
        query = query.filter_by(subscription_status="trial")
    elif status == "paid":
        query = query.filter_by(subscription_status="active")
    
    # Apply plan filter
    if plan_id:
        query = query.filter_by(subscription_id=plan_id)
    
    # Apply search filter
    if search:
        query = query.filter(
            or_(
                User.email.ilike(f"%{search}%"),
                User.first_name.ilike(f"%{search}%"),
                User.last_name.ilike(f"%{search}%")
            )
        )
    
    per_page = admin_page_size()
    pagination = query.order_by(User.created_at.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=per_page,
        error_out=False,
    )
    users = pagination.items
    if users:
        project_counts = dict(
            db.session.query(Project.owner_user_id, func.count(Project.id))
            .filter(Project.owner_user_id.in_([user.id for user in users]))
            .group_by(Project.owner_user_id)
            .all()
        )
        for user in users:
            user.live_project_count = int(project_counts.get(user.id, 0))
    
    # Get all plans for filter dropdown
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    
    return render_template(
        "admin/user_profiles.html",
        admin=admin,
        users=users,
        plans=plans,
        status=status,
        search=search,
        selected_plan_id=plan_id,
        pagination=pagination,
        per_page=per_page
    )

@app.route("/admin/projects", methods=["GET"])
@require_admin_permission("admin.projects.view")
def admin_projects():
    admin = current_admin()
    search = request.args.get("search", "").strip()
    owner_type = request.args.get("owner_type", "all")
    readiness = request.args.get("readiness", "all")
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 25, type=int), 1), 100)
    owner_admin = aliased(Admin)

    pair_counts = (
        db.session.query(
            ProjectPair.project_id.label("project_id"),
            func.count(ProjectPair.id).label("pair_count"),
            func.sum(case((ProjectPair.is_processed == True, 1), else_=0)).label("ready_pair_count"),
            func.sum(case((ProjectPair.processing_status == "failed", 1), else_=0)).label("failed_pair_count"),
            func.sum(case((ProjectPair.processing_status == "processing", 1), else_=0)).label("processing_pair_count"),
        )
        .group_by(ProjectPair.project_id)
        .subquery()
    )
    scan_counts = (
        db.session.query(
            ScanLog.project_id.label("project_id"),
            func.count(ScanLog.id).label("scan_count"),
            func.sum(case((ScanLog.is_successful == True, 1), else_=0)).label("successful_scan_count"),
            func.sum(case((ScanLog.is_successful == False, 1), else_=0)).label("failed_scan_count"),
        )
        .group_by(ScanLog.project_id)
        .subquery()
    )

    query = (
        db.session.query(
            Project,
            User,
            owner_admin,
            pair_counts.c.pair_count,
            pair_counts.c.ready_pair_count,
            pair_counts.c.failed_pair_count,
            pair_counts.c.processing_pair_count,
            scan_counts.c.scan_count,
            scan_counts.c.successful_scan_count,
            scan_counts.c.failed_scan_count,
        )
        .outerjoin(User, Project.owner_user_id == User.id)
        .outerjoin(owner_admin, Project.owner_admin_id == owner_admin.id)
        .outerjoin(pair_counts, Project.id == pair_counts.c.project_id)
        .outerjoin(scan_counts, Project.id == scan_counts.c.project_id)
    )

    if owner_type == "user":
        query = query.filter(Project.owner_user_id.isnot(None))
    elif owner_type == "admin":
        query = query.filter(Project.owner_admin_id.isnot(None))

    if readiness == "ready":
        query = query.filter(func.coalesce(pair_counts.c.pair_count, 0) > 0)
        query = query.filter(func.coalesce(pair_counts.c.pair_count, 0) == func.coalesce(pair_counts.c.ready_pair_count, 0))
    elif readiness == "processing":
        query = query.filter(func.coalesce(pair_counts.c.processing_pair_count, 0) > 0)
    elif readiness == "pending":
        query = query.filter(func.coalesce(pair_counts.c.pair_count, 0) > 0)
        query = query.filter(func.coalesce(pair_counts.c.ready_pair_count, 0) == 0)
        query = query.filter(func.coalesce(pair_counts.c.failed_pair_count, 0) == 0)
        query = query.filter(func.coalesce(pair_counts.c.processing_pair_count, 0) == 0)
    elif readiness == "failed":
        query = query.filter(func.coalesce(pair_counts.c.failed_pair_count, 0) > 0)

    if search:
        search_terms = []
        search_id = int(search) if search.isdigit() else None
        if search_id is not None and Project.query.filter(Project.id == search_id).first():
            search_terms.append(Project.id == search_id)
        else:
            search_terms.extend([
                Project.name.ilike(f"%{search}%"),
                User.email.ilike(f"%{search}%"),
                User.first_name.ilike(f"%{search}%"),
                User.last_name.ilike(f"%{search}%"),
                owner_admin.email.ilike(f"%{search}%"),
                owner_admin.name.ilike(f"%{search}%"),
            ])
            if search_id is not None:
                search_terms.extend([
                    Project.id == search_id,
                    Project.owner_user_id == search_id,
                    Project.owner_admin_id == search_id,
                ])
        query = query.filter(or_(*search_terms))

    pagination = query.order_by(Project.created_at.desc(), Project.id.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )
    project_rows = []
    for project, owner_user, row_admin, pair_count, ready_pairs, failed_pairs, processing_pairs, scan_count, success_scans, failed_scans in pagination.items:
        owner = _owner_display(project, owner_user, row_admin)
        project_rows.append({
            "project": project,
            "owner": owner,
            "pair_count": int(pair_count or 0),
            "ready_pair_count": int(ready_pairs or 0),
            "failed_pair_count": int(failed_pairs or 0),
            "processing_pair_count": int(processing_pairs or 0),
            "readiness_summary": _project_readiness_summary(pair_count, ready_pairs, failed_pairs, processing_pairs),
            "qr_ready": bool(project.qr_code_path or project.qr_code_filename),
            "scan_count": int(scan_count or 0),
            "successful_scan_count": int(success_scans or 0),
            "failed_scan_count": int(failed_scans or 0),
        })

    return render_template(
        "admin/projects.html",
        admin=admin,
        project_rows=project_rows,
        pagination=pagination,
        search=search,
        owner_type=owner_type,
        readiness=readiness,
        per_page=per_page,
    )

@app.route("/admin/projects/<int:project_id>", methods=["GET"])
@require_admin_permission("admin.projects.view")
def admin_view_project(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    owner_user = User.query.get(project.owner_user_id) if project.owner_user_id else None
    owner_admin = Admin.query.get(project.owner_admin_id) if project.owner_admin_id else None
    owner = _owner_display(project, owner_user, owner_admin)

    pair_count = ProjectPair.query.filter_by(project_id=project_id).count()
    pairs = (
        ProjectPair.query
        .filter_by(project_id=project_id)
        .order_by(ProjectPair.pair_index.asc())
        .limit(100)
        .all()
    )
    for pair in pairs:
        pair.safe_image_filename = _safe_display_filename(pair.image_filename)
        pair.safe_video_filename = _safe_display_filename(pair.video_filename)
        pair.recognition_ready = pair.feature_extraction_status == "extracted" or bool(pair.is_processed)

    scan_summary = (
        db.session.query(
            func.count(ScanLog.id),
            func.sum(case((ScanLog.is_successful == True, 1), else_=0)),
            func.sum(case((ScanLog.is_successful == False, 1), else_=0)),
        )
        .filter(ScanLog.project_id == project_id)
        .first()
    )
    total_scans = int(scan_summary[0] or 0)
    successful_scans = int(scan_summary[1] or 0)
    failed_scans = int(scan_summary[2] or 0)
    scan_history = (
        ScanLog.query
        .filter_by(project_id=project_id)
        .order_by(ScanLog.created_at.desc())
        .limit(25)
        .all()
    )

    return render_template("admin/view_project.html",
                         admin=admin,
                         project=project,
                         owner=owner,
                         pairs=pairs,
                         pair_count=pair_count,
                         scan_history=scan_history,
                         total_scans=total_scans,
                         successful_scans=successful_scans,
                         failed_scans=failed_scans,
                         safe_qr_filename=_safe_display_filename(project.qr_code_filename or project.qr_code_path),
                         qr_ready=bool(project.qr_code_path or project.qr_code_filename))

@app.route("/admin/projects/<int:project_id>/toggle-status", methods=["POST"])
@require_admin_permission("admin.projects.suspend")
def admin_toggle_project_status(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    
    project.is_active = not project.is_active
    db.session.commit()
    
    # Log activity
    status = "activated" if project.is_active else "deactivated"
    log_admin_activity(admin.id, "project_toggle", f"{status} project: {project.name} (ID: {project.id})")
    
    flash(f"Project {status} successfully.", "success")
    return redirect(url_for("admin_view_project", project_id=project_id))

@app.route("/admin/projects/<int:project_id>/suspend", methods=["POST"])
@require_admin_permission("admin.projects.suspend")
def admin_suspend_project(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    if not project.is_active:
        flash("Project is already suspended.", "info")
        return redirect(url_for("admin_view_project", project_id=project_id))
    project.is_active = False
    db.session.commit()
    log_admin_activity(admin.id, "project_suspend", f"Suspended project: {project.name} (ID: {project.id})")
    flash("Project suspended. Public scanner and media access are blocked.", "success")
    return redirect(url_for("admin_view_project", project_id=project_id))

@app.route("/admin/projects/<int:project_id>/restore", methods=["POST"])
@require_admin_permission("admin.projects.suspend")
def admin_restore_project(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    if project.is_active:
        flash("Project is already active.", "info")
        return redirect(url_for("admin_view_project", project_id=project_id))
    project.is_active = True
    db.session.commit()
    log_admin_activity(admin.id, "project_restore", f"Restored project: {project.name} (ID: {project.id})")
    flash("Project restored. Normal scanner and media access are available again.", "success")
    return redirect(url_for("admin_view_project", project_id=project_id))

@app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
@require_admin_permission("superadmin.repair.execute")
def admin_delete_project(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    
    # Get user before deletion for logging
    user = User.query.get(project.owner_user_id) if project.owner_user_id else None
    
    # Delete project files and database records
    _delete_project_files_and_rows(project)
    
    # Update user project count if applicable
    if user:
        user.projects_used = max(0, (user.projects_used or 0) - 1)
        db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "project_delete", 
                      f"Deleted project: {project.name} (ID: {project.id}) owned by {user.email if user else 'unknown'}")
    
    flash("Project deleted successfully.", "success")
    return redirect(url_for("admin_projects"))

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 9: Scan Usage Control
# --------------------------------------------------------------------------------------------
@app.route("/admin/scans", methods=["GET"])
@require_admin_permission("admin.processing.view")
def admin_scans():
    admin = current_admin()
    
    # Get filter parameters
    user_id = request.args.get("user_id", type=int)
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    
    # Build query
    query = db.session.query(
        User.id,
        User.email,
        User.first_name,
        User.last_name,
        func.count(ScanLog.id).label('total_scans'),
        func.sum(case((ScanLog.is_successful == True, 1), else_=0)).label('successful_scans'),
        func.max(ScanLog.created_at).label('last_scan_date')
    ).join(ScanLog, User.id == ScanLog.user_id, isouter=True)
    
    if user_id:
        query = query.filter(User.id == user_id)
    
    if start_date:
        start_dt = dt.strptime(start_date, "%Y-%m-%d")
        query = query.filter(ScanLog.created_at >= start_dt)
    
    if end_date:
        end_dt = dt.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(ScanLog.created_at < end_dt)
    
    scan_stats = query.group_by(User.id).order_by(func.count(ScanLog.id).desc()).all()
    
    # Get users for filter dropdown
    users = User.query.order_by(User.email).all()
    
    return render_template("admin/scans.html",
                         admin=admin,
                         scan_stats=scan_stats,
                         users=users,
                         selected_user_id=user_id,
                         start_date=start_date,
                         end_date=end_date)

@app.route("/admin/scans/user/<int:user_id>", methods=["GET"])
@require_admin_permission("admin.processing.view")
def admin_user_scans(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    # Get user scan history
    scan_history = ScanLog.query.filter_by(user_id=user_id).order_by(ScanLog.created_at.desc()).all()
    
    # Get scan statistics
    total_scans = len(scan_history)
    successful_scans = sum(1 for scan in scan_history if scan.is_successful)
    failed_scans = total_scans - successful_scans
    
    # Get recent scans (last 7 days)
    seven_days_ago = dt.utcnow() - timedelta(days=7)
    recent_scans = [scan for scan in scan_history if scan.created_at >= seven_days_ago]
    
    return render_template("admin/user_scans.html",
                         admin=admin,
                         user=user,
                         scan_history=scan_history,
                         total_scans=total_scans,
                         successful_scans=successful_scans,
                         failed_scans=failed_scans,
                         recent_scans=recent_scans)

@app.route("/admin/scans/<int:user_id>/update-limit", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_update_scan_limit(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    new_scan_limit = request.form.get("new_scan_limit", type=int)
    
    if new_scan_limit is None or new_scan_limit < 0:
        flash("Please enter a valid scan limit.", "error")
        return redirect(url_for("admin_user_scans", user_id=user_id))
    
    old_limit = user.subscribed_scan_limit
    user.subscribed_scan_limit = new_scan_limit
    
    # If user was at limit and we increased it, update status
    if user.subscription_status == "limit_reached":
        if user.subscribed_scan_limit in (None, 0) or user.remaining_scans > 0:
            user.subscription_status = "active"
    
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "scan_limit_update",
                      f"Updated scan limit for {user.email}: {old_limit} → {new_scan_limit}")
    
    flash(f"Scan limit updated from {old_limit} to {new_scan_limit}.", "success")
    return redirect(url_for("admin_user_scans", user_id=user_id))

@app.route("/admin/scans/<int:user_id>/grant-extra", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_grant_extra_scans(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    extra_scans = request.form.get("extra_scans", type=int, default=0)
    
    if extra_scans <= 0:
        flash("Please enter a positive number of scans.", "error")
        return redirect(url_for("admin_user_scans", user_id=user_id))
    
    user.subscribed_scan_limit += extra_scans
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "extra_scans_grant",
                      f"Granted {extra_scans} extra scans to {user.email}")
    
    flash(f"Granted {extra_scans} extra scans to user.", "success")
    return redirect(url_for("admin_user_scans", user_id=user_id))

@app.route("/admin/scans/<int:user_id>/lock-scanner", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_lock_user_scanner(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    
    # Set scans used to limit to prevent further scans
    user.scans_used = user.subscribed_scan_limit
    user.subscription_status = "limit_reached"
    db.session.commit()
    
    # Log activity
    log_admin_activity(admin.id, "scanner_lock", f"Locked scanner for user: {user.email}")
    
    flash("Scanner locked for this user. They cannot perform more scans until limit is increased.", "success")
    return redirect(url_for("admin_user_scans", user_id=user_id))

# --------------------------------------------------------------------------------------------
# Admin Routes - System Settings
# --------------------------------------------------------------------------------------------
@app.route("/admin/settings", methods=["GET", "POST"])
@require_admin_permission("superadmin.settings.manage")
def admin_settings():
    admin = current_admin()
    
    if request.method == "POST":
        # Update trial settings
        free_trial_projects = request.form.get("free_trial_projects", type=int)
        free_trial_scans = request.form.get("free_trial_scans", type=int)
        free_trial_days = request.form.get("free_trial_days", type=int)
        razorpay_enabled = request.form.get("razorpay_enabled") == "on"
        
        set_system_config("free_trial_projects", free_trial_projects, "integer", "Free trial project limit")
        set_system_config("free_trial_scans", free_trial_scans, "integer", "Free trial scan limit")
        set_system_config("free_trial_days", free_trial_days, "integer", "Free trial duration in days")
        set_system_config("razorpay_enabled", razorpay_enabled, "boolean", "Enable Razorpay payments")
        
        # Update general settings
        site_name = request.form.get("site_name", "").strip()
        site_url = request.form.get("site_url", "").strip()
        support_email = request.form.get("support_email", "").strip()
        currency = request.form.get("currency", "INR")
        
        set_system_config("site_name", site_name, "string", "Website name")
        set_system_config("site_url", site_url, "string", "Website URL")
        set_system_config("support_email", support_email, "string", "Support email")
        set_system_config("currency", currency, "string", "Default currency")
        
        # Update security settings
        max_login_attempts = request.form.get("max_login_attempts", type=int)
        session_timeout = request.form.get("session_timeout", type=int)
        
        set_system_config("max_login_attempts", max_login_attempts, "integer", "Maximum login attempts")
        set_system_config("session_timeout", session_timeout, "integer", "Session timeout in minutes")
        
        # Update other settings
        maintenance_mode = request.form.get("maintenance_mode") == "on"
        allow_registration = request.form.get("allow_registration") == "on"
        require_email_verification = request.form.get("require_email_verification") == "on"
        login_notifications = request.form.get("login_notifications") == "on"
        payment_mode = request.form.get("payment_mode", "test")
        
        set_system_config("maintenance_mode", maintenance_mode, "boolean", "Maintenance mode")
        set_system_config("allow_registration", allow_registration, "boolean", "Allow user registration")
        set_system_config("require_email_verification", require_email_verification, "boolean", "Require email verification")
        set_system_config("login_notifications", login_notifications, "boolean", "Login notifications")
        set_system_config("payment_mode", payment_mode, "string", "Payment mode")
        
        # Log activity
        log_admin_activity(admin.id, "settings_update", "Updated system settings")
        
        flash("Settings updated successfully.", "success")
        return redirect(url_for("admin_settings"))
    
    return render_template("admin/settings.html", 
                         admin=admin,
                         get_system_config=get_system_config) 

# --------------------------------------------------------------------------------------------
# Admin Routes - Activity Logs
# --------------------------------------------------------------------------------------------
@app.route("/admin/activity-logs", methods=["GET"])
@require_admin_permission("superadmin.audit.view")
def admin_activity_logs():
    admin = current_admin()
    
    # Get filter parameters
    activity_type = request.args.get("activity_type", "all")
    admin_id = request.args.get("admin_id", type=int)
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    
    # Build query
    query = AdminActivity.query
    
    if activity_type != "all":
        query = query.filter_by(activity_type=activity_type)
    
    if admin_id:
        query = query.filter_by(admin_id=admin_id)
    
    if start_date:
        start_dt = dt.strptime(start_date, "%Y-%m-%d")
        query = query.filter(AdminActivity.activity_at >= start_dt)
    
    if end_date:
        end_dt = dt.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
        query = query.filter(AdminActivity.activity_at < end_dt)
    
    per_page = admin_page_size()
    pagination = query.order_by(AdminActivity.activity_at.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=per_page,
        error_out=False,
    )
    activities = pagination.items
    
    # Get admins for filter dropdown
    admins = Admin.query.order_by(Admin.name).all()
    
    # Get unique activity types
    activity_types = db.session.query(AdminActivity.activity_type).distinct().all()
    activity_types = [at[0] for at in activity_types]
    
    return render_template("admin/activity_logs.html",
                         admin=admin,
                         activities=activities,  # Just pass all activities
                         admins=admins,
                         activity_types=activity_types,
                         selected_activity_type=activity_type,
                         selected_admin_id=admin_id,
                         start_date=start_date,
                         end_date=end_date,
                         pagination=pagination,
                         per_page=per_page)

# --------------------------------------------------------------------------------------------
# Admin Routes - Project Creation (Unlimited & Free for Admin)
# --------------------------------------------------------------------------------------------
@app.route("/admin/projects/create", methods=["GET"])
@admin_required
def admin_create_project_page():
    """GET: Show the project creation form for admin"""
    admin = current_admin()
    return render_template(
        "user/user_create_project.html",
        user=admin,
        is_admin=True,
        unlimited_pairs=True,
        max_pairs_per_project=None,
        get_system_config=get_system_config,
        video_upload_warnings=VIDEO_UPLOAD_WARNINGS,
        crop_debug_enabled=(
            request.args.get("crop_debug") == "1"
            and (app.config.get("TESTING") or os.environ.get("FLASK_ENV") == "development")
        ),
        upload_debug_enabled=(app.config.get("TESTING") or os.environ.get("FLASK_ENV") == "development"),
    )
@app.route("/admin/projects/upload", methods=["POST"])
@admin_required
def admin_handle_upload():
    """Admin project creation with fast multi-pair processing"""
    admin = current_admin()
    
    t0 = time.time()
    
    # Get project name and uploaded files
    name = request.form.get("name", "Untitled Project")
    images = request.files.getlist("images")
    videos = request.files.getlist("videos")

    # Validation
    if not images or not videos or len(images) != len(videos):
        flash("Error: Please upload equal number of images and videos", "error")
        return redirect(url_for("admin_create_project_page"))
    
    
    
    # Validate every file from its actual content BEFORE the project row or
    # any pair is created (P0D) - matches the user-upload path. All-or-nothing.
    validated_media = []
    try:
        for i, (image_file, video_file) in enumerate(zip(images, videos)):
            try:
                img_temp, img_ext = validate_image(
                    image_file, TMP_UPLOADS_DIR, MAX_IMAGE_SIZE, MAX_IMAGE_DIMENSION_PX, MAX_IMAGE_PIXELS
                )
            except UploadValidationError as exc:
                app.logger.warning(f"Admin upload rejected (image, pair {i}): {exc.detail}")
                raise
            try:
                vid_temp, vid_ext = validate_video(
                    video_file, TMP_UPLOADS_DIR, MAX_VIDEO_SIZE, MAX_VIDEO_DURATION_SECONDS
                )
            except UploadValidationError as exc:
                _safe_remove(img_temp)
                app.logger.warning(f"Admin upload rejected (video, pair {i}): {exc.detail}")
                raise
            validated_media.append({"image_temp": img_temp, "image_ext": img_ext, "video_temp": vid_temp, "video_ext": vid_ext})
    except UploadValidationError as exc:
        for item in validated_media:
            _safe_remove(item["image_temp"])
            _safe_remove(item["video_temp"])
        flash(exc.safe_message, "error")
        return redirect(url_for("admin_create_project_page"))

    # Create project
    # Assign a per-admin project index (persisted) so admin projects also have stable numbers
    try:
        max_admin_index = db.session.query(func.max(Project.user_project_index)).filter(
            Project.owner_admin_id == admin.id
        ).scalar()
        admin_project_index = (int(max_admin_index) if max_admin_index and int(max_admin_index) > 0 else 0) + 1
    except Exception:
        admin_project_index = 1

    project = Project(
        name=name,
        owner_admin_id=admin.id,
        owner_user_id=None,
        user_project_index=admin_project_index
    )
    db.session.add(project)
    db.session.commit()
    
    # Move ALL already-validated files into place
    pairs_data = []
    for i, (image_file, video_file) in enumerate(zip(images, videos)):
        media = validated_media[i]
        # Generate filenames
        img_filename = f"{project.id}_{i}.jpg"
        vid_filename = f"{project.id}_{i}{media['video_ext']}"

        # ✅ CHANGE 1: Save to ADMIN folders
        img_path = os.path.join(ADMIN_IMAGES_DIR, img_filename)  # ← CHANGED
        os.replace(media["image_temp"], img_path)

        vid_path = os.path.join(ADMIN_VIDEOS_DIR, vid_filename)  # ← CHANGED
        os.replace(media["video_temp"], vid_path)
        
        # ✅ CHANGE 2: Use admin image URL
        pair = ProjectPair(
            project_id=project.id,
            pair_index=i,
            image_filename=img_filename,
            video_filename=vid_filename,
            image_path=f"/admin/image/{project.id}/{i}",  # ← CHANGED
            is_processed=False,
            processing_status="uploaded",
            feature_extraction_status="pending",
            processing_error=None
        )
        db.session.add(pair)
        
        pairs_data.append({
            "pair_index": i,
            "image_filename": img_filename,
            "video_filename": vid_filename
        })
    
    db.session.commit()
    
    # Generate QR code
    admin_name = admin.name or admin.email.split("@")[0]
    
    scanner_url = url_for(
        "scanner",
        project_id=project.id,
        admin_id=admin.id,
        admin_name=admin_name,
        _external=True,
        _scheme="https"
    )
    
    qr_filename = f"project_{project.id}_admin.png"
    # ✅ CHANGE 3: Save QR to ADMIN folder
    qr_path = os.path.join(ADMIN_QR_DIR, qr_filename)  # ← CHANGED
    
    ok = generate_custom_qr(scanner_url, qr_path, project_name=project.name)
    if not ok or not os.path.exists(qr_path):
        generate_basic_qr(scanner_url, "black", "white", qr_path, project_name=project.name)
    
    # Update project
    project.scanner_url = scanner_url
    project.qr_code_filename = qr_filename
    # ✅ CHANGE 4: Use admin QR URL
    project.qr_code_path = f"/admin/qr/{qr_filename}"  # ← CHANGED
    db.session.commit()
    
    # Start background processing for admin project
    try:
        import threading
        from concurrent.futures import ThreadPoolExecutor
        
        def process_single_pair_bg_admin(project_id, pair_index, img_filename):
            """Process ONE admin pair in background"""
            try:
                # Mark as processing
                with app.app_context():
                    pair = ProjectPair.query.filter_by(
                        project_id=project_id,
                        pair_index=pair_index
                    ).first()
                    if pair:
                        pair.processing_status = "processing"
                        pair.feature_extraction_status = "extracting"
                        db.session.commit()

                # ✅ CHANGE 5: Use ADMIN paths in background
                img_path = os.path.join(ADMIN_IMAGES_DIR, img_filename)  # ← CHANGED
                work_img_path = os.path.join(ADMIN_IMAGES_DIR, f"{project_id}_{pair_index}_work.jpg")  # ← CHANGED
                npz_path = os.path.join(ADMIN_FEATURES_DIR, f"{project_id}_{pair_index}.npz")  # ← CHANGED

                # See the user-path comment above process_single_pair_bg's equivalent call —
                # marker_meta is never passed here either: the uploaded pixels are already
                # the selected ROI, applying crop_* again double-crops.
                make_feature_working_jpeg(img_path, work_img_path, max_dim=ORB_MAX_DIM, jpeg_quality=92)
                extract_features_multi(work_img_path, npz_path, max_dim=ORB_MAX_DIM)
                
                # Clean up
                try:
                    if os.path.exists(work_img_path):
                        os.remove(work_img_path)
                except Exception:
                    pass
                
                # Update database
                with app.app_context():
                    pair = ProjectPair.query.filter_by(
                        project_id=project_id,
                        pair_index=pair_index
                    ).first()
                    if pair:
                        pair.is_processed = True
                        pair.processing_status = "completed"
                        pair.feature_extraction_status = "extracted"
                        pair.processing_error = None
                        db.session.commit()
                
                return True
                
            except Exception as e:
                print(f"[ADMIN BG ERROR] Failed pair {pair_index}: {e}")
                with app.app_context():
                    pair = ProjectPair.query.filter_by(
                        project_id=project_id,
                        pair_index=pair_index
                    ).first()
                    if pair:
                        pair.is_processed = False
                        pair.processing_status = "failed"
                        pair.feature_extraction_status = "failed"
                        pair.processing_error = str(e)
                        db.session.commit()
                return False
        
        def background_processing_admin(project_id, all_pairs_data):
            """Process all admin pairs in parallel"""
            with app.app_context():
                try:
                    print(f"[ADMIN BG] Processing {len(all_pairs_data)} pairs")
                    
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                        futures = []
                        for pair_data in all_pairs_data:
                            future = executor.submit(
                                process_single_pair_bg_admin,
                                project_id,
                                pair_data["pair_index"],
                                pair_data["image_filename"]
                            )
                            futures.append(future)
                        
                        results = [f.result() for f in futures]
                        successful = sum(results)
                        
                        print(f"[ADMIN BG DONE] {successful}/{len(all_pairs_data)} pairs processed")
                    
                    load_features.cache_clear()
                    
                except Exception as e:
                    print(f"[ADMIN BG FATAL ERROR] {e}")
        
        # Start thread
        thread = threading.Thread(
            target=background_processing_admin,
            args=(project.id, pairs_data),
            daemon=True
        )
        thread.start()
        
    except Exception as e:
        print(f"Admin background thread failed: {e}")
    
    print(f"[ADMIN UPLOAD] Project {project.id} created in {time.time() - t0:.2f}s with {len(pairs_data)} pairs")
    
    flash("Project created successfully!", "success")
    return redirect(url_for("admin_success_page", project_id=project.id))
# ============================================================
# ADMIN FILE SERVING ROUTES
# ============================================================
@app.route("/admin/image/<int:project_id>/<int:image_id>")
def serve_admin_image(project_id, image_id):
    """Serve images for ADMIN projects only"""
    project = Project.query.get(project_id)
    if not project or not project.owner_admin_id:
        abort(404)
    if not _project_is_available(project):
        return _project_unavailable_response()
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        abort(404)
    
    # ✅ ADD THIS CHECK
    file_path = os.path.join(ADMIN_IMAGES_DIR, pair.image_filename)
    if not os.path.exists(file_path):
        print(f"❌ Admin image not found: {file_path}")
        abort(404)
    
    response = send_from_directory(ADMIN_IMAGES_DIR, pair.image_filename)
    return _apply_short_public_cache(response)
@app.route("/admin/video/<int:project_id>/<int:image_id>")
def serve_admin_video(project_id, image_id):
    """Serve videos for ADMIN projects only"""
    project = Project.query.get(project_id)
    if not project or not project.owner_admin_id:
        abort(404)
    if not _project_is_available(project):
        return _project_unavailable_response()
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        abort(404)
    
    response = send_from_directory(ADMIN_VIDEOS_DIR, pair.video_filename)
    response.headers["Content-Disposition"] = "inline"
    return _apply_short_public_cache(response)

@app.route("/admin/qr/<filename>")
def serve_admin_qr(filename):
    """Serve QR codes for ADMIN projects only"""
    # Extract project ID from filename (format: project_123_admin.png)
    try:
        project_id = int(filename.split('_')[1])
    except:
        abort(404)
    
    project = Project.query.get(project_id)
    if not project or not project.owner_admin_id:
        abort(404)
    if not _project_is_available(project):
        return _project_unavailable_response()
    
    response = send_from_directory(ADMIN_QR_DIR, filename)
    return _apply_short_public_cache(response)

@app.route("/admin/success/<int:project_id>", methods=["GET"])
@admin_required
def admin_success_page(project_id):
    """Success page for admin project creation"""
    admin = current_admin()
    project = Project.query.get(project_id)
    
    if not project or project.owner_admin_id != admin.id:
        abort(404)
    
    # ✅ Calculate display number for the admin project
    previous_count = Project.query.filter(
        Project.owner_admin_id == admin.id,
        Project.created_at < project.created_at
    ).count()
    project.display_number = previous_count + 1
    
    pairs = ProjectPair.query.filter_by(project_id=project.id).order_by(ProjectPair.pair_index.asc()).all()
    
    return render_template(
        "user/success.html",
        project=project,
        pairs=pairs,
        user=admin,
        is_admin=True,
        qr_download_url=url_for("admin_download_project_qr", project_id=project.id),
        projects_url=url_for("admin_my_projects"),
        test_scanner_url=url_for("admin_scanner_test_entry", project_id=project.id)
    )

@app.route("/admin/projects/<int:project_id>/qr")
@admin_required
def admin_download_project_qr(project_id):
    """Download QR code for admin project"""
    admin = current_admin()
    project = Project.query.get(project_id)
    
    if not project or project.owner_admin_id != admin.id:
        abort(404)
    
    if not project.qr_code_filename:
        abort(404)
    
    # ✅ FIX: Use ADMIN_QR_DIR instead of QR_DIR
    return send_from_directory(
        ADMIN_QR_DIR,  # ← CHANGE THIS (was QR_DIR)
        project.qr_code_filename,
        as_attachment=True,
        download_name=_build_qr_download_filename(project)
    )

@app.route("/admin/projects/delete/<int:project_id>", methods=["POST"])
@admin_required
def admin_delete_own_project(project_id):
    """Admin delete their own project"""
    admin = current_admin()
    project = Project.query.get(project_id)
    
    if not project or project.owner_admin_id != admin.id:
        abort(404)
    
    _delete_project_files_and_rows(project)
    db.session.commit()
    
    flash("Project deleted successfully.", "success")
    return redirect(url_for("admin_projects"))

# --------------------------------------------------------------------------------------------
# Error Handlers for JSON Responses
# --------------------------------------------------------------------------------------------
@app.errorhandler(404)
@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_error(error):
    """Ensure all errors return JSON for API endpoints.

    Detailed exception info (message, traceback) is logged server-side
    only via app.logger.exception(); clients never receive str(error),
    stack traces, SQL errors, or filesystem paths.
    """
    error_code = getattr(error, 'code', 500) or 500
    app.logger.exception(error)

    # Check if the request is for an API/detection endpoint
    if request.path.startswith('/detect') or request.path.startswith('/api'):
        generic_reason = "Not found" if error_code == 404 else "Server error"
        return jsonify({
            "detected": False,
            "reason": generic_reason,
            "error": True,
            "path": request.path,
            "method": request.method
        }), error_code

    if error_code == 404:
        return "<h1>404 Not Found</h1><p>The page you requested could not be found.</p>", 404

    # For regular routes, return a generic HTTP response - never the raw exception
    return "<h1>Something went wrong</h1><p>An unexpected error occurred. Please try again later.</p>", error_code
# --------------------------------------------------------------------------------------------
# SEO: sitemap.xml and robots.txt
# --------------------------------------------------------------------------------------------
@app.route("/sitemap.xml")
def sitemap():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://myscanstory.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
  <url><loc>https://myscanstory.com/pricing</loc><changefreq>monthly</changefreq><priority>0.9</priority></url>
  <url><loc>https://myscanstory.com/blog</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>
  <url><loc>https://myscanstory.com/contact</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>
  <url><loc>https://myscanstory.com/register</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
  <url><loc>https://myscanstory.com/terms</loc><changefreq>yearly</changefreq><priority>0.4</priority></url>
  <url><loc>https://myscanstory.com/privacy</loc><changefreq>yearly</changefreq><priority>0.4</priority></url>

  <url><loc>https://myscanstory.com/blog/augmented-reality-transforming-marketing-customer-engagement</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://myscanstory.com/blog/interactive-qr-codes-future-digital-brand-experiences</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://myscanstory.com/blog/smart-packaging-ar-product-communication</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://myscanstory.com/blog/ar-revolutionizing-education-training-knowledge-sharing</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://myscanstory.com/blog/ai-ar-qr-codes-future-interactive-experiences</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>
  <url><loc>https://myscanstory.com/blog/myscanstory-seo-aeo-geo-strategy</loc><changefreq>monthly</changefreq><priority>0.7</priority></url>

  
</urlset>"""
    return app.response_class(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    content = "User-agent: *\nAllow: /\n\nSitemap: https://myscanstory.com/sitemap.xml\n"
    return app.response_class(content, mimetype="text/plain")


from flask import send_file

@app.route('/yandex_2bd289a4ca147833.html')
def yandex_verification():
    return send_file('yandex_2bd289a4ca147833.html')

@app.route("/faqs")
@app.route("/faqs/")
def faqs_page():
    return render_template("user/landing.html", open_faqs=True)

# --------------------------------------------------------------------------------------------
# Main Application Entry Point
# --------------------------------------------------------------------------------------------
if __name__ == "__main__":
    # Create application context and bootstrap database
    with app.app_context():
        # Create all tables first
        db.create_all()
        ensure_marker_schema()
        
        # Then populate with default data
        bootstrap_database()
    
    # Run the app.
    # NOTE: this is the Werkzeug development server. Production deployments
    # must run behind a real WSGI server (gunicorn, waitress, etc.) - never
    # via `python app.py`. debug/use_reloader only activate when FLASK_DEBUG=1
    # is explicitly set (and are always off when SCANSTORY_TESTING=1).
    app.run(host="0.0.0.0", port=5000, debug=FLASK_DEBUG_ENABLED, use_reloader=FLASK_DEBUG_ENABLED)
