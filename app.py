import os
import sys
import time
import calendar
import shutil
import mimetypes
import threading
import json
import uuid
import razorpay
from functools import lru_cache, wraps
from datetime import datetime as dt, timedelta
from flask import (
    Flask, request, redirect, url_for, session, make_response,
    jsonify, flash, send_from_directory, render_template, abort, has_request_context
)
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.datastructures import FileStorage
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
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import aliased

from core.config import (
    database_backend_name as _database_backend_name,
    normalize_database_url as _normalize_database_url,
    env_flag as _env_flag,
    runtime_production_mode_flag_active as _runtime_production_mode_flag_active,
    smtp_port as _smtp_port,
    smtp_security_mode as _smtp_security_mode,
    smtp_timeout_seconds as _smtp_timeout_seconds,
)

# ✅ Import models
from models import (
    db, User, Admin, SubscriptionPlan, TrialDetails, OTPCode,
    Project, ProjectPair, PaymentOrder, ScanLog, SystemConfig,
    UserLoginActivity, AdminActivity, CapacityConfig, PaymentReservation,
    RazorpayWebhookEvent, ProcessingJob, UploadSession, UPLOAD_SESSION_PURPOSES, get_utc_now,
    ScanEvent, SCAN_EVENT_TYPES, UserConsentEvidence, AddonCatalog,
    AddonPurchase, EntitlementTransaction, PROJECT_EXPERIENCE_TYPES,
    PROJECT_PLAYBACK_MODES, ACCOUNT_TYPE_BUSINESS_VENDOR,
    ACCOUNT_TYPE_INDIVIDUAL,
    PROJECT_ACTIVE_TRANSFER_STATUSES, PROJECT_ACTIVE_CLAIM_STATUSES,
    ProjectOwnershipTransfer, ProjectOwnershipClaim, ProjectServiceCoverage,
    ContentReport, CONTENT_REPORT_REASONS, CONTENT_REPORT_STATUSES,
    CONTENT_REPORT_ACTIONS, PaymentRefund, REFUND_STATUSES,
    REFUND_RECONCILIATION_STATUSES, MediaObject,
    PLAN_FAMILIES, PLAN_LIFECYCLE_STATUSES, PLAN_PURCHASABLE_STATUSES,
    USER_ACCOUNT_TYPES,
)
import storage_accounting as _storage
from upload_validation import UploadValidationError, validate_image, validate_video, _safe_remove
import entitlements as _ent
from entitlements import (
    get_effective_entitlements,
    image_limits,
    is_downgrade,
    video_limits,
)
from rate_limit import identity_digest, limiter as request_limiter
from processing_queue import (
    QueueUnavailable,
    active_project_job,
    enqueue_project_pair_processing,
    queue_config_summary,
    queue_mode,
    queue_required,
    queue_worker_state,
    processing_job_status_payload,
    redis_ready_check,
    retry_failed_job,
    safe_error_summary,
)
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
# ponytail: logging.getLogger("app") is a process-wide singleton keyed by
# name, so it survives this module being re-imported. migrations/env.py's
# fileConfig(...) call (default disable_existing_loggers=True) silently
# flips .disabled=True on any already-registered logger it doesn't list
# (alembic.ini only lists root/sqlalchemy/alembic/flask_migrate) whenever a
# migration runs in the same process after app.py has been imported once -
# e.g. the test suite. Re-assert enabled here, the one place every app.py
# import already passes through, rather than chase every migration call site.
app.logger.disabled = False

# CSRF protection is enabled globally (see P0B). Narrow, justified exemptions
# are applied per-route below via @csrf.exempt - see the route inventory in
# the P0B report for why each exemption exists.
app.config['WTF_CSRF_ENABLED'] = True
app.config['WTF_CSRF_CHECK_DEFAULT'] = True
app.config['WTF_CSRF_HEADERS'] = ["X-CSRFToken", "X-CSRF-Token"]


def _runtime_environment_declared():
    """True when the deployment has explicitly stated which environment it is.

    Any non-blank value counts (development, staging, production, ...); the
    point is that the operator made a statement, not which statement.
    """
    for key in ("SCANSTORY_PRODUCTION", "APP_ENV", "ENV", "FLASK_ENV"):
        if (os.environ.get(key) or "").strip():
            return True
    return False


def _required_razorpay_config_missing():
    """Names of Razorpay settings required for production payment paths.

    Values are deliberately never returned or logged here. The API key pair is
    required for browser-created orders/refunds; the webhook secret is required
    for provider-to-server reconciliation.
    """
    return [
        key for key in (
            "RAZORPAY_KEY_ID",
            "RAZORPAY_KEY_SECRET",
            "RAZORPAY_WEBHOOK_SECRET",
        )
        if not (os.environ.get(key) or "").strip()
    ]


def _production_security_config_missing():
    """Production-only security/deployment config gaps, by variable name only."""
    if not _runtime_production_mode_flag_active():
        return []
    missing = _required_razorpay_config_missing()
    if not _env_flag("SECURITY_CSP_ENABLED", default=True):
        missing.append("SECURITY_CSP_ENABLED=1")
    if not _env_flag("SECURITY_CSP_ENFORCE", default=True):
        missing.append("SECURITY_CSP_ENFORCE=1")
    return missing


def _production_security_readiness_checks():
    """Safe /ready labels for production-only config gates.

    Startup validation normally prevents these from being bad in production;
    readiness still reports them generically for reloads/tests where the env can
    change after import.
    """
    if not _runtime_production_mode_flag_active():
        return {}
    missing = _production_security_config_missing()
    payment_missing = _required_razorpay_config_missing()
    return {
        "configuration": "unavailable" if missing else "ok",
        "payments": "unavailable" if payment_missing else "ok",
        "csp": "unavailable" if (
            not _env_flag("SECURITY_CSP_ENABLED", default=True)
            or not _env_flag("SECURITY_CSP_ENFORCE", default=True)
        ) else "ok",
    }


def _with_production_security_readiness(checks):
    checks.update(_production_security_readiness_checks())
    return checks


def _validate_required_runtime_config():
    """Fail fast on missing required runtime-security configuration.

    Centralized so future required settings can be added here. Error text names
    missing variables only; it never includes secret values.
    """
    missing = []
    if not os.environ.get("FLASK_SECRET_KEY"):
        missing.append("FLASK_SECRET_KEY")
    # Normalized BEFORE the backend check so the check sees the same URL the
    # engine will actually be built from, and so an explicitly requested
    # unsupported driver (e.g. postgresql+psycopg2://) fails startup here with a
    # named reason rather than at first connect (P0-3).
    database_url = _normalize_database_url(os.environ.get("DATABASE_URL"))
    if not SCANSTORY_TESTING:
        if not database_url:
            missing.append("DATABASE_URL")
        elif _database_backend_name(database_url) != "postgresql":
            missing.append("DATABASE_URL=postgresql")
    if _runtime_production_mode_flag_active():
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM"):
            if not os.environ.get(key):
                missing.append(key)
        try:
            effective_queue_mode = queue_mode()
        except QueueUnavailable as exc:
            raise RuntimeError("SCANSTORY_QUEUE_MODE must be rq in production.") from exc
        if effective_queue_mode != "rq":
            missing.append("SCANSTORY_QUEUE_MODE=rq")
        if not os.environ.get("REDIS_URL"):
            missing.append("REDIS_URL")
        if not _env_flag("SESSION_COOKIE_SECURE", default=False):
            missing.append("SESSION_COOKIE_SECURE=true")
        if os.environ.get("SCANSTORY_DEV_TESTING") == "1":
                missing.append("SCANSTORY_DEV_TESTING=0")
        # SCANSTORY_TESTING=1 on a production host permits SQLite (see the
        # database branch above) and forces queue mode 'fake', i.e. total silent
        # degradation. It belongs in the prohibition list beside
        # SCANSTORY_DEV_TESTING and was simply missing (P0-6 / ANM-52).
        if SCANSTORY_TESTING:
            missing.append("SCANSTORY_TESTING=0")
        missing.extend(_production_security_config_missing())
    elif not SCANSTORY_TESTING and not _runtime_environment_declared():
        # P0-6: production was detected only by an opt-IN flag, so a deploy that
        # set none of SCANSTORY_PRODUCTION / APP_ENV / ENV / FLASK_ENV booted
        # happily into queue mode 'fake' - jobs created, nothing ever run, and
        # /ready still 200. Refuse to boot ambiguously instead: a non-testing
        # runtime must state which environment it is. This cannot be bypassed by
        # omission, which was the whole failure mode.
        raise RuntimeError(
            "Runtime environment is not declared. Set one of SCANSTORY_PRODUCTION, "
            "APP_ENV, ENV or FLASK_ENV (e.g. APP_ENV=production or "
            "FLASK_ENV=development) before starting the app (see .env.example)."
        )
    _smtp_timeout_seconds()
    if os.environ.get("SMTP_PORT"):
        _smtp_port()
    _smtp_security_mode()
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
database_uri = _normalize_database_url(
    os.environ.get("TEST_DATABASE_URL") if SCANSTORY_TESTING else os.environ.get("DATABASE_URL", "")
)
engine_options = {}
database_backend = _database_backend_name(database_uri) if database_uri else ""
if database_uri and database_backend != "sqlite":
    connect_args = {
        'connect_timeout': 10,
    }

    # utf8mb4 is a MySQL-specific connection option.
    # PostgreSQL uses UTF-8 natively and does not accept this option.
    if database_uri.startswith(("mysql://", "mysql+")):
        connect_args['charset'] = 'utf8mb4'

    engine_options = {
        'pool_size': 10,
        'max_overflow': 20,
        'pool_recycle': 3600,
        'pool_pre_ping': True,
        'pool_timeout': 30,
        'connect_args': connect_args,
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
    "scanner_fallback": (60, 60),
    "scanner_fallback_event": (60, 60),
    "scanner_opencv_telemetry": (30, 60),
    "upload": (8, 3600),
    "login_ip": (80, 900),
    "login_identity": (15, 900),
    "register_ip": (30, 3600),
    "forgot_password_ip": (30, 3600),
    "resend_otp_ip": (20, 3600),
    "content_report": (5, 3600),
    # P0-8. /admin/login and /admin/forgot-password had NO request-layer limit
    # at all: admin login was protected only by a per-email DB lockout (useless
    # against distributed spray across many admin emails) and admin
    # forgot-password was an unlimited OTP-mail trigger that bypassed the
    # _resend_otp throttles entirely.
    #
    # Two buckets per route, deliberately: a per-IP bucket stops one host
    # spraying many identities, and a tighter identity+IP bucket carries the
    # real per-account limit. Keying the tight bucket on identity+IP rather than
    # identity alone means one abusive client cannot deny an entire NAT'd
    # network, and cannot lock a known admin out from elsewhere.
    "admin_login_ip": (20, 900),
    "admin_login_identity": (10, 900),
    "admin_forgot_password_ip": (10, 3600),
    "admin_forgot_password_identity": (3, 3600),
    # V1.1 Wave 4. Filing an ownership review request names another account's
    # project, so it is the one ownership mutation worth a bucket; the rest are
    # already scoped to a row the caller is a party to.
    "ownership_claim": (10, 3600),
    # V1.1 P1-10. The claim-preflight lookup answers "can I file a claim for the
    # project on this QR?" and therefore has to be cheap enough for a normal
    # claimant and too slow to sweep a range of ids with.
    "ownership_claim_lookup": (30, 3600),
}


def _rate_limit_key(scope, *parts):
    clean = [scope, _client_ip()]
    clean.extend(str(part or "-")[:120] for part in parts)
    return ":".join(clean)


def _rate_limited_html(template, retry_after, message="Too many attempts. Please try again later."):
    flash(message, "error")
    response = make_response(render_template(template), 429)
    response.headers["Retry-After"] = str(retry_after)
    return response


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


def _apply_short_private_cache(response):
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


def _log_scanner_latency(event, start_time, stage_timings=None, **fields):
    """stage_timings (Wave 7): optional dict of per-substage seconds (e.g. "read", "prep",
    "detect", "quick_score", "match", "homography"), the same breakdown this file has always
    computed and printed to stdout (see the `print(f"⏱ ...")` lines in detect_init/detect_track)
    but never folded into this structured log record — see
    docs/development/wave-7-detection-overlay-audit.md §3/§12. Purely additive: does not
    change duration_ms/outcome/stage or any accept/reject decision. Non-numeric/invalid entries
    are silently dropped rather than raising, since this is best-effort observability, not a
    correctness path."""
    safe = {
        "event": event,
        "duration_ms": round((time.time() - start_time) * 1000, 2),
    }
    for key, value in fields.items():
        if key in {"project_id", "outcome", "stage", "pair_id", "scan_session_id"}:
            safe[key] = value
    if stage_timings:
        for key, value in stage_timings.items():
            try:
                safe[f"stage_{key}_ms"] = round(float(value) * 1000, 2)
            except (TypeError, ValueError):
                continue
    app.logger.info("scanner_latency", extra={"scanner_latency": safe})


_TIMING_MAX_MS = 24 * 60 * 60 * 1000


def _elapsed_ms(start_time):
    try:
        return round(max(0.0, min((time.perf_counter() - start_time) * 1000, _TIMING_MAX_MS)), 2)
    except Exception:
        return 0.0


def _safe_timing_value(value):
    try:
        return round(max(0.0, min(float(value), _TIMING_MAX_MS)), 2)
    except (TypeError, ValueError):
        return 0.0


def _log_upload_timing(event, **fields):
    allowed = {
        "upload_session_id", "owner_type", "pair_count", "total_bytes", "image_bytes", "video_bytes",
        "chunk_size", "claimed_offset", "resulting_offset", "request_duration_ms",
        "server_write_duration_ms", "duplicate_chunk", "offset_mismatch", "checksum_duration_ms",
        "validation_duration_ms", "project_create_duration_ms", "qr_duration_ms", "enqueue_duration_ms",
        "finalize_duration_ms", "recovered_existing_completion", "project_id", "status", "safe_error_code",
        # Multi-content-set telemetry (Phase 2). Which set of how many, never
        # file contents, auth headers, tokens or connection strings.
        "set_index", "set_count",
    }
    safe = {"event": event}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key.endswith("_ms"):
            safe[key] = _safe_timing_value(value)
        elif key in {"upload_session_id", "pair_count", "total_bytes", "image_bytes", "video_bytes",
                     "chunk_size", "claimed_offset", "resulting_offset", "project_id",
                     "set_index", "set_count"}:
            safe[key] = int(value) if value is not None else None
        elif key in {"duplicate_chunk", "offset_mismatch", "recovered_existing_completion"}:
            safe[key] = bool(value)
        else:
            safe[key] = value
    app.logger.info("upload_timing", extra={"upload_timing": safe})


def _log_processing_timing(event, **fields):
    allowed = {
        "job_id", "project_id", "job_type", "queue_wait_duration_ms", "processing_duration_ms",
        "pair_count", "attempt_count", "status", "safe_error_code", "pair_id", "pair_index",
        "pair_processing_duration_ms", "feature_generation_duration_ms",
        "image_standardization_duration_ms", "enqueue_duration_ms",
    }
    safe = {"event": event}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key.endswith("_ms"):
            safe[key] = _safe_timing_value(value)
        elif key in {"job_id", "project_id", "pair_count", "attempt_count", "pair_id", "pair_index"}:
            safe[key] = int(value) if value is not None else None
        else:
            safe[key] = value
    app.logger.info("processing_timing", extra={"processing_timing": safe})

@app.context_processor
def inject_recaptcha_key():
    return {
        "RECAPTCHA_SITE_KEY": RECAPTCHA_SITE_KEY
    }


def verify_recaptcha_v3(expected_action):
    # MISSING CONFIG IS A DEPLOYMENT FAULT, NOT A PASS (V1.1 P1-2).
    #
    # Unconfigured keys used to return True unconditionally, so a production
    # deployment that forgot RECAPTCHA_SECRET_KEY ran every protected form with
    # no verification at all and nothing said so. A production-flagged runtime
    # now fails CLOSED here; local dev and the test suite (which have no real
    # keys, by design) keep the documented bypass.
    #
    # A provider/network failure already fails closed in the except branch
    # below - that behaviour is deliberate and unchanged.
    if not RECAPTCHA_SITE_KEY or not RECAPTCHA_SECRET_KEY:
        if _runtime_production_mode_flag_active():
            # Names the missing setting, never a key value.
            app.logger.error(
                "recaptcha_not_configured_in_production action=%s missing=%s",
                expected_action,
                ",".join(
                    name for name, value in (
                        ("RECAPTCHA_SITE_KEY", RECAPTCHA_SITE_KEY),
                        ("RECAPTCHA_SECRET_KEY", RECAPTCHA_SECRET_KEY),
                    ) if not value
                ),
            )
            return False, "Security verification is unavailable. Please try again later."
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

# CSP rollout: non-production can still run report-only for browser debugging,
# but a production-flagged runtime must enforce the policy. The validator above
# rejects production attempts to disable CSP or force report-only mode.
#   SECURITY_CSP_ENABLED=0        -> send neither CSP header at all
#   SECURITY_CSP_ENABLED=1, ENFORCE=0 -> Content-Security-Policy-Report-Only
#   SECURITY_CSP_ENABLED=1, ENFORCE=1 -> Content-Security-Policy
# Defaults: development/test report-only, production enforcing.
CSP_ENABLED = _env_flag("SECURITY_CSP_ENABLED", default=True)
CSP_ENFORCE = _env_flag("SECURITY_CSP_ENFORCE", default=_runtime_production_mode_flag_active())

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


def _format_csp(directives):
    return "; ".join(f"{directive} {' '.join(sources)}" for directive, sources in directives.items())


def _scanner_csp_policy():
    """Scanner-only CSP exception for the current self-hosted OpenCV bundle.

    static/js/opencv.js is generated by Emscripten and uses dynamic code
    creation while initializing the WASM runtime. Keeping 'unsafe-eval' scoped
    to scanner pages avoids relaxing script execution for the rest of the app.
    """
    directives = {key: list(value) for key, value in _CSP_DIRECTIVES.items()}
    script_sources = directives["script-src"]
    if "'unsafe-eval'" not in script_sources:
        script_sources.insert(script_sources.index("'wasm-unsafe-eval'") + 1, "'unsafe-eval'")
    connect_sources = directives["connect-src"]
    if "data:" not in connect_sources:
        connect_sources.append("data:")
    return _format_csp(directives)


SCANNER_CONTENT_SECURITY_POLICY = _scanner_csp_policy()


def _response_csp_policy():
    if request.endpoint == "scanner":
        return SCANNER_CONTENT_SECURITY_POLICY
    return CONTENT_SECURITY_POLICY


def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Scanner needs its own camera; every other page gets no camera at all.
    response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"

    if CSP_ENABLED:
        # Never send both - enforcing mode wins when explicitly opted into,
        # otherwise the same policy is sent report-only.
        csp_policy = _response_csp_policy()
        if CSP_ENFORCE:
            response.headers["Content-Security-Policy"] = csp_policy
        else:
            response.headers["Content-Security-Policy-Report-Only"] = csp_policy

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
    response = jsonify({"status": "ok"})
    response.headers["Cache-Control"] = "no-store"
    return response, 200


def _readiness_checks():
    db.session.execute(text("SELECT 1"))
    checks = {"database": "ok"}
    try:
        mode = queue_mode()
    except QueueUnavailable:
        return _with_production_security_readiness({"database": "ok", "queue": "unavailable"})
    if mode == "rq":
        if not redis_ready_check():
            return _with_production_security_readiness({"database": "ok", "queue": "unavailable"})
        checks["queue"] = "ok"
        # P1-3: a reachable Redis with no worker attached is a queue that
        # accepts jobs and runs none - indistinguishable from health before this
        # check existed. /healthz deliberately does NOT do this (it stays a
        # process-liveness probe); worker awareness belongs on /ready only.
        worker_state, usable_workers = queue_worker_state()
        checks["workers"] = worker_state
        checks["usable_worker_count"] = usable_workers
    elif queue_required():
        # P0-6: fake/inline skip the Redis probe entirely, so readiness used to
        # report 200 precisely when no upload would ever be processed. In a
        # production runtime any non-rq mode is a not-ready condition, never a
        # reason to skip the check.
        return _with_production_security_readiness({"database": "ok", "queue": "unavailable"})
    else:
        checks["queue"] = mode
    return _with_production_security_readiness(checks)


@app.route("/ready", methods=["GET"])
def ready():
    try:
        checks = _readiness_checks()
        # Any component reporting "unavailable" is not-ready. Scanning the values
        # rather than naming one key means a component added later cannot be
        # silently ignored the way the worker check would have been.
        if "unavailable" in checks.values():
            response = jsonify({"status": "not_ready", "checks": checks})
            response.headers["Cache-Control"] = "no-store"
            return response, 503
        response = jsonify({"status": "ready", "checks": checks})
        response.headers["Cache-Control"] = "no-store"
        return response, 200
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning("readiness_check_failed", exc_info=True)
        response = jsonify({"status": "not_ready", "checks": {"database": "unavailable"}})
        response.headers["Cache-Control"] = "no-store"
        return response, 503


# --------------------------------------------------------------------------------------------
# Razorpay Configuration
# --------------------------------------------------------------------------------------------
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")

# Dedicated webhook secret - deliberately NOT RAZORPAY_KEY_SECRET. Razorpay
# issues a separate secret per configured webhook (dashboard > Webhooks),
# unrelated to the API key pair used to call razorpay_client.order.create().
# No fallback to the API secret: that would let anyone who somehow learned
# the API secret (a different trust boundary - server-to-Razorpay auth, not
# Razorpay-to-server auth) forge webhook deliveries. Missing/empty here means
# the webhook route fails closed (see razorpay_webhook() below) rather than
# silently skipping verification.
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

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
# Migration commands must be able to import app.py without creating
# tables or inserting startup/bootstrap data.
SCANSTORY_SKIP_STARTUP_BOOTSTRAP = _env_flag(
    "SCANSTORY_SKIP_STARTUP_BOOTSTRAP",
    default=not SCANSTORY_TESTING,
)

with app.app_context():
    if not SCANSTORY_SKIP_STARTUP_BOOTSTRAP:
        if not SCANSTORY_TESTING:
            raise RuntimeError(
                "Runtime db.create_all() bootstrap is disabled outside tests. "
                "Run Alembic migrations with `flask --app app db upgrade` and seed data explicitly."
            )
        db.create_all()
        ensure_marker_schema()
    
    # Create default trial plan
    if (not SCANSTORY_SKIP_STARTUP_BOOTSTRAP and
            SubscriptionPlan.query.filter_by(is_trial_plan=True).first() is None):
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
    if (not SCANSTORY_SKIP_STARTUP_BOOTSTRAP and
            SubscriptionPlan.query.filter_by(plan_name="Basic").first() is None):
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
    
    if (not SCANSTORY_SKIP_STARTUP_BOOTSTRAP and
            SubscriptionPlan.query.filter_by(plan_name="Pro").first() is None):
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
    if not SCANSTORY_SKIP_STARTUP_BOOTSTRAP:
        _maybe_create_bootstrap_admin()
    
    # Create default system config
    if (not SCANSTORY_SKIP_STARTUP_BOOTSTRAP and
            SystemConfig.query.count() == 0):
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
    
    if not SCANSTORY_SKIP_STARTUP_BOOTSTRAP:
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
    # request.remote_addr is already normalized by ProxyFix above. Do not
    # read X-Forwarded-For directly here; only the trusted WSGI proxy layer
    # may translate that header into remote_addr.
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
        "admin.reports.view",
        "admin.reports.manage",
        "admin.ownership.view",
        "admin.ownership.manage",
    },
    "superadmin": {
        "admin.dashboard.view",
        "admin.users.view",
        "admin.users.manage",
        "admin.projects.view",
        "admin.projects.suspend",
        "admin.payments.view",
        "admin.payments.refund",
        "admin.processing.view",
        "admin.reports.view",
        "admin.reports.manage",
        "admin.ownership.view",
        "admin.ownership.manage",
        "superadmin.admins.manage",
        "superadmin.plans.manage",
        "superadmin.addons.manage",
        "superadmin.settings.manage",
        "superadmin.capacity.manage",
        "superadmin.audit.view",
        "superadmin.operations.view",
        "superadmin.repair.execute",
    },
}
HIGH_IMPACT_PERMISSIONS = {
    "admin.payments.refund",
    "superadmin.admins.manage",
    "superadmin.plans.manage",
    "superadmin.addons.manage",
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
    # Body-only change (Task 7, V1 Agent 2): same 404 status for every caller
    # (scanner() page view, serve_video/serve_image/serve_qr and their admin
    # variants) - only the response body goes from a bare text string to a
    # styled page. No scanner/recognition logic is upstream of this helper.
    return (render_template("user/project_unavailable.html"), 404)


def is_business_vendor(user):
    return bool(user and (user.account_type or "").upper() == ACCOUNT_TYPE_BUSINESS_VENDOR)


# ---------------------------------------------------------------------------
# V1.1 Experience UX - user-facing label maps.
#
# The domain layer stores raw enum strings (BUSINESS_VENDOR, PENDING_CAPACITY,
# STANDALONE_PROJECT_RENEWAL, ...). None of them may ever reach an end user's
# screen. These maps are the single translation point; the templates only ever
# render a value looked up here, and tests assert the keys stay in lockstep
# with the model-level enum sets so a new backend state can never silently
# render as a bare code.
# ---------------------------------------------------------------------------
ACCOUNT_TYPE_LABELS = {
    ACCOUNT_TYPE_INDIVIDUAL: "Individual",
    ACCOUNT_TYPE_BUSINESS_VENDOR: "Business / Vendor",
}
PROJECT_TRANSFER_STATUS_LABELS = {
    "PENDING_ACCEPTANCE": "Waiting for recipient",
    "PENDING_CAPACITY": "Recipient needs project/storage capacity",
    "COMPLETED": "Ownership transferred",
    "CANCELLED": "Transfer cancelled",
    "EXPIRED": "Transfer expired",
    "DISPUTED": "Transfer under review",
}
PROJECT_CLAIM_STATUS_LABELS = {
    "OPEN": "Submitted - waiting for review",
    "VENDOR_NOTIFIED": "Current owner has been notified",
    "APPROVED_BY_VENDOR": "Current owner agreed - waiting to complete",
    "PENDING_ADMIN_REVIEW": "Waiting for the ScanStory team to review",
    "APPROVED_BY_ADMIN": "Approved - ownership handover started",
    "REJECTED": "Not approved",
    "CANCELLED": "Request cancelled",
    "EXPIRED": "Request expired",
    "TRANSFER_COMPLETED": "Ownership handed over",
}
# Display copy for project_coverage_state()'s four values. Labels only - the
# state itself is decided by the resolver, never by a template. "Suspended" is
# worded so it can never read as "expired": the fix for one is not the fix for
# the other.
PROJECT_COVERAGE_STATE_LABELS = {
    "active": "Coverage active",
    "expired": "Coverage expired",
    "none": "No coverage",
    "suspended": "Suspended by ScanStory",
}
PROJECT_COVERAGE_SOURCE_LABELS = {
    "OWNER_SUBSCRIPTION": "Owner's plan",
    "STANDALONE_PROJECT_RENEWAL": "ScanStory Coverage purchase",
    "TRANSFER_CARRY_OVER": "Carried over with this ScanStory",
    "ADMIN_GRANT": "Granted by the ScanStory team",
    "LEGACY_COMPATIBILITY": "Included with your original ScanStory",
}
# Ordered on purpose: this is also the on-screen order of the report reasons.
CONTENT_REPORT_REASON_LABELS = {
    "EXPLICIT_OR_INAPPROPRIATE": "Explicit or inappropriate content",
    "VIOLENCE_OR_DANGER": "Violence or dangerous content",
    "HATE_OR_HARASSMENT": "Hate or harassment",
    "SCAM_OR_MISLEADING": "Scam or misleading content",
    "COPYRIGHT_OR_IP": "Copyright or intellectual property",
    "PRIVACY": "Privacy concern",
    "SPAM": "Spam",
    "OTHER": "Other",
}
CONTENT_REPORT_STATUS_LABELS = {
    "OPEN": "Open",
    "UNDER_REVIEW": "Under review",
    "ACTION_TAKEN": "Action taken",
    "DISMISSED": "Dismissed",
}
CONTENT_REPORT_ACTION_LABELS = {
    "NONE": "No action needed",
    "PROJECT_SUSPENDED": "ScanStory suspended",
    "CREATOR_CONTACT_REQUIRED": "Creator contact required",
    "LEGAL_REVIEW_REQUIRED": "Legal review required",
    "OTHER": "Other",
}
# Enforced by the public report endpoint below AND rendered as the textarea's
# maxlength, so the form and the validator can never disagree. Lives here so
# the Jinja globals registration a few lines down can see it.
CONTENT_REPORT_DETAILS_MAX = 2000

# Refund display. Two INDEPENDENT axes, deliberately never merged: the provider
# can have refunded the money (REFUNDED) while the local entitlement
# reconciliation still needs a human (MANUAL_REVIEW_REQUIRED). Labelling that
# combination "Refund failed" would be a lie, so the admin surfaces render both
# lines side by side, always.
REFUND_STATUS_LABELS = {
    "REFUND_REQUESTED": "Refund requested",
    "REFUND_PROCESSING": "Refund processing",
    "REFUNDED": "Refunded",
    "REFUND_FAILED": "Refund failed",
}
REFUND_RECONCILIATION_LABELS = {
    "PENDING": "Entitlement update pending",
    "APPLIED": "Entitlements reconciled",
    "MANUAL_REVIEW_REQUIRED": "Manual reconciliation required",
    "FAILED": "Reconciliation needs attention",
}
# What a refund does to entitlements, in words, per entitlement type the
# refund reconciliation actually touches. Nothing here may imply deletion of a
# ScanStory, its media or its QR code - the backend never deletes any of those.
REFUND_ENTITLEMENT_EFFECT_NOTES = {
    "PROJECT_CAPACITY": (
        "Refunding this removes the purchased project slots from the account. "
        "Existing ScanStorys, media and QR codes are kept and keep working. If the "
        "account ends up above its available slots, new ScanStory creation and "
        "incoming transfers stay unavailable until usage is back within the "
        "available slots."
    ),
    "EXTRA_SCANS": (
        "Refunding this removes the purchased scan allowance. Existing ScanStorys, "
        "media and QR codes are kept and keep working."
    ),
    "PROJECT_SERVICE_COVERAGE": (
        "Refunding this revokes the purchased ScanStory Coverage period for the "
        "project. Existing ScanStorys, media and QR codes are kept; the project "
        "simply returns to whatever other coverage it has."
    ),
    "VALIDITY_EXTENSION": (
        "Validity extensions are not reversed automatically. The payment is "
        "refunded and the subscription expiry is left untouched for an admin to "
        "reconcile manually."
    ),
}
# Shown on screen AND used verbatim in the confirm() prompt, so the promise the
# admin reads and the promise the admin confirms cannot drift apart.
REFUND_CONFIRMATION_NOTICE = (
    "This is a FULL refund. Partial refunds are not supported in V1.1.\n"
    "Razorpay will process the refund.\n"
    "Entitlement and capacity effects may follow provider confirmation, so they may not be instant.\n"
    "ScanStorys, media and QR codes are never deleted automatically.\n"
    "Some subscription and validity refunds require manual entitlement reconciliation by an admin."
)


def account_type_label(user):
    return ACCOUNT_TYPE_LABELS.get(
        (getattr(user, "account_type", None) or ACCOUNT_TYPE_INDIVIDUAL).upper(), "Individual"
    )


# jinja_env.globals, not a context_processor: these are request-independent
# constants, and globals are also visible to callers that render a template
# directly through jinja_env.get_template(...).render() rather than through
# Flask's render_template() - which several existing tests do.
app.jinja_env.globals.update(
    ACCOUNT_TYPE_LABELS=ACCOUNT_TYPE_LABELS,
    PROJECT_TRANSFER_STATUS_LABELS=PROJECT_TRANSFER_STATUS_LABELS,
    PROJECT_CLAIM_STATUS_LABELS=PROJECT_CLAIM_STATUS_LABELS,
    PROJECT_COVERAGE_SOURCE_LABELS=PROJECT_COVERAGE_SOURCE_LABELS,
    PROJECT_COVERAGE_STATE_LABELS=PROJECT_COVERAGE_STATE_LABELS,
    CONTENT_REPORT_REASON_LABELS=CONTENT_REPORT_REASON_LABELS,
    CONTENT_REPORT_STATUS_LABELS=CONTENT_REPORT_STATUS_LABELS,
    CONTENT_REPORT_ACTION_LABELS=CONTENT_REPORT_ACTION_LABELS,
    CONTENT_REPORT_DETAILS_MAX=CONTENT_REPORT_DETAILS_MAX,
    REFUND_STATUS_LABELS=REFUND_STATUS_LABELS,
    REFUND_RECONCILIATION_LABELS=REFUND_RECONCILIATION_LABELS,
    REFUND_ENTITLEMENT_EFFECT_NOTES=REFUND_ENTITLEMENT_EFFECT_NOTES,
    REFUND_CONFIRMATION_NOTICE=REFUND_CONFIRMATION_NOTICE,
    account_type_label=account_type_label,
)


def project_ownership_context(project, viewer):
    """Everything the ownership/coverage panels render, resolved once.

    The central /ownership surface owns user mutations. Project pages use this
    context for truthful state and lightweight links/forms, while backend
    routes remain authoritative for every ownership transition.
    """
    if not project:
        return None
    creator_id = project_created_by_user_id(project)
    creator = User.query.get(creator_id) if creator_id else None
    owner_id = project_current_owner_user_id(project)
    owner = User.query.get(owner_id) if owner_id else None
    manager = User.query.get(project.manager_vendor_user_id) if project.manager_vendor_user_id else None
    beneficiary = User.query.get(project.beneficiary_user_id) if project.beneficiary_user_id else None
    transfer = (
        ProjectOwnershipTransfer.query.filter_by(project_id=project.id)
        .order_by(ProjectOwnershipTransfer.id.desc())
        .first()
    )
    claims = (
        ProjectOwnershipClaim.query.filter_by(project_id=project.id)
        .order_by(ProjectOwnershipClaim.id.desc())
        .limit(5)
        .all()
    )
    viewer_id = getattr(viewer, "id", None)
    viewer_active_claim = next(
        (
            claim for claim in claims
            if viewer_id and claim.claimant_user_id == viewer_id and claim.status in PROJECT_ACTIVE_CLAIM_STATUSES
        ),
        None,
    )
    return {
        "creator": creator,
        "owner": owner,
        "manager": manager,
        "beneficiary": beneficiary,
        "viewer_active_claim": viewer_active_claim,
        "viewer_is_creator": bool(viewer_id and creator and creator.id == viewer_id),
        "viewer_is_owner": bool(viewer_id and owner and owner.id == viewer_id),
        "viewer_is_manager": bool(viewer_id and manager and manager.id == viewer_id),
        "transfer": transfer,
        "transfer_is_active": bool(transfer and transfer.status in PROJECT_ACTIVE_TRANSFER_STATUSES),
        "claims": claims,
        "transfer_recipient": User.query.get(transfer.to_user_id) if transfer and transfer.to_user_id else None,
    }


def project_current_owner_user_id(project):
    return project.current_owner_user_id or project.owner_user_id if project else None


def project_created_by_user_id(project):
    return project.created_by_user_id or project.owner_user_id if project else None


def user_can_manage_project(user, project):
    if not user or not project:
        return False
    user_id = user.id
    if project_current_owner_user_id(project) == user_id:
        return True
    return bool(project.manager_vendor_user_id == user_id and is_business_vendor(user))


def user_can_transfer_project(user, project):
    return user_can_manage_project(user, project)


def project_user_access_filter(user_id):
    return or_(
        Project.current_owner_user_id == user_id,
        and_(Project.current_owner_user_id.is_(None), Project.owner_user_id == user_id),
        Project.manager_vendor_user_id == user_id,
    )


def _active_project_transfer(project_id):
    return ProjectOwnershipTransfer.query.filter(
        ProjectOwnershipTransfer.project_id == project_id,
        ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES),
    ).first()


def set_project_current_owner(project, new_owner, retain_vendor_management=False, manager_vendor=None):
    if not project or not new_owner:
        raise ValueError("Project and new owner are required.")
    if manager_vendor is not None and not is_business_vendor(manager_vendor):
        raise ValueError("manager_vendor_user_id must reference a BUSINESS_VENDOR user.")
    if project.created_by_user_id is None:
        project.created_by_user_id = project_created_by_user_id(project)
    project.current_owner_user_id = new_owner.id
    project.owner_user_id = new_owner.id
    project.manager_vendor_user_id = manager_vendor.id if retain_vendor_management and manager_vendor else None


def initiate_project_ownership_transfer(project, initiated_by_user, recipient_user, retain_vendor_management=False, reason=None, expires_at=None):
    if not user_can_transfer_project(initiated_by_user, project):
        raise PermissionError("Only the current owner or explicit vendor manager can initiate transfer.")
    current_owner_id = project_current_owner_user_id(project)
    if not recipient_user or not current_owner_id:
        raise ValueError("A valid recipient and current owner are required.")
    if recipient_user.id == current_owner_id:
        raise ValueError("Cannot transfer a project to the current owner.")
    if _active_project_transfer(project.id):
        raise ValueError("An active transfer already exists for this project.")

    transfer = ProjectOwnershipTransfer(
        project_id=project.id,
        initiated_by_user_id=initiated_by_user.id,
        from_owner_user_id=current_owner_id,
        to_user_id=recipient_user.id,
        retain_vendor_management=bool(retain_vendor_management),
        status="PENDING_ACCEPTANCE",
        reason=reason,
        # P1-4: every pending transfer now carries a deadline. The column and the
        # expiry check in accept_project_ownership_transfer() already existed;
        # nothing ever populated this, so EXPIRED was unreachable in production.
        # An explicit expires_at from a caller still wins.
        expires_at=expires_at or (get_utc_now() + timedelta(days=ownership_transfer_expiry_days())),
    )
    db.session.add(transfer)
    db.session.flush()
    _record_ownership_event(
        transfer,
        "transfer_initiated",
        actor_user=initiated_by_user,
        reason=reason,
        project_id=project.id,
        from_owner_user_id=current_owner_id,
        to_user_id=recipient_user.id,
        retain_vendor_management=bool(retain_vendor_management),
    )
    db.session.flush()
    return transfer


# ---------------------------------------------------------------------------
# V1.1 Wave 4: governed ownership transitions.
#
# CONCURRENCY. Every state change below goes through one conditional UPDATE
# gated on the row's CURRENT status - the same primitive shape as Wave 1's
# _atomic_increment_user_counter and Wave 3's reserve_account_storage. A second
# concurrent (or duplicated) request matches zero rows and no-ops, so ownership
# can never move twice, a recipient slot can never be consumed twice, and
# MediaObject rows can never be re-owned twice.
#
# AUDIT. metadata_json carries the append-only transition trail. Actor ids,
# states and the capacity numbers that were actually checked - never secrets,
# never filesystem paths.
# ---------------------------------------------------------------------------
_TRANSFER_RESUMABLE_STATUSES = ("PENDING_ACCEPTANCE", "PENDING_CAPACITY")

# V1.1 P1-4 / P1-5: the two ownership deadlines, in one place.
#
# EXPIRED and the vendor-response escalation both already existed in the
# vocabulary (PROJECT_TRANSFER_STATUSES / ProjectOwnershipClaim
# .response_deadline_at) but neither column was ever populated, so EXPIRED was
# unreachable and "the vendor never answered" had no deterministic resolution.
# These are the durations, named and env-overridable, not magic numbers inline.
def _ownership_deadline_days(name, default_days):
    try:
        return max(1, int(os.environ.get(name, str(default_days))))
    except (TypeError, ValueError):
        return default_days


def ownership_transfer_expiry_days():
    """How long a pending transfer stays acceptable."""
    return _ownership_deadline_days("OWNERSHIP_TRANSFER_EXPIRY_DAYS", 14)


def ownership_claim_response_days():
    """How long a managing vendor has to answer a claim before admin may act."""
    return _ownership_deadline_days("OWNERSHIP_CLAIM_RESPONSE_DAYS", 7)


def _ownership_metadata(record):
    try:
        data = json.loads(record.metadata_json) if record.metadata_json else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _record_ownership_event(record, action, actor_user=None, admin=None, reason=None,
                            capacity_block=None, **detail):
    data = _ownership_metadata(record)
    trail = data.get("audit")
    if not isinstance(trail, list):
        trail = []
    entry = {
        "action": action,
        "at": get_utc_now().isoformat(),
        "status": record.status,
        "actor_user_id": getattr(actor_user, "id", None),
        "actor_admin_id": getattr(admin, "id", None),
    }
    if reason:
        entry["reason"] = str(reason)[:500]
    entry.update(detail)
    trail.append(entry)
    # ponytail: bounded trail. 40 transitions is far more than any real
    # ownership dispute produces; swap for a dedicated table if that stops
    # being true.
    data["audit"] = trail[-40:]
    if capacity_block is not None:
        data["capacity_block"] = capacity_block
    record.metadata_json = json.dumps(data)
    return record


def _transition_ownership_row(model, record, from_statuses, to_status, **columns):
    """Conditional UPDATE gated on current status. True only for the winner.

    A duplicate/concurrent caller matches zero rows and gets False, which every
    caller below turns into an idempotent no-op rather than a second effect.
    """
    values = {model.status: to_status}
    values.update({getattr(model, key): value for key, value in columns.items()})
    updated = model.query.filter(
        model.id == record.id,
        model.status.in_(tuple(from_statuses)),
    ).update(values, synchronize_session=False)
    if updated != 1:
        return False
    record.status = to_status
    for key, value in columns.items():
        setattr(record, key, value)
    return True


def _transition_transfer(transfer, from_statuses, to_status, **columns):
    return _transition_ownership_row(ProjectOwnershipTransfer, transfer, from_statuses, to_status, **columns)


def _transition_claim(claim, from_statuses, to_status, **columns):
    return _transition_ownership_row(ProjectOwnershipClaim, claim, from_statuses, to_status, **columns)


def transfer_capacity_snapshot(transfer):
    """The recorded reason a transfer is parked, without re-deriving it."""
    return _ownership_metadata(transfer).get("capacity_block") if transfer else None


def ownership_audit_trail(record):
    return _ownership_metadata(record).get("audit") or [] if record else []


def evaluate_transfer_capacity(project, recipient):
    """Both capacity dimensions for one transfer-in, as plain numbers.

    Project capacity reuses the Wave 1 effective-limit helpers; storage reuses
    Wave 3's evaluate_project_storage_transfer() unmodified. Read-only - it
    reserves nothing, so it is safe to call from a GET.
    """
    storage_ok, project_bytes = evaluate_project_storage_transfer(project, recipient)
    used, allowance = account_storage_state(recipient)
    project_limit = effective_project_limit(recipient)
    projects_used = int(getattr(recipient, "projects_used", 0) or 0)
    slot_ok = has_dev_test_entitlement(recipient) or not _limit_reached(project_limit, projects_used)
    return {
        "storage_ok": bool(storage_ok),
        "project_slot_ok": bool(slot_ok),
        "project_bytes": int(project_bytes or 0),
        "recipient_storage_used_bytes": int(used or 0),
        "recipient_storage_allowance_bytes": allowance,
        "recipient_project_limit": project_limit,
        "recipient_projects_used": projects_used,
        "checked_at": get_utc_now().isoformat(),
    }


def _park_transfer_pending_capacity(transfer, snapshot, actor_user=None, admin=None):
    """Non-destructive stall. Nothing has moved and nothing will be unwound.

    PENDING_CAPACITY already meant "the recipient cannot absorb this yet" for
    project slots; storage is a second capacity DIMENSION, not a second state,
    so the snapshot records WHICH dimension failed and at what values. Retrying
    re-runs accept_project_ownership_transfer() on the SAME row.
    """
    _transition_transfer(transfer, _TRANSFER_RESUMABLE_STATUSES, "PENDING_CAPACITY")
    _record_ownership_event(
        transfer,
        "transfer_pending_capacity",
        actor_user=actor_user,
        admin=admin,
        capacity_block=snapshot,
        **{k: snapshot[k] for k in ("storage_ok", "project_slot_ok", "project_bytes")},
    )
    db.session.flush()
    return transfer


def _mark_claims_transfer_completed(transfer):
    for claim in ProjectOwnershipClaim.query.filter(
        ProjectOwnershipClaim.transfer_id == transfer.id,
        ProjectOwnershipClaim.status != "TRANSFER_COMPLETED",
    ).all():
        if _transition_claim(claim, (claim.status,), "TRANSFER_COMPLETED"):
            _record_ownership_event(claim, "claim_transfer_completed", transfer_id=transfer.id)


def expire_transfer_if_due(transfer, actor_user=None, admin=None, now=None):
    """True when this transfer is past its deadline (and now recorded EXPIRED).

    Called before any mutating action on a transfer, and by the
    `expire-ownership-transfers` CLI. Idempotent by construction: the conditional
    UPDATE only matches a still-pending row, so a second run neither
    re-transitions nor errors, and an already-EXPIRED transfer still answers
    True. Ownership is NEVER touched here - expiry closes the handover offer and
    nothing else. A linked claim is deliberately left alone: an expired transfer
    and an open claim are separate lifecycles, and cancelling someone's claim as
    a side effect of a missed deadline is not a decision this function may take.
    """
    if not transfer or not transfer.expires_at:
        return False
    if (now or get_utc_now()) <= transfer.expires_at:
        return False
    if transfer.status == "EXPIRED":
        return True
    if transfer.status not in _TRANSFER_RESUMABLE_STATUSES:
        # Already COMPLETED/CANCELLED/DISPUTED - a deadline cannot reopen or
        # override a state a human or a completed handover already reached.
        return False
    if _transition_transfer(transfer, _TRANSFER_RESUMABLE_STATUSES, "EXPIRED"):
        _record_ownership_event(transfer, "transfer_expired", actor_user=actor_user, admin=admin)
        db.session.flush()
    return True


def expired_pending_transfer_query(now=None):
    """Pending transfers whose deadline has passed."""
    return ProjectOwnershipTransfer.query.filter(
        ProjectOwnershipTransfer.status.in_(_TRANSFER_RESUMABLE_STATUSES),
        ProjectOwnershipTransfer.expires_at.isnot(None),
        ProjectOwnershipTransfer.expires_at < (now or get_utc_now()),
    ).order_by(ProjectOwnershipTransfer.id.asc())


def accept_project_ownership_transfer(transfer, acting_user=None, completed_by_admin=None):
    """Complete a transfer if - and only if - BOTH capacity dimensions allow it.

    Idempotent: an already-COMPLETED transfer returns unchanged, and two
    concurrent acceptances leave exactly one ownership transition behind.
    """
    if transfer.status == "COMPLETED":
        return transfer
    if transfer.status not in _TRANSFER_RESUMABLE_STATUSES:
        raise ValueError("Transfer is not pending acceptance.")
    if acting_user is None and completed_by_admin is None:
        raise PermissionError("Transfer acceptance requires the recipient or an admin override.")
    if acting_user is not None and acting_user.id != transfer.to_user_id:
        raise PermissionError("Only the intended recipient can accept this transfer.")

    if expire_transfer_if_due(transfer, actor_user=acting_user, admin=completed_by_admin):
        raise ValueError("This transfer has expired.")

    project = Project.query.get(transfer.project_id)
    recipient = User.query.get(transfer.to_user_id)
    sender = User.query.get(transfer.from_owner_user_id)
    if not project or not recipient or not sender:
        raise ValueError("Transfer project, sender, or recipient no longer exists.")
    if project_current_owner_user_id(project) != transfer.from_owner_user_id:
        raise ValueError("Project owner changed after transfer initiation.")

    # STORAGE CAPACITY IS CHECKED FIRST (Wave 3), before the project slot is
    # reserved, so an insufficient-storage recipient needs no counter to be
    # unwound. Nothing is deleted, no accounting moves, the sender stays the
    # owner and keeps their slot until the transfer TRULY completes.
    snapshot = evaluate_transfer_capacity(project, recipient)
    if not snapshot["storage_ok"]:
        return _park_transfer_pending_capacity(transfer, snapshot, actor_user=acting_user, admin=completed_by_admin)

    if not _reserve_project_quota_atomic(recipient):
        snapshot["project_slot_ok"] = False
        return _park_transfer_pending_capacity(transfer, snapshot, actor_user=acting_user, admin=completed_by_admin)

    try:
        now = get_utc_now()
        # THE GATE. Everything below happens exactly once per transfer because
        # only one caller can move the row out of a resumable status.
        if not _transition_transfer(
            transfer,
            _TRANSFER_RESUMABLE_STATUSES,
            "COMPLETED",
            accepted_at=transfer.accepted_at or now,
            completed_at=now,
            completed_by_admin_id=completed_by_admin.id if completed_by_admin else None,
        ):
            # A concurrent acceptance already won. Hand back the slot this
            # attempt reserved and report the winner's state, untouched.
            _release_project_quota_atomic(recipient)
            db.session.expire(transfer)
            return transfer

        _release_project_quota_atomic(sender)
        manager_vendor = sender if transfer.retain_vendor_management and is_business_vendor(sender) else None
        set_project_current_owner(
            project,
            recipient,
            retain_vendor_management=bool(manager_vendor),
            manager_vendor=manager_vendor,
        )
        # SAME TRANSACTION as the ownership change: storage responsibility can
        # never end up split from the project it belongs to. Files are not
        # copied or moved - only the MediaObject rows change account.
        moved_bytes = _storage.move_project_storage_ownership(project.id, sender.id, recipient.id)
        _record_ownership_event(
            transfer,
            "transfer_completed",
            actor_user=acting_user,
            admin=completed_by_admin,
            project_id=project.id,
            from_owner_user_id=sender.id,
            to_user_id=recipient.id,
            moved_bytes=int(moved_bytes or 0),
            manager_vendor_user_id=project.manager_vendor_user_id,
        )
        _mark_claims_transfer_completed(transfer)
        db.session.flush()
        return transfer
    except Exception:
        db.session.rollback()
        raise


def reject_project_ownership_transfer(transfer, acting_user, reason=None):
    """Recipient declines. CANCELLED is the existing terminal state for
    "this handover will not happen"; the trail records that it was a decline
    rather than a withdrawal, so no new status code is invented."""
    if acting_user is None or acting_user.id != transfer.to_user_id:
        raise PermissionError("Only the intended recipient can decline this transfer.")
    if not _transition_transfer(transfer, _TRANSFER_RESUMABLE_STATUSES, "CANCELLED", cancelled_at=get_utc_now()):
        raise ValueError("This transfer is no longer pending.")
    _record_ownership_event(transfer, "transfer_rejected", actor_user=acting_user, reason=reason)
    db.session.flush()
    return transfer


def cancel_project_ownership_transfer(transfer, acting_user=None, admin=None, reason=None):
    """Sender/initiator withdraws, or an admin resolves a dispute by cancelling."""
    if admin is None:
        if acting_user is None or acting_user.id not in {transfer.from_owner_user_id, transfer.initiated_by_user_id}:
            raise PermissionError("Only the sender or an admin can cancel this transfer.")
    allowed = _TRANSFER_RESUMABLE_STATUSES + (("DISPUTED",) if admin is not None else ())
    if not _transition_transfer(transfer, allowed, "CANCELLED", cancelled_at=get_utc_now()):
        raise ValueError("This transfer is no longer cancellable.")
    _record_ownership_event(transfer, "transfer_cancelled", actor_user=acting_user, admin=admin, reason=reason)
    db.session.flush()
    return transfer


def mark_project_transfer_disputed(transfer, admin, reason=None):
    """Freeze a transfer for manual review. The CURRENT owner stays authoritative."""
    if admin is None:
        raise PermissionError("Only an admin can mark a transfer disputed.")
    if not _transition_transfer(transfer, _TRANSFER_RESUMABLE_STATUSES, "DISPUTED"):
        raise ValueError("Only a pending transfer can be disputed.")
    _record_ownership_event(transfer, "transfer_disputed", admin=admin, reason=reason)
    db.session.flush()
    return transfer


def release_project_transfer_dispute(transfer, admin, reason=None):
    """Return a disputed transfer to the normal pending flow. Never auto-resolves ownership."""
    if admin is None:
        raise PermissionError("Only an admin can resolve a dispute.")
    if not _transition_transfer(transfer, ("DISPUTED",), "PENDING_ACCEPTANCE"):
        raise ValueError("This transfer is not disputed.")
    _record_ownership_event(transfer, "transfer_dispute_released", admin=admin, reason=reason)
    db.session.flush()
    return transfer


def create_project_ownership_claim(project, claimant_user, evidence_summary=None, evidence_json=None):
    if not project or not claimant_user:
        raise ValueError("Project and claimant are required.")
    if project_current_owner_user_id(project) == claimant_user.id:
        raise ValueError("Current owner cannot claim their own project.")
    existing = ProjectOwnershipClaim.query.filter(
        ProjectOwnershipClaim.project_id == project.id,
        ProjectOwnershipClaim.claimant_user_id == claimant_user.id,
        ProjectOwnershipClaim.status.in_(PROJECT_ACTIVE_CLAIM_STATUSES),
    ).first()
    if existing:
        return existing
    claim = ProjectOwnershipClaim(
        project_id=project.id,
        claimant_user_id=claimant_user.id,
        current_owner_user_id=project_current_owner_user_id(project),
        status="OPEN",
        # P1-5: the deterministic escalation instant. Until it passes, a claim on
        # a VENDOR-MANAGED project belongs to the vendor to answer; after it,
        # admin review is unblocked so a silent vendor cannot park a claim
        # forever. Populated here because nothing ever populated it before.
        response_deadline_at=get_utc_now() + timedelta(days=ownership_claim_response_days()),
        evidence_summary=evidence_summary,
        evidence_json=json.dumps(evidence_json) if isinstance(evidence_json, (dict, list)) else evidence_json,
    )
    db.session.add(claim)
    db.session.flush()
    _record_ownership_event(claim, "claim_submitted", actor_user=claimant_user, project_id=project.id)
    db.session.flush()
    return claim


def user_can_respond_to_claim(user, claim):
    """Backend-enforced vendor scope: only this project's current owner or its
    explicit managing vendor, and never the claimant themselves."""
    if not user or not claim or claim.claimant_user_id == user.id:
        return False
    return user_can_manage_project(user, Project.query.get(claim.project_id))


def respond_to_project_ownership_claim(claim, acting_user, accept, response_note=None):
    """The current owner / managing vendor answers a claim.

    Accepting is CONSENT, not a transfer: it opens a normal governed transfer
    that the claimant must still accept and that still has to pass both
    capacity checks. Refusing never closes the claim unilaterally - it escalates
    to Admin review, because the counterparty is not the adjudicator.
    """
    if not claim:
        raise ValueError("Claim is required.")
    if not user_can_respond_to_claim(acting_user, claim):
        raise PermissionError("Only this project's current owner or managing vendor can respond to this claim.")
    if claim.status not in PROJECT_ACTIVE_CLAIM_STATUSES:
        raise ValueError("Claim is not active.")
    project = Project.query.get(claim.project_id)
    claimant = User.query.get(claim.claimant_user_id)
    if not project or not claimant:
        raise ValueError("Claim project or claimant no longer exists.")

    now = get_utc_now()
    target = "APPROVED_BY_VENDOR" if accept else "PENDING_ADMIN_REVIEW"
    if not _transition_claim(
        claim,
        PROJECT_ACTIVE_CLAIM_STATUSES,
        target,
        vendor_notified_at=claim.vendor_notified_at or now,
    ):
        return claim, None

    transfer = None
    if accept:
        transfer = initiate_project_ownership_transfer(
            project,
            initiated_by_user=acting_user,
            recipient_user=claimant,
            retain_vendor_management=False,
            reason="Current owner accepted an ownership review request.",
        )
        claim.transfer_id = transfer.id
    _record_ownership_event(
        claim,
        "claim_vendor_accepted" if accept else "claim_vendor_refused",
        actor_user=acting_user,
        reason=response_note,
        transfer_id=transfer.id if transfer else None,
    )
    db.session.flush()
    return claim, transfer


def cancel_project_ownership_claim(claim, acting_user, reason=None):
    if not claim or not acting_user or claim.claimant_user_id != acting_user.id:
        raise PermissionError("Only the claimant can withdraw this request.")
    if not _transition_claim(claim, PROJECT_ACTIVE_CLAIM_STATUSES, "CANCELLED"):
        raise ValueError("Claim is not active.")
    _record_ownership_event(claim, "claim_cancelled", actor_user=acting_user, reason=reason)
    db.session.flush()
    return claim


_CLAIM_ADMIN_REVIEWABLE_STATUSES = ("PENDING_ADMIN_REVIEW", "APPROVED_BY_VENDOR")


def claim_admin_review_block_reason(claim, now=None):
    """Why admin adjudication is premature for this claim, or None if it is not.

    THE GOVERNED ORDER (P1-5). Where a project has an explicit managing vendor,
    that vendor answers first: admin is the escalation, not the first responder,
    so an OPEN/VENDOR_NOTIFIED claim is not admin-reviewable until either the
    vendor has responded (which lands the claim in PENDING_ADMIN_REVIEW or
    APPROVED_BY_VENDOR) or the response deadline has passed.

    Where there is NO managing vendor, direct admin review of an OPEN claim is
    the correct governed path, not a bug - there is no vendor step that could
    ever be satisfied, and the current owner can still respond at any time.
    Nothing here transfers ownership either way.
    """
    if not claim:
        return None
    if claim.status in _CLAIM_ADMIN_REVIEWABLE_STATUSES:
        return None
    project = Project.query.get(claim.project_id)
    if not project or not project.manager_vendor_user_id:
        return None
    deadline = claim.response_deadline_at
    if deadline and (now or get_utc_now()) > deadline:
        return None
    return (
        "This project has a managing vendor, who has not responded yet. "
        "Admin review opens once the vendor responds or the vendor response deadline passes."
    )


def approve_project_ownership_claim_by_admin(claim, admin, decision_reason=None):
    """Approval alone NEVER moves ownership: it opens a governed transfer that
    still has to pass both capacity checks, so an approved claim against a full
    recipient lands in PENDING_CAPACITY rather than forcing an invalid move."""
    if not claim or not admin:
        raise ValueError("Claim and admin are required.")
    if claim.status not in PROJECT_ACTIVE_CLAIM_STATUSES:
        raise ValueError("Claim is not active.")
    premature = claim_admin_review_block_reason(claim)
    if premature:
        raise PermissionError(premature)
    project = Project.query.get(claim.project_id)
    claimant = User.query.get(claim.claimant_user_id)
    if not project or not claimant:
        raise ValueError("Claim project or claimant no longer exists.")
    # The LIVE current owner, not the snapshot taken when the claim was filed -
    # ownership may have moved legitimately since.
    owner_id = project_current_owner_user_id(project)
    if not _transition_claim(
        claim,
        PROJECT_ACTIVE_CLAIM_STATUSES,
        "APPROVED_BY_ADMIN",
        reviewed_at=get_utc_now(),
        reviewed_by_admin_id=admin.id,
        decision_reason=decision_reason,
    ):
        raise ValueError("Claim is not active.")
    transfer = initiate_project_ownership_transfer(
        project,
        initiated_by_user=User.query.get(owner_id),
        recipient_user=claimant,
        retain_vendor_management=False,
        reason="Admin-approved ownership recovery claim.",
    ) if owner_id else None
    claim.transfer_id = transfer.id if transfer else None
    _record_ownership_event(
        claim,
        "claim_approved_by_admin",
        admin=admin,
        reason=decision_reason,
        transfer_id=transfer.id if transfer else None,
    )
    db.session.flush()
    return claim, transfer


def reject_project_ownership_claim_by_admin(claim, admin, decision_reason=None):
    if not claim or not admin:
        raise ValueError("Claim and admin are required.")
    premature = claim_admin_review_block_reason(claim)
    if premature:
        raise PermissionError(premature)
    if not _transition_claim(
        claim,
        PROJECT_ACTIVE_CLAIM_STATUSES,
        "REJECTED",
        reviewed_at=get_utc_now(),
        reviewed_by_admin_id=admin.id,
        decision_reason=decision_reason,
    ):
        raise ValueError("Claim is not active.")
    _record_ownership_event(claim, "claim_rejected_by_admin", admin=admin, reason=decision_reason)
    db.session.flush()
    return claim


def can_convert_to_individual(user):
    """(ok, reason) for a BUSINESS_VENDOR -> INDIVIDUAL downgrade.

    Validation foundation only - there is no account-conversion HTTP route in
    this build, so this exists to be called by one rather than to be a second
    place where the rule is written. Nothing is deleted either way: a blocked
    downgrade just stays a vendor account.
    """
    if not user:
        return False, "Unknown account."
    if not is_business_vendor(user):
        return True, None
    managed = Project.query.filter(Project.manager_vendor_user_id == user.id).count()
    if managed:
        return False, f"{managed} project(s) still list this account as the managing vendor."
    active_transfers = ProjectOwnershipTransfer.query.filter(
        ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES),
        or_(
            ProjectOwnershipTransfer.from_owner_user_id == user.id,
            ProjectOwnershipTransfer.to_user_id == user.id,
            ProjectOwnershipTransfer.initiated_by_user_id == user.id,
        ),
    ).count()
    if active_transfers:
        return False, f"{active_transfers} ownership transfer(s) are still in progress."
    open_claims = (
        ProjectOwnershipClaim.query.join(Project, Project.id == ProjectOwnershipClaim.project_id)
        .filter(
            ProjectOwnershipClaim.status.in_(PROJECT_ACTIVE_CLAIM_STATUSES),
            or_(
                Project.current_owner_user_id == user.id,
                Project.manager_vendor_user_id == user.id,
                ProjectOwnershipClaim.claimant_user_id == user.id,
            ),
        )
        .count()
    )
    if open_claims:
        return False, f"{open_claims} ownership review request(s) are still open."
    return True, None


def _project_specific_coverage_candidates(project, now):
    if not project:
        return []
    return ProjectServiceCoverage.query.filter(
        ProjectServiceCoverage.project_id == project.id,
        ProjectServiceCoverage.status == "ACTIVE",
        ProjectServiceCoverage.coverage_start <= now,
        or_(ProjectServiceCoverage.coverage_end.is_(None), ProjectServiceCoverage.coverage_end > now),
    ).all()


def add_project_service_coverage(project, source_type, coverage_start=None, coverage_end=None, source_id=None, source_reference=None, created_by_user=None, created_by_admin=None, reason=None):
    coverage = ProjectServiceCoverage(
        project_id=project.id,
        source_type=source_type,
        source_id=source_id,
        source_reference=source_reference,
        coverage_start=coverage_start or get_utc_now(),
        coverage_end=coverage_end,
        status="ACTIVE",
        created_by_user_id=created_by_user.id if created_by_user else None,
        created_by_admin_id=created_by_admin.id if created_by_admin else None,
        reason=reason,
    )
    db.session.add(coverage)
    db.session.flush()
    return coverage


def _project_future_coverage_candidates(project, now):
    """ACTIVE, not-yet-expired coverage rows - including ones that start in the
    future. Deliberately NOT filtered by coverage_start (unlike
    _project_specific_coverage_candidates, which answers "is it live right
    now"): renewal chaining has to see coverage bought for a later window, or
    a second purchase would overlap and waste the first."""
    if not project:
        return []
    return ProjectServiceCoverage.query.filter(
        ProjectServiceCoverage.project_id == project.id,
        ProjectServiceCoverage.status == "ACTIVE",
        or_(ProjectServiceCoverage.coverage_end.is_(None), ProjectServiceCoverage.coverage_end > now),
    ).all()


def project_renewal_anchor(project, now=None):
    """The instant a newly purchased project-service period must start from.

    Returns the latest already-paid-for horizon across every coverage source
    (current owner's account subscription + this project's own ACTIVE
    coverages), or `now` when nothing covers it. Coverage_end is exclusive
    everywhere in this codebase (`coverage_end > now` means live), so chaining
    start = previous end is contiguous with no overlap and no wasted day -
    the same convention VALIDITY_EXTENSION already uses for subscriptions.

    Returns None when an *indefinite* coverage (coverage_end IS NULL) is
    active: no finite purchase could ever become effective on top of it.
    Project.is_active is deliberately ignored here - admin suspension governs
    live availability, not whether paid dates exist underneath it.
    """
    now = now or get_utc_now()
    if not project:
        return now
    anchor = now

    owner_id = project_current_owner_user_id(project)
    owner = User.query.get(owner_id) if owner_id else None
    if owner and owner.has_active_subscription():
        horizon = owner.subscription_expires_at
        if horizon is None and owner.subscription_status == "trial":
            # A trial has no subscription_expires_at but is not open-ended.
            trial = owner.trial_details
            horizon = trial.trial_end if trial else None
        if horizon is None:
            return None
        if horizon > anchor:
            anchor = horizon

    for coverage in _project_future_coverage_candidates(project, now):
        if coverage.coverage_end is None:
            return None
        if coverage.coverage_end > anchor:
            anchor = coverage.coverage_end
    return anchor


def project_renewal_eligibility(project, now=None):
    """LEGACY_COMPATIBILITY rule (Domain 2A backfilled every then-active
    project an indefinite, never-ending coverage row). Chosen rule: block paid
    standalone renewal while any indefinite coverage is active, rather than
    normalizing it away. Normalizing would have to cap a currently-unlimited
    live project at the purchased horizon - the user would pay to receive
    strictly *less* coverage, and a bad cap would take a live QR offline. A
    read-only guard cannot lose data; normalization is an ops migration that
    can happen later without a schema change.
    """
    if not project:
        return False, "PROJECT_NOT_FOUND", "Project not found."
    if project_renewal_anchor(project, now) is None:
        return False, "COVERAGE_ALREADY_INDEFINITE", "This ScanStory is already covered without an end date; renewal is not required."
    return True, None, None


def apply_standalone_project_renewal(project, user, days, source_id, source_reference=None, reason=None, now=None):
    """Create exactly one STANDALONE_PROJECT_RENEWAL coverage row, chained so
    it never overlaps existing coverage. Never touches
    User.subscription_expires_at - project service is project-specific."""
    days = int(days or 0)
    if days <= 0:
        raise ValueError("Renewal duration must be a positive number of days.")
    anchor = project_renewal_anchor(project, now)
    if anchor is None:
        raise ValueError("Project already has indefinite coverage.")
    return add_project_service_coverage(
        project,
        "STANDALONE_PROJECT_RENEWAL",
        coverage_start=anchor,
        coverage_end=anchor + timedelta(days=days),
        source_id=source_id,
        source_reference=source_reference,
        created_by_user=user,
        reason=reason,
    )


def admin_grant_project_service_coverage(project, admin, days, reason, now=None):
    """Governed admin grant. Finite only - the existing admin policy has no
    indefinite-grant capability and this checkpoint does not add one."""
    if not project or not admin:
        raise ValueError("Project and admin are required.")
    days = int(days or 0)
    if days <= 0:
        raise ValueError("Admin grant requires a finite positive duration in days.")
    if not (reason or "").strip():
        raise ValueError("Admin grant requires a reason.")
    now = now or get_utc_now()
    anchor = project_renewal_anchor(project, now) or now
    coverage = add_project_service_coverage(
        project,
        "ADMIN_GRANT",
        coverage_start=anchor,
        coverage_end=anchor + timedelta(days=days),
        created_by_admin=admin,
        reason=reason.strip(),
    )
    log_admin_activity(
        admin.id,
        "project_coverage_grant",
        f"Granted {days} days service coverage to project {project.id}: {reason.strip()[:150]}",
    )
    return coverage


def _project_coverage_ended_in_past(project, now):
    """True when this project HAS been covered and that coverage has run out.

    The one fact needed to tell "expired" apart from "never covered" - which the
    is_live/reason pair cannot express, because both collapse to
    'no_valid_coverage'.
    """
    owner_id = project_current_owner_user_id(project)
    owner = User.query.get(owner_id) if owner_id else None
    if owner and owner.subscription_expires_at and owner.subscription_expires_at <= now:
        return True
    return db.session.query(ProjectServiceCoverage.id).filter(
        ProjectServiceCoverage.project_id == project.id,
        ProjectServiceCoverage.coverage_end.isnot(None),
        ProjectServiceCoverage.coverage_end <= now,
    ).first() is not None


def project_coverage_state(project, access_state, now):
    """'suspended' | 'active' | 'expired' | 'none' - the badge-level state.

    SUSPENDED IS DELIBERATELY NOT 'expired'. An admin-suspended project and a
    project whose coverage lapsed are different problems with different fixes,
    and collapsing them is how a UI ends up telling someone to buy coverage that
    would not bring their project back.
    """
    if not project:
        return "none"
    if not project.is_active:
        return "suspended"
    if access_state["is_live"]:
        return "active"
    return "expired" if _project_coverage_ended_in_past(project, now) else "none"


def project_coverage_summary(project, now=None):
    now = now or get_utc_now()
    state = project_public_access_state(project, now)
    anchor = project_renewal_anchor(project, now)
    eligible, code, _message = project_renewal_eligibility(project, now)
    return {
        "project_id": project.id if project else None,
        "coverage_state": project_coverage_state(project, state, now),
        "is_live": state["is_live"],
        "reason": state["reason"],
        "coverage_source": state["coverage_source"],
        "effective_coverage_until": state["effective_coverage_until"].isoformat() if state["effective_coverage_until"] else None,
        "renewal_starts_at": anchor.isoformat() if anchor else None,
        "renewal_eligible": eligible,
        "renewal_blocked_code": code,
        "is_suspended": bool(project and not project.is_active),
    }


def project_public_access_state(project, now=None):
    now = now or get_utc_now()
    if not project:
        return {"is_live": False, "reason": "not_found", "effective_coverage_until": None, "coverage_source": None}
    if not project.is_active:
        return {"is_live": False, "reason": "inactive", "effective_coverage_until": None, "coverage_source": None}

    best_until = None
    best_source = None
    has_indefinite = False

    owner = User.query.get(project_current_owner_user_id(project)) if project_current_owner_user_id(project) else None
    if owner and owner.has_active_subscription():
        best_source = "OWNER_SUBSCRIPTION"
        best_until = owner.subscription_expires_at
        has_indefinite = best_until is None
    elif project.owner_admin_id and not project_current_owner_user_id(project):
        # Admin-owned projects (platform demo/preview rows) carry ownership on
        # owner_admin_id, so they can never establish OWNER_SUBSCRIPTION coverage
        # and were permanently unavailable at all 13 enforcement surfaces (P0-9).
        # They are platform-operated rather than customer-billed: coverage is the
        # owning Admin account being active, which is a real authorization fact
        # rather than a synthesized paid User subscription. project.is_active is
        # still checked above, so admin suspension keeps working unchanged, and
        # the branch is deliberately gated on there being NO user owner so a
        # transferred-to-user project is judged by the user rule only.
        owner_admin = Admin.query.get(project.owner_admin_id)
        if owner_admin and owner_admin.is_active:
            best_source = "ADMIN_OWNED"
            best_until = None
            has_indefinite = True

    for coverage in _project_specific_coverage_candidates(project, now):
        if coverage.coverage_end is None:
            has_indefinite = True
            best_until = None
            best_source = coverage.source_type
        elif not has_indefinite and (best_until is None or coverage.coverage_end > best_until):
            best_until = coverage.coverage_end
            best_source = coverage.source_type

    if best_source:
        return {
            "is_live": True,
            "reason": "covered",
            "effective_coverage_until": None if has_indefinite else best_until,
            "coverage_source": best_source,
        }
    return {"is_live": False, "reason": "no_valid_coverage", "effective_coverage_until": None, "coverage_source": None}


def _project_is_available(project):
    return bool(project_public_access_state(project)["is_live"])


# ---------------------------------------------------------------------
# Scanner fallback video resolution (V1 Wave 6)
# ---------------------------------------------------------------------
def _fallback_video_payload(pair):
    """Never includes a raw filesystem path - video_url is the same
    url_for("serve_video", ...) helper ProjectPair.get_video_url() already
    uses, which itself re-checks project availability on every request."""
    return {
        "available": True,
        "source": "project_default",
        "pair_index": pair.pair_index,
        "video_url": pair.get_video_url(),
    }


def resolve_scanner_fallback_video(project):
    """Resolve the EXPLICIT fallback video (if any) a scanner client may offer for
    `project`. Fallback is available if and only if the project creator configured
    `Project.fallback_pair_id` AND that pair's video is actually servable
    (`ProjectPair.can_serve_video`) - nothing else makes `available: true`.

    Fix 6 (V1 Agent 2): this used to also treat ANY pair the client had merely matched/
    tracked toward (a `pair_index` hint, sent unconditionally on every confirmed match -
    see `verifiedFallbackPairContext` in scanner.html) as an implicit fallback candidate,
    checked BEFORE `fallback_pair_id` and regardless of whether the creator ever configured
    one. That meant an ordinary matched pair's own AR trigger video could be offered back to
    the visitor as "fallback" even when no explicit fallback was ever set up - a real
    product bug, not an intentional "partial detection" feature (the previous docstring's
    framing was inaccurate; there is no code path here that only fires on a *partial* match).
    The `pair_index` hint is no longer accepted by this function at all: it has no remaining
    legitimate purpose once fallback is explicit-only, and accepting-but-ignoring it would
    just leave a dead parameter inviting the same confusion again.

    The ProjectPair lookup below is scoped to `project_id=project.id` - a fallback response
    for one project can never resolve to another project's pair even if `fallback_pair_id`
    happens to reference a row that exists (under a different project) after a project delete/
    reassignment edge case.
    """
    if project.fallback_pair_id:
        pair = ProjectPair.query.filter_by(id=project.fallback_pair_id, project_id=project.id).first()
        if pair and pair.can_serve_video:
            return _fallback_video_payload(pair)

    return {"available": False}


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
    # Returned so callers that need a stable, auditable id to key an
    # entitlement ledger row against (admin grants) can use it.
    return activity
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


def _schedule_project_pair_processing(project_id, failure_flash="Processing queue is unavailable. Please retry later.", attempt_scope="initial"):
    enqueue_start = time.perf_counter()
    try:
        if attempt_scope == "initial":
            job, created = enqueue_project_pair_processing(project_id)
        else:
            job, created = enqueue_project_pair_processing(project_id, attempt_scope=attempt_scope)
        if created:
            app.logger.info(f"processing_job_enqueued job_id={job.id} project_id={project_id} type={job.job_type}")
        else:
            app.logger.info(f"processing_job_duplicate_ignored job_id={job.id} project_id={project_id} type={job.job_type}")
        _log_processing_timing(
            "processing_job_enqueue",
            job_id=job.id,
            project_id=project_id,
            job_type=job.job_type,
            enqueue_duration_ms=_elapsed_ms(enqueue_start),
            status="queued" if created else "duplicate_active",
            attempt_scope=attempt_scope,
        )
        return job
    except QueueUnavailable:
        app.logger.error(f"processing_queue_unavailable project_id={project_id}")
        _log_processing_timing(
            "processing_job_enqueue",
            project_id=project_id,
            job_type="process_project_pairs",
            enqueue_duration_ms=_elapsed_ms(enqueue_start),
            status="failed",
            safe_error_code="QUEUE_UNAVAILABLE",
        )
        if has_request_context():
            flash(failure_flash, "error")
        return None
# Add this after the helper function
@app.template_filter('project_display_number')
def project_display_number_filter(project):
    """Jinja2 filter to get display number"""
    return get_project_display_number(project)


@app.template_filter('friendly_date')
def friendly_date_filter(value):
    """Render either a datetime or an ISO-8601 string as '31 Dec 2026'.

    The coverage/capacity summaries are JSON-shaped (they are served over HTTP
    too), so their dates reach a template as strings while ORM columns reach
    it as datetimes. One filter handles both instead of each template slicing
    the string itself.
    """
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            value = dt.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    try:
        return value.strftime("%d %b %Y")
    except AttributeError:
        return str(value)
# --------------------------------------------------------------------------------------------
# SMTP Email
# --------------------------------------------------------------------------------------------
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# CR, LF and NUL are the only characters that can end a header line early and
# therefore forge Bcc/Cc/Reply-To/any custom header. Everything else - including
# every non-ASCII name - is legitimate header content.
_HEADER_BREAK_CHARS = ("\r", "\n", "\x00")


def safe_email_header(value, field="header"):
    """Return `value` only if it cannot break out of a single header line.

    REJECTS rather than strips: a contact-form name containing a newline is an
    injection attempt, not a typo, and silently mangling it hides the attempt
    from the operator. Unicode is preserved untouched - non-ASCII is RFC 2047
    encoded at the point of use (see send_email_smtp below), never stripped.
    This is the ONE enforcement point every mail path routes through, so no
    caller has to remember to sanitize.
    """
    text = str(value or "")
    if any(char in text for char in _HEADER_BREAK_CHARS):
        raise ValueError(f"Line breaks are not allowed in the email {field}.")
    return text


def send_email_smtp(to_email: str, subject: str, html_body: str):
    host = os.environ.get("SMTP_HOST")
    port = _smtp_port()
    username = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    mail_from = os.environ.get("MAIL_FROM", username)
    timeout = _smtp_timeout_seconds()
    security = _smtp_security_mode()

    if not all([host, port, username, password, mail_from]):
        raise RuntimeError("SMTP env vars missing.")

    # Header-derived values are validated BEFORE the message is built, so an
    # injected header never reaches the wire and no partial send happens.
    # html_body is deliberately NOT checked: newlines in a body are ordinary.
    to_email = safe_email_header(to_email, "recipient address")
    mail_from = safe_email_header(mail_from, "sender address")
    subject = safe_email_header(subject, "subject")

    msg = MIMEMultipart("alternative")
    msg["From"] = mail_from
    msg["To"] = to_email
    # A non-ASCII subject must be RFC 2047 encoded or msg.as_string() produces a
    # string smtplib cannot encode; ASCII subjects stay human-readable as before.
    msg["Subject"] = subject if subject.isascii() else Header(subject, "utf-8")
    msg.attach(MIMEText(html_body, "html"))
    
    context = ssl.create_default_context()
    smtp_client = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    with smtp_client(host, port, timeout=timeout) as server:
        server.ehlo()
        if security == "starttls":
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

def _clear_normal_user_auth_session():
    session.pop("user_id", None)
    session.pop("user_email", None)

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
                if (
                    view.__name__ == "dashboard"
                    and request.args.get("admin_view") == "true"
                    and request.args.get("user_id", type=int)
                ):
                    return view(*args, **kwargs)
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("login"))
        if getattr(u, "is_blocked", False):
            logout_user()
            flash("Your account is blocked. Contact support.", "error")
            return redirect(url_for("login"))
        if not getattr(u, "is_verified", False):
            pending_email = u.email
            _clear_normal_user_auth_session()
            session["pending_verify_email"] = pending_email
            flash("Please verify your email before continuing.", "warning")
            return redirect(url_for("verify_email"))
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


@app.route("/api/processing/jobs/<int:job_id>", methods=["GET"])
def processing_job_status(job_id):
    job = ProcessingJob.query.get_or_404(job_id)
    user = current_user()
    admin = current_admin()
    authorized = False
    if user and job.owner_user_id == user.id:
        authorized = True
    if admin:
        if admin_has_permission(admin, "superadmin.operations.view"):
            authorized = True
        elif job.owner_admin_id == admin.id:
            authorized = True
    if not authorized:
        abort(404)
    response = jsonify(processing_job_status_payload(job))
    response.headers["Cache-Control"] = "no-store"
    return response

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


# ---------------------------------------------------------------------------
# Effective project capacity (Domain 2B).
#
# SOURCE OF TRUTH: User.subscribed_project_limit stays the *materialized
# effective* limit (base plan capacity + purchased PROJECT_CAPACITY ledger
# deltas), which is what _apply_entitlement_transaction() already did before
# this checkpoint and what _reserve_project_quota_atomic() compares against
# inside a single atomic UPDATE. Computing the limit dynamically instead would
# mean replacing that atomic reservation with a read-then-write, losing the
# concurrency guarantee - so the ledger stays the *audit* trail and the column
# stays the *enforcement* value. Every place that re-syncs the column from a
# plan must therefore route through reconciled_project_limit() so purchased
# capacity is never silently dropped (rule 3: it survives subscription lapse).
# There is exactly one addition of ledger to plan, so no double counting.
# ---------------------------------------------------------------------------
def purchased_project_capacity(user):
    """Auditable sum of PROJECT_CAPACITY entitlement deltas for this user."""
    if not user:
        return 0
    total = db.session.query(
        func.coalesce(func.sum(EntitlementTransaction.delta_value), 0)
    ).filter(
        EntitlementTransaction.user_id == user.id,
        EntitlementTransaction.entitlement_type == "PROJECT_CAPACITY",
    ).scalar()
    return int(total or 0)


def reconciled_project_limit(user, plan_project_limit):
    """Base plan capacity + purchased capacity. None/0 plan limit = unlimited."""
    if plan_project_limit in (None, 0):
        return plan_project_limit
    return int(plan_project_limit) + purchased_project_capacity(user)


def _add_calendar_months(start, months):
    """Add whole calendar months, clamping to the last valid day of the target
    month (31 Jan + 1 month -> 28/29 Feb). stdlib only; no dateutil dependency.
    """
    months = int(months or 0)
    if months <= 0:
        return start
    total = (start.year * 12 + (start.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return start.replace(year=year, month=month, day=day)


def purchased_scan_capacity(user):
    """Auditable sum of EXTRA_SCANS entitlement deltas for this user.

    Exact mirror of purchased_project_capacity: scans were the neglected twin
    of projects (P0-1) - the ledger was written correctly by add-on fulfilment
    but nothing ever re-added it when a plan re-synced the materialized column.
    """
    if not user:
        return 0
    total = db.session.query(
        func.coalesce(func.sum(EntitlementTransaction.delta_value), 0)
    ).filter(
        EntitlementTransaction.user_id == user.id,
        EntitlementTransaction.entitlement_type == "EXTRA_SCANS",
    ).scalar()
    return int(total or 0)


def reconciled_scan_limit(user, plan_scan_limit):
    """Base plan scan allowance + purchased EXTRA_SCANS. None/0 = unlimited."""
    if plan_scan_limit in (None, 0):
        return plan_scan_limit
    return int(plan_scan_limit) + purchased_scan_capacity(user)


_UNSET = object()


def materialize_plan_entitlements(user, plan_project_limit=_UNSET, plan_scan_limit=_UNSET):
    """The ONE place the materialized entitlement columns are ever written.

    Wave 1 established that these columns must always be rebuilt as
    "base plan allowance + entitlement ledger", never assigned a bare plan or
    admin number, or purchased capacity is silently destroyed (P0-1). Wave 2
    added two more writers (deferred plan changes, admin scan-limit edits), so
    the rule is now enforced by having exactly one function that does it.

    Omitted arguments leave that column untouched.
    """
    if plan_project_limit is not _UNSET:
        user.subscribed_project_limit = reconciled_project_limit(user, plan_project_limit)
    if plan_scan_limit is not _UNSET:
        user.subscribed_scan_limit = reconciled_scan_limit(user, plan_scan_limit)


def effective_project_limit(user):
    """The one capacity number every project-capacity check must use.

    Returns None for "unlimited" (matching _limit_reached's own convention).
    """
    if not user or has_dev_test_entitlement(user):
        return None
    limit = user.subscribed_project_limit
    if limit in (None, 0):
        return None
    return int(limit)


def project_capacity_summary(user):
    limit = effective_project_limit(user)
    purchased = purchased_project_capacity(user)
    used = int(user.projects_used or 0)
    over_capacity = False if limit is None else used > limit
    return {
        "effective_project_limit": limit,
        "purchased_project_capacity": purchased,
        "base_project_limit": None if limit is None else max(0, limit - purchased),
        "projects_used": used,
        "projects_remaining": None if limit is None else max(0, limit - used),
        "over_capacity": over_capacity,
        "unlimited": limit is None,
    }


V11_EXPERIENCE_PRESENTATION = (
    {
        "experience_type": "image_video",
        "playback_mode": "tracked_overlay",
        "label": "Tracked Overlay",
        "description": "Image-to-video recognition with a tracked overlay.",
    },
    {
        "experience_type": "image_video",
        "playback_mode": "detect_once",
        "label": "Detect Once",
        "description": "Image-to-video recognition that opens the video after detection.",
    },
    {
        "experience_type": "direct_qr",
        "playback_mode": "direct",
        "label": "Direct QR",
        "description": "QR opens video directly without requiring a target image.",
    },
)


def plan_experience_options(plan):
    """Per-plan experience presentation, annotated with the plan's REAL
    entitlement flags via the central resolver's allowed_playback_modes()."""
    modes = _ent.allowed_playback_modes(plan)
    return [
        dict(option, allowed=option["playback_mode"] in modes)
        for option in V11_EXPERIENCE_PRESENTATION
    ]


def plan_media_policy_display(plan):
    """Effective per-file media policy for a plan, hard ceiling already applied
    by entitlements.cap(). Never re-derived here."""
    return {
        "max_image": format_bytes_display(_ent.cap(getattr(plan, "max_image_bytes", None), _ent.MAX_IMAGE_SIZE)),
        "max_video": format_bytes_display(_ent.cap(getattr(plan, "max_video_bytes", None), _ent.MAX_VIDEO_SIZE)),
        "base_storage": format_bytes_display(getattr(plan, "base_storage_bytes", None)),
    }


def _limit_display_value(value):
    if value in (None, 0, 999999):
        return "Unlimited"
    return str(int(value))


def format_bytes_display(value):
    """Byte count -> short human string. None/0 -> None ("not specified")."""
    if value in (None, 0):
        return None
    size = float(value)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.10g} {unit}"
        size /= 1024


def format_storage_bytes_display(value):
    """Storage meter display. None means unenforced; zero is still real usage."""
    if value is None:
        return None
    if int(value or 0) == 0:
        return "0 bytes"
    return format_bytes_display(value)


app.jinja_env.globals.update(format_bytes_display=format_bytes_display)


def plan_family_label(family):
    """Plan family -> display label. PLAN_FAMILY_* deliberately reuses the
    ACCOUNT_TYPE_* vocabulary (see models.py), so one label map serves both."""
    key = (family or ACCOUNT_TYPE_INDIVIDUAL).upper()
    return ACCOUNT_TYPE_LABELS.get(key, key)


def purchasable_plans_query():
    """Plans a NEW purchase may be made against - the query form of
    SubscriptionPlan.is_purchasable, which was previously dead code: every
    customer-facing listing filtered on is_active alone, so a DRAFT or
    CLOSED_FOR_NEW_PURCHASE plan was still on sale. Existing subscribers are
    untouched; this only governs what can be newly bought."""
    return SubscriptionPlan.query.filter(
        SubscriptionPlan.is_active.is_(True),
        SubscriptionPlan.lifecycle_status.in_(sorted(PLAN_PURCHASABLE_STATUSES)),
    )


def user_entitlement_summary(user):
    """Read-only UX summary of what this account may currently do.

    Every commercial value here is READ from the central resolver
    (entitlements.get_effective_entitlements via user_entitlements) - this
    function never re-derives plan math, and enforcement stays in the existing
    quota, payment, add-on and project-creation services.
    """
    if not user:
        return None
    ents = user_entitlements(user)
    # ponytail: the base/purchased split stays sourced from
    # project_capacity_summary / purchased_scan_capacity, which split the
    # MATERIALIZED (enforced) column. The resolver's base_* fields are the plan
    # row's own numbers, which can legitimately differ after an admin edits a
    # per-user limit - displaying those would show a "plan + purchased" pair
    # that does not add up to the total on the same screen.
    project_capacity = project_capacity_summary(user)
    purchased_scans = purchased_scan_capacity(user)
    effective_scan_limit = ents["effective_scan_limit"]
    scans_used = ents["scans_used"]
    over_scan_capacity = False if effective_scan_limit is None else scans_used > effective_scan_limit
    pending_plan = getattr(user, "pending_subscription_plan", None)
    return {
        "account_type_label": account_type_label(user),
        "plan_name": user.current_plan_name,
        "subscription_status": ents["subscription_status"],
        "plan_family": ents["plan_family"],
        "plan_family_label": plan_family_label(ents["plan_family"]),
        "plan_lifecycle_status": ents["plan_lifecycle_status"],
        "plan_revision": ents["plan_revision"],
        "base_project_limit": project_capacity["base_project_limit"],
        "purchased_project_capacity": project_capacity["purchased_project_capacity"],
        "effective_project_limit": project_capacity["effective_project_limit"],
        "projects_used": project_capacity["projects_used"],
        "projects_remaining": project_capacity["projects_remaining"],
        "project_unlimited": project_capacity["unlimited"],
        "over_project_capacity": project_capacity["over_capacity"],
        "base_scan_limit": None if effective_scan_limit is None else max(0, effective_scan_limit - purchased_scans),
        "purchased_scan_capacity": purchased_scans,
        "effective_scan_limit": effective_scan_limit,
        "scans_used": scans_used,
        "scan_unlimited": effective_scan_limit is None,
        "over_scan_capacity": over_scan_capacity,
        "max_pairs_per_project": ents["max_pairs_per_project"],
        # Wave 3: usage is now REAL, so storage_usage_tracked is True and the
        # Wave 2 "not tracked yet" disclaimer no longer renders. The numbers
        # below are supplied for whichever checkpoint renders the storage meter;
        # no template is changed here.
        "base_storage_display": format_storage_bytes_display(ents["base_storage_bytes"]),
        "purchased_storage_display": format_storage_bytes_display(ents["purchased_storage_bytes"]),
        "admin_granted_storage_display": format_storage_bytes_display(ents["admin_granted_storage_bytes"]),
        "effective_storage_display": format_storage_bytes_display(ents["effective_storage_bytes"]),
        "storage_used_display": format_storage_bytes_display(ents["storage_used_bytes"]),
        "storage_remaining_display": format_storage_bytes_display(ents["storage_remaining_bytes"]),
        "over_storage": ents["over_storage"],
        "storage_overage_display": format_storage_bytes_display(ents["storage_overage_bytes"]),
        "storage_usage_tracked": ents["storage_usage_tracked"],
        "max_image_display": format_bytes_display(ents["image_policy"]["max_bytes"]),
        "max_video_display": format_bytes_display(ents["video_policy"]["max_bytes"]),
        # Real per-plan experience entitlements, straight off the resolver.
        "allowed_experiences": [
            dict(option, allowed=option["playback_mode"] in ents["allowed_playback_modes"])
            for option in V11_EXPERIENCE_PRESENTATION
        ],
        "pending_plan_name": getattr(pending_plan, "plan_name", None),
        "pending_plan_effective_at": ents["pending_plan_effective_at"],
    }


# Defined here rather than in the constants block above because these read the
# Wave 2 plan-policy helpers, which are declared just above.
app.jinja_env.globals.update(
    plan_family_label=plan_family_label,
    plan_experience_options=plan_experience_options,
    plan_media_policy_display=plan_media_policy_display,
)


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


def _release_project_quota_atomic(user):
    """Give one project slot back. The exact mirror of the reservation above.

    Clamped at zero the same way release_account_storage() clamps bytes, and it
    skips dev-test accounts because _reserve_project_quota_atomic() never
    incremented one - releasing there would drive the counter negative-ward.
    """
    if not user or has_dev_test_entitlement(user):
        return
    used = func.coalesce(User.projects_used, 0)
    User.query.filter(User.id == user.id).update(
        {User.projects_used: case((used < 1, 0), else_=used - 1)},
        synchronize_session=False,
    )


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
    if not requested_pairs:
        # This gate is about GROWTH. Adding zero pairs cannot exceed any limit,
        # and an over-limit grandfathered project must stay editable - replacing
        # media on an existing pair is count-neutral and must never be blocked
        # just because the project already sits above the current plan limit.
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


def _parse_project_experience_type():
    raw = (request.form.get("experience_type") or "image_video").strip().lower()
    if raw not in PROJECT_EXPERIENCE_TYPES:
        raise ValueError("Unsupported experience type")
    return raw


def _normalize_playback_mode(value):
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw == "tracked":
        return "tracked_overlay"
    if raw not in PROJECT_PLAYBACK_MODES:
        raise ValueError("Unsupported playback mode")
    return raw


def _default_playback_mode_for_experience(experience_type):
    return "direct" if experience_type == "direct_qr" else "tracked_overlay"


def _validate_project_experience_playback(experience_type, playback_mode):
    if experience_type == "direct_qr" and playback_mode == "direct":
        return
    if experience_type == "image_video" and playback_mode in {"tracked_overlay", "detect_once"}:
        return
    raise ValueError("Playback mode is not supported for this experience type")


EXPERIENCE_MODE_LABELS = {
    "direct": "Direct QR",
    "detect_once": "Detect Once",
    "tracked_overlay": "Tracked Overlay",
}


def _enforce_experience_entitlement(playback_mode, user):
    """Plan gate on CREATING or CHANGING INTO an experience/playback mode.

    Only ever called from create/change paths, which is precisely what makes
    grandfathering work: an already-created project is never re-checked, so a
    downgrade that removes an entitlement leaves existing projects running.
    """
    if user is None:
        return
    ents = user_entitlements(user)
    if playback_mode not in ents["allowed_playback_modes"]:
        label = EXPERIENCE_MODE_LABELS.get(playback_mode, playback_mode)
        raise ValueError(f"Your current plan does not include {label}.")


def _resolve_project_experience_playback(experience_type_value=None, playback_mode_value=None, user=None):
    experience_type = (experience_type_value or "image_video").strip().lower()
    if experience_type not in PROJECT_EXPERIENCE_TYPES:
        raise ValueError("Unsupported experience type")
    playback_mode = _normalize_playback_mode(playback_mode_value)
    playback_mode = playback_mode or _default_playback_mode_for_experience(experience_type)
    # Combination validity first: an invalid experience/playback pairing must
    # still be rejected as invalid, regardless of what the plan entitles.
    _validate_project_experience_playback(experience_type, playback_mode)
    _enforce_experience_entitlement(playback_mode, user)
    return experience_type, playback_mode


def _parse_project_playback_mode(experience_type, user=None):
    return _resolve_project_experience_playback(
        experience_type, request.form.get("playback_mode"), user=user
    )[1]


def _direct_qr_marker_meta():
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
        "display_orientation": "direct_qr",
    }


def user_entitlements(user):
    """The one entitlement view for this user. Wraps the central resolver so
    call sites never have to remember to pass the dev-test override."""
    return get_effective_entitlements(user, unlimited_override=has_dev_test_entitlement(user))


def get_plan_pairs_limit(user):
    """Max pairs per project for this user's plan, hard-ceiling applied.

    Delegates to the central resolver (entitlements.plan_pairs_limit) so the
    min(plan, MAX_PAIRS_PER_PROJECT_CEILING) rule exists in exactly one place.
    None still means "unlimited / not configured", unchanged.
    """
    if has_dev_test_entitlement(user):
        return None
    return _ent.plan_pairs_limit(getattr(user, "subscription_plan", None))


DEV_TEST_USER_EMAILS = tuple(f"scanstorytest{i:02d}@gmail.com" for i in range(1, 11))
DEV_TEST_CONFIG_KEY = "dev_test_user_identity"


def _production_mode_flag_active():
    return _runtime_production_mode_flag_active()


def scanner_diagnostics_enabled():
    """Dev/testing-only gate for the scanner diagnostics panel. Rendered server-side into
    the template — if this is False the panel's HTML never reaches the page, so the
    client-side ?scanner_debug=1 query flag alone can never surface it in production."""
    if _production_mode_flag_active():
        return False
    return bool(SCANSTORY_TESTING or app.debug or (os.environ.get("FLASK_ENV") or "").strip().lower() == "development")


EXPERIENCE_TYPE_IMAGE_VIDEO = "image_video"
EXPERIENCE_TYPE_DIRECT_QR = "direct_qr"


def direct_qr_experience_supported():
    """True once a project can actually store which experience type it is.

    V1.1 ships the Direct QR Video creator/viewer front end, but the persisted field is
    owned by the backend workstream. Until `Project.experience_type` exists, the creator
    keeps the option visible-but-unpublishable instead of starting an upload that could
    never be replayed correctly, and the viewer keeps defaulting to Image -> Video."""
    return hasattr(Project, "experience_type")


def project_experience_type(project):
    """Experience type for a project, defaulting to the only type V1 could store."""
    value = getattr(project, "experience_type", None) or EXPERIENCE_TYPE_IMAGE_VIDEO
    return EXPERIENCE_TYPE_DIRECT_QR if value == EXPERIENCE_TYPE_DIRECT_QR else EXPERIENCE_TYPE_IMAGE_VIDEO


def project_playback_mode(project):
    """Authoritative playback mode stored on Project, with legacy-safe defaults."""
    experience_type = project_experience_type(project)
    value = getattr(project, "playback_mode", None) or _default_playback_mode_for_experience(experience_type)
    try:
        value = _normalize_playback_mode(value) or _default_playback_mode_for_experience(experience_type)
        _validate_project_experience_playback(experience_type, value)
        return value
    except ValueError:
        return _default_playback_mode_for_experience(experience_type)


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


def apply_pending_plan_change_if_due(user):
    """Apply a deferred (downgrade) plan change once its term boundary passes.

    Attached to the existing request-time limit gate rather than a new cron:
    check_user_limits() already runs on every gated user action and already
    handles term expiry, so it IS this codebase's term-boundary hook.

    KNOWN CEILING (documented, not hidden): a user who never returns is not
    transitioned until their next gated request. That is harmless here because
    an unapplied downgrade only ever leaves the account on a HIGHER allowance,
    never a lower one, and no billing is driven off this field.
    ponytail: move to a scheduled sweep only if a background job ever needs
    the downgraded state without a user request.

    Strictly additive: it changes plan and allowances, and deletes nothing.
    Projects, media, pairs, QR codes and existing playback modes are untouched;
    they simply become grandfathered under the new, lower entitlements.
    """
    if not user or not user.pending_plan_id or not user.pending_plan_effective_at:
        return False
    if user.pending_plan_effective_at > dt.utcnow():
        return False

    plan = SubscriptionPlan.query.get(user.pending_plan_id)
    if not plan:
        # Plan vanished - drop the pending change rather than trap the account.
        user.pending_plan_id = None
        user.pending_plan_effective_at = None
        db.session.commit()
        return False

    now = dt.utcnow()
    user.subscription_id = plan.id
    user.subscription_taken_at = now
    if plan.duration_type == "time":
        user.subscription_expires_at = _add_calendar_months(now, plan.duration_value or 0)
    else:
        user.subscription_expires_at = now + timedelta(days=365 * 10)
    user.subscription_status = "active"
    # Purchased and admin-granted ledger entitlement survives the downgrade -
    # the user paid for it separately from the plan.
    materialize_plan_entitlements(user, plan.total_project_limit, plan.total_scan_limit)
    user.pending_plan_id = None
    user.pending_plan_effective_at = None
    db.session.commit()
    app.logger.info(f"pending_plan_change_applied user_id={user.id} plan_id={plan.id}")
    return True


def check_user_limits(user):
    """
    Single source of truth enforcement:
    - None / NULL limit means unlimited
    - Numeric limit is enforced
    """
    if user.is_blocked:
        return False, url_for("login"), "Account is blocked"

    # Term-boundary processing hook for deferred downgrades (Wave 2).
    apply_pending_plan_change_if_due(user)

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

        if _limit_reached(effective_project_limit(user), user.projects_used):
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

        if _limit_reached(effective_project_limit(user), user.projects_used):
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
def project_media_dirs(project):
    """(images, videos, features, qr) resolved from ACTUAL project ownership.

    P0-4: the delete helper used to hard-code the USER directories, so for an
    admin-owned project (which writes to data_admin/*) os.path.exists() was
    False for every path, nothing was ever unlinked, and 100% of its media was
    orphaned permanently and silently. Ownership - not the calling route -
    decides the directory set, matching processing_operations._dirs_for_project.
    """
    if project is not None and project.owner_admin_id:
        return ADMIN_IMAGES_DIR, ADMIN_VIDEOS_DIR, ADMIN_FEATURES_DIR, ADMIN_QR_DIR
    return IMAGES_DIR, VIDEOS_DIR, FEATURES_DIR, QR_DIR


def _safe_media_path(directory, filename):
    """Join a stored filename onto a media root, refusing to escape that root.

    These names are server-generated ({project_id}_{index}.ext), but they are
    persisted in the database and are never re-validated on read, so the delete
    path must not trust them to be traversal-free.
    """
    if not filename:
        return None
    name = os.path.basename(str(filename).replace("\\", "/"))
    if not name or name in (".", ".."):
        return None
    root = os.path.abspath(directory)
    candidate = os.path.abspath(os.path.join(root, name))
    if os.path.dirname(candidate) != root:
        return None
    return candidate


# --------------------------------------------------------------------------------------------
# Storage accounting glue (V1.1 Wave 3).
#
# MediaObject.storage_key is a stable, root-qualified pointer at the EXISTING
# filesystem layout - "user/images/12_0.jpg", "admin/videos/12_0.mp4". The root
# prefix is required because the user and admin trees reuse the same
# {project_id}_{pair_index} filenames, so an unqualified name is ambiguous
# across them. Nothing about the on-disk scheme changes; the ledger only points
# at it.
# --------------------------------------------------------------------------------------------
MEDIA_STORAGE_ROOTS = {
    "user": {"images": IMAGES_DIR, "videos": VIDEOS_DIR},
    "admin": {"images": ADMIN_IMAGES_DIR, "videos": ADMIN_VIDEOS_DIR},
}
MEDIA_KIND_ROLES = {"images": _storage.MEDIA_ROLE_TRIGGER_IMAGE, "videos": _storage.MEDIA_ROLE_VIDEO}

# Never names a byte figure: allowances are plan/add-on data, not a constant.
STORAGE_LIMIT_MESSAGE = (
    "Not enough storage on your account for this upload. "
    "Delete media you no longer need, add storage, or upgrade your plan."
)
STORAGE_REPLACEMENT_OVER_LIMIT_MESSAGE = (
    "Your account is over its storage allowance, so a replacement must be "
    "smaller than the media it replaces."
)


class _ReplacementRejected(Exception):
    """Internal control flow: a staged replacement failed policy; unwind cleanly."""


def media_storage_root_name(project):
    return "admin" if (project is not None and project.owner_admin_id) else "user"


def build_media_storage_key(project, kind, filename):
    """Ledger key for one retained file, or None if there is no such file."""
    if not filename:
        return None
    name = os.path.basename(str(filename).replace("\\", "/"))
    if not name or name in (".", ".."):
        return None
    return f"{media_storage_root_name(project)}/{kind}/{name}"


def media_storage_abs_path(storage_key):
    """Resolve a ledger key back to an absolute path, refusing traversal."""
    parts = (storage_key or "").split("/")
    if len(parts) != 3:
        return None
    root, kind, name = parts
    directory = MEDIA_STORAGE_ROOTS.get(root, {}).get(kind)
    if not directory:
        return None
    return _safe_media_path(directory, name)


def project_storage_owner_ids(project):
    """(owner_user_id, owner_admin_id) for storage responsibility.

    Reuses the EXISTING ownership rule - project_current_owner_user_id() - so a
    transferred or vendor-managed project's storage follows its current owner
    rather than whoever created it. No second ownership system.
    """
    if project is not None and project.owner_admin_id:
        return None, project.owner_admin_id
    return project_current_owner_user_id(project), None


def account_storage_state(user):
    """(used_bytes, effective_allowance_bytes). None allowance = unenforced."""
    if not user:
        return 0, None
    ents = user_entitlements(user)
    return ents["storage_used_bytes"], ents["effective_storage_bytes"]


def evaluate_project_storage_transfer(project, recipient):
    """(ok, project_bytes) - reusable storage validation for an ownership move.

    A callable primitive, NOT a route: the ownership-transfer HTTP surface does
    not exist yet and building it is out of scope here. Whichever checkpoint
    adds it calls this, then completes the move through
    accept_project_ownership_transfer(), which already moves the accounting in
    the same transaction as the ownership.
    """
    if project is None or recipient is None:
        return False, 0
    used, allowance = account_storage_state(recipient)
    return _storage.evaluate_storage_transfer(project.id, used, allowance)


def record_pair_media_objects(project, pair, image_bytes=None, video_bytes=None, source="upload"):
    """Ledger rows for a newly persisted pair's retained media.

    Only the trigger image and the video. The .npz recognition artifact, the
    _work.jpg / _fast.mp4 derivatives and the QR PNG are server-generated, not
    customer-uploaded, and are deliberately never given a row.
    """
    owner_user_id, owner_admin_id = project_storage_owner_ids(project)
    created = []
    for kind, filename, size in (
        ("images", pair.image_filename, image_bytes),
        ("videos", pair.video_filename, video_bytes),
    ):
        key = build_media_storage_key(project, kind, filename)
        if not key or size is None:
            continue
        created.append(_storage.record_media_object(
            storage_key=key,
            size_bytes=size,
            media_role=MEDIA_KIND_ROLES[kind],
            owner_user_id=owner_user_id,
            owner_admin_id=owner_admin_id,
            project_id=project.id,
            pair_id=pair.id,
            source=source,
            # Admin-owned media is retained and real, but no account is billed
            # for it - there is no subscription behind an admin project.
            counts_toward_quota=owner_user_id is not None,
        ))
    return created


def _unlink_project_media(paths, project_id):
    """Remove media, returning the paths that could not be deleted.

    A missing file is a success (deletion is idempotent and re-runnable). A real
    failure - permission denied, file locked - is logged with the basename only
    and reported to the caller instead of being swallowed by `except: pass`,
    which previously hid genuine failures on the user path too (ANM-06).
    """
    failures = []
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failures.append(path)
            app.logger.error(
                "project_media_unlink_failed project_id=%s file=%s error=%s",
                project_id, os.path.basename(path), safe_error_summary(exc),
            )
    return failures


def release_project_media_accounting(project_id, failed_paths=()):
    """Free ledger bytes for a project's media whose files are genuinely gone.

    `failed_paths` are the absolute paths _unlink_project_media() could not
    remove; their rows stay ACTIVE and stay counted. Returns
    (freed_bytes, retained_object_count).
    """
    freed_by_user = {}
    retained = 0
    for obj in _storage.active_media_objects(project_id=project_id):
        path = media_storage_abs_path(obj.storage_key)
        if path in failed_paths or (path and os.path.exists(path)):
            retained += 1
            app.logger.error(
                "storage_release_blocked project_id=%s storage_key=%s reason=file_still_present",
                project_id, obj.storage_key,
            )
            continue
        _storage.mark_media_object_deleted(obj)
        if obj.owner_user_id and obj.counts_toward_quota:
            freed_by_user[obj.owner_user_id] = freed_by_user.get(obj.owner_user_id, 0) + int(obj.size_bytes or 0)
    for user_id, freed in freed_by_user.items():
        _storage.release_account_storage(user_id, freed)
    return sum(freed_by_user.values()), retained


class ProjectDeletionBlocked(Exception):
    """Hard delete refused because the project is mid-lifecycle.

    `str(exc)` is deliberately a finished, safe, user/admin-facing sentence: no
    ids of other users, no internal state names, no filesystem paths.
    """


def project_deletion_block_reason(project):
    """Safe reason this project must not be hard-deleted, or None if it may be.

    Active means "a human is still owed an outcome": a transfer awaiting
    acceptance or capacity, a dispute, or an open/unreviewed claim. Destroying
    those silently is the failure this guards (V1.1 P0-2). Historical rows
    (COMPLETED / CANCELLED / REJECTED / EXPIRED ...) never block - they are
    preserved by _detach_ownership_history instead.
    """
    if project is None or project.id is None:
        return None
    if ProjectOwnershipTransfer.query.filter(
        ProjectOwnershipTransfer.project_id == project.id,
        ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES),
    ).first():
        return (
            "This project has an ownership transfer in progress. "
            "Complete or cancel the transfer before deleting the project."
        )
    if ProjectOwnershipClaim.query.filter(
        ProjectOwnershipClaim.project_id == project.id,
        ProjectOwnershipClaim.status.in_(PROJECT_ACTIVE_CLAIM_STATUSES),
    ).first():
        return (
            "This project has an unresolved ownership claim. "
            "Resolve the claim before deleting the project."
        )
    return None


def _detach_ownership_history(project_id, project_name):
    """Preserve ownership/claim audit rows past the project's deletion.

    The live FK is cleared and the project's identity is copied into the
    historical_* columns, so the evidence stays queryable by project id and
    readable by a human without a projects row to join to. Bulk UPDATE rather
    than a per-row loop: nothing here needs ORM events, and it is idempotent -
    a re-run finds no rows still pointing at the project.
    """
    for model in (ProjectOwnershipTransfer, ProjectOwnershipClaim):
        model.query.filter(model.project_id == project_id).update(
            {
                model.historical_project_id: project_id,
                model.historical_project_name: (project_name or "")[:255] or None,
                model.project_id: None,
            },
            synchronize_session=False,
        )


def _delete_project_files_and_rows(project: Project):
    # LIFECYCLE GUARD FIRST - before any unlink, before any storage credit, so a
    # refused delete leaves media and accounting exactly as they were. Enforced
    # here rather than in each route because all four delete paths funnel
    # through this helper and none of them may bypass it.
    blocked = project_deletion_block_reason(project)
    if blocked:
        raise ProjectDeletionBlocked(blocked)

    images_dir, videos_dir, features_dir, qr_dir = project_media_dirs(project)
    project_id = project.id
    project_name = project.name
    pairs = ProjectPair.query.filter_by(project_id=project_id).all()

    targets = []
    for pair in pairs:
        targets.append(_safe_media_path(images_dir, pair.image_filename))
        targets.append(_safe_media_path(videos_dir, pair.video_filename))
        targets.append(_safe_media_path(features_dir, f"{project_id}_{pair.pair_index}.npz"))
        # Derived artifacts that the old helper never removed at all.
        targets.append(_safe_media_path(images_dir, f"{project_id}_{pair.pair_index}_work.jpg"))
        if pair.video_filename:
            stem = os.path.splitext(os.path.basename(str(pair.video_filename)))[0]
            targets.append(_safe_media_path(videos_dir, f"{stem}_fast.mp4"))

    if project.qr_code_path:
        targets.append(_safe_media_path(qr_dir, os.path.basename(project.qr_code_path)))
    if getattr(project, "qr_code_filename", None):
        targets.append(_safe_media_path(qr_dir, project.qr_code_filename))

    failures = _unlink_project_media(dict.fromkeys(p for p in targets if p), project_id)

    # STORAGE IS FREED ONLY AFTER THE PHYSICAL DELETE SUCCEEDED (Wave 3).
    # Deliberately AFTER _unlink_project_media and keyed off its per-path
    # result, never before it and never unconditionally: a row whose file could
    # not be removed stays ACTIVE and stays counted, so the account is never
    # credited bytes that are still on disk. Reconciliation or a retried delete
    # can free them later. Rerunning this is idempotent - an already-DELETED row
    # is skipped and a missing file counts as a successful unlink.
    release_project_media_accounting(project_id, set(failures))

    # Resumable upload sessions are audit history and are deliberately retained
    # with their references cleared rather than cascade-deleted (P0-5). The
    # schema now also enforces ON DELETE SET NULL for the paths that bypass
    # this helper; doing it explicitly here keeps behaviour identical on a
    # SQLite database running without PRAGMA foreign_keys=ON.
    session_conditions = [UploadSession.project_id == project_id]
    pair_ids = [pair.id for pair in pairs]
    if pair_ids:
        session_conditions.append(UploadSession.pair_id.in_(pair_ids))
    UploadSession.query.filter(or_(*session_conditions)).update(
        {UploadSession.project_id: None, UploadSession.pair_id: None},
        synchronize_session=False,
    )

    # Ownership transfer/claim history is audit evidence, not project debris: it
    # is detached and kept, never cascade-deleted (P0-2). Must run before the
    # project row goes, while the FK is still satisfiable.
    _detach_ownership_history(project_id, project_name)

    for pair in pairs:
        db.session.delete(pair)
    db.session.delete(project)
    db.session.commit()
    load_features.cache_clear()

    if failures:
        # Rows are gone but some bytes remain. Surfaced as an operational signal
        # (never to the end user) so the orphan is detectable rather than silent.
        app.logger.error(
            "project_delete_incomplete_media_cleanup project_id=%s orphaned_files=%d",
            project_id, len(failures),
        )
    return failures


def reconcile_storage_ledger(apply_changes=False):
    """Discover pre-ledger media on disk and record it. Returns a report dict.

    NEVER DELETES ANYTHING. It reads the filesystem and writes media_objects
    rows plus the users.storage_used_bytes counter, nothing else - no customer
    media is removed, moved or rewritten, and no MediaObject row is deleted.

    Deterministic and idempotent: the unit of work is the (project, pair, role)
    tuple, the dedup key is the ACTIVE storage_key, and a rerun finds every row
    it created last time and reports it as already-reconciled instead of
    double-counting it. Anything it cannot resolve honestly - a DB row whose
    file is gone, a file with no DB row, a project with no determinable owner -
    is REPORTED, never guessed at and never fabricated into bytes.
    """
    now = get_utc_now()
    report = {
        "discovered": 0, "created": 0, "already_reconciled": 0,
        "missing_files": [], "orphan_files": [], "ambiguous_ownership": [],
        "size_mismatches": [], "counter_drift": [], "errors": [],
        "total_bytes_accounted": 0,
    }
    expected_keys = set()

    rows = (
        db.session.query(ProjectPair, Project)
        .join(Project, ProjectPair.project_id == Project.id)
        .order_by(ProjectPair.project_id.asc(), ProjectPair.pair_index.asc())
        .all()
    )
    for pair, project in rows:
        owner_user_id, owner_admin_id = project_storage_owner_ids(project)
        if owner_user_id is None and owner_admin_id is None:
            # No determinable billing account. Do not guess an owner.
            report["ambiguous_ownership"].append(
                {"project_id": project.id, "pair_id": pair.id, "reason": "no_resolvable_owner"}
            )
            continue

        for kind, filename in (("images", pair.image_filename), ("videos", pair.video_filename)):
            key = build_media_storage_key(project, kind, filename)
            if not key:
                continue
            expected_keys.add(key)
            path = media_storage_abs_path(key)
            if not path or not os.path.exists(path):
                report["missing_files"].append(
                    {"project_id": project.id, "pair_id": pair.id, "storage_key": key}
                )
                continue
            try:
                size_bytes = os.path.getsize(path)
            except OSError as exc:
                report["errors"].append({"storage_key": key, "error": safe_error_summary(exc)})
                continue

            report["discovered"] += 1
            report["total_bytes_accounted"] += size_bytes
            existing = _storage.active_media_object_for_key(key)
            if existing is not None:
                report["already_reconciled"] += 1
                if int(existing.size_bytes or 0) != size_bytes:
                    report["size_mismatches"].append({
                        "storage_key": key,
                        "ledger_bytes": int(existing.size_bytes or 0),
                        "disk_bytes": size_bytes,
                    })
                    if apply_changes:
                        existing.size_bytes = size_bytes
                        existing.reconciled_at = now
                continue

            report["created"] += 1
            if apply_changes:
                obj = _storage.record_media_object(
                    storage_key=key,
                    size_bytes=size_bytes,
                    media_role=MEDIA_KIND_ROLES[kind],
                    owner_user_id=owner_user_id,
                    owner_admin_id=owner_admin_id,
                    project_id=project.id,
                    pair_id=pair.id,
                    source="reconciliation",
                    counts_toward_quota=owner_user_id is not None,
                )
                obj.reconciled_at = now

    # Files on disk that no ProjectPair points at. Reported separately and
    # never counted against anyone - an orphan has no owner to bill.
    for root_name, kinds in MEDIA_STORAGE_ROOTS.items():
        for kind, directory in kinds.items():
            try:
                names = os.listdir(directory)
            except OSError:
                continue
            for name in names:
                if not os.path.isfile(os.path.join(directory, name)):
                    continue
                key = f"{root_name}/{kind}/{name}"
                if key not in expected_keys:
                    report["orphan_files"].append(key)

    if apply_changes:
        db.session.flush()
    for user in User.query.order_by(User.id.asc()).all():
        calculated = _storage.account_storage_used_bytes(user.id)
        stored = _storage.stored_storage_used_bytes(user)
        if calculated != stored:
            report["counter_drift"].append(
                {"user_id": user.id, "stored": stored, "calculated": calculated}
            )
            if apply_changes:
                user.storage_used_bytes = calculated

    if apply_changes:
        db.session.commit()
    else:
        db.session.rollback()
    return report


@app.cli.command("reconcile-storage")
@click.option("--apply", "apply_changes", is_flag=True, default=False, help="Write ledger rows and counters.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Report only (the default).")
@click.option("--json", "json_output", is_flag=True, default=False, help="Emit the full report as JSON.")
def reconcile_storage_command(apply_changes, dry_run, json_output):
    """Reconcile the media storage ledger against the filesystem.

    Read-only by default. Deliberately NOT part of any Alembic migration:
    the schema upgrade creates an empty table, and this command - run by an
    operator, against a host that actually has the media volume mounted -
    populates it. It never deletes customer media.
    """
    if dry_run and apply_changes:
        raise click.UsageError("--dry-run and --apply are mutually exclusive.")
    report = reconcile_storage_ledger(apply_changes=apply_changes)

    # Categories, in one place, so the human-readable and --json outputs cannot
    # describe different findings. "needs_human" marks the categories a
    # scheduled run must NOT be able to exit 0 on (P1-8): a hard error, or
    # ownership that could not be resolved and therefore was left alone.
    # ORPHAN FILES ARE NOT ONE OF THEM: an orphan file is reported, never
    # deleted, and never blocks a run.
    categories = (
        ("missing_files", "Missing files (ledger row exists, file absent on disk)", False),
        ("orphan_files", "Orphan files (file on disk, no ledger row - reported only, never deleted)", False),
        ("ambiguous_ownership", "Ambiguous ownership (left unassigned for a human)", True),
        ("size_mismatches", "Ledger/disk size mismatches", False),
        ("counter_drift", "Account counter drift", False),
        ("errors", "Hard reconciliation errors", True),
    )
    blocking = sum(len(report[key]) for key, _label, needs_human in categories if needs_human)

    if json_output:
        # Machine-readable, and carries only what the text output carries: counts,
        # totals and the report's own entry strings. No credentials, no
        # environment, no absolute media paths beyond what reconcile_storage_ledger
        # already puts in an entry.
        click.echo(json.dumps({
            "mode": "apply" if apply_changes else "dry-run",
            "discovered": report["discovered"],
            "created": report["created"],
            "already_reconciled": report["already_reconciled"],
            "total_bytes_accounted": report["total_bytes_accounted"],
            "counts": {key: len(report[key]) for key, _label, _needs_human in categories},
            "needs_human_total": blocking,
            "findings": {key: [str(entry) for entry in report[key]] for key, _label, _needs_human in categories},
        }, indent=2, default=str))
    else:
        click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
        click.echo(f"Media discovered on disk: {report['discovered']}")
        click.echo(f"Ledger rows created: {report['created']}")
        click.echo(f"Already reconciled: {report['already_reconciled']}")
        click.echo(f"Total bytes accounted: {report['total_bytes_accounted']}")
        for key, label, _needs_human in categories:
            entries = report[key]
            click.echo(f"{label}: {len(entries)}")
            for entry in entries[:20]:
                click.echo(f"  - {entry}")
            if len(entries) > 20:
                click.echo(f"  ... and {len(entries) - 20} more (total {len(entries)}; use --json for all)")
        if not apply_changes:
            click.echo("Dry run: nothing was written. Re-run with --apply to persist.")

    # One audit line per run, in the existing app log - not a new
    # reconciliation-history subsystem.
    app.logger.info(
        "reconcile_storage_run mode=%s discovered=%s created=%s ambiguous=%s errors=%s",
        "apply" if apply_changes else "dry-run",
        report["discovered"], report["created"],
        len(report["ambiguous_ownership"]), len(report["errors"]),
    )
    if blocking:
        # Non-zero so a cron/CI run cannot look clean while storage accounting is
        # unresolved. Nothing was deleted either way.
        raise SystemExit(1)


@app.cli.command("expire-ownership-transfers")
@click.option("--apply", "apply_changes", is_flag=True, default=False, help="Persist EXPIRED transitions. Default is dry-run.")
def expire_ownership_transfers_command(apply_changes):
    """Close pending ownership transfers whose deadline has passed (V1.1 P1-4).

    Read-only by default. Ownership is never changed here and no linked claim is
    cancelled - an expired handover offer and an open claim are separate
    lifecycles. Safe to re-run: an already-EXPIRED transfer is skipped, so a
    second pass neither double-transitions nor errors.
    """
    rows = expired_pending_transfer_query().all()
    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"Pending transfers past their deadline: {len(rows)}")
    expired = 0
    for transfer in rows:
        click.echo(
            f"  transfer_id={transfer.id} project_id={transfer.project_id} "
            f"status={transfer.status} expires_at={transfer.expires_at}"
        )
        if apply_changes and expire_transfer_if_due(transfer):
            expired += 1
    if apply_changes:
        db.session.commit()
        click.echo(f"Transferred to EXPIRED: {expired}")
    else:
        click.echo("Dry run: nothing was written. Re-run with --apply to expire these transfers.")


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


@app.cli.command("recover-processing-jobs")
@click.option("--older-than-minutes", default=30, show_default=True, type=int)
@click.option("--job-id", type=int, default=None)
@click.option("--project-id", type=int, default=None)
@click.option("--apply", "apply_changes", is_flag=True, help="Persist stale-job recovery decisions. Default is dry-run.")
def recover_processing_jobs(older_than_minutes, job_id, project_id, apply_changes):
    """Inspect or recover stale durable processing jobs. CLI-only."""
    cutoff = dt.utcnow() - timedelta(minutes=max(1, older_than_minutes))
    query = ProcessingJob.query.filter(
        ProcessingJob.job_type == "process_project_pairs",
        ProcessingJob.status.in_(["queued", "processing", "retrying", "ready", "claimed", "running", "retry_scheduled"]),
    )
    if job_id:
        query = query.filter(ProcessingJob.id == job_id)
    if project_id:
        query = query.filter(ProcessingJob.project_id == project_id)
    stale_jobs = query.filter(
        or_(
            ProcessingJob.last_heartbeat_at.is_(None),
            ProcessingJob.last_heartbeat_at < cutoff,
        )
    ).order_by(ProcessingJob.created_at.asc()).all()

    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"Stale jobs found: {len(stale_jobs)}")
    for job in stale_jobs:
        retryable = int(job.attempt_count or 0) < int(job.max_attempts or 1)
        action = "retrying" if retryable else "failed"
        click.echo(
            f"job_id={job.id} project_id={job.project_id} status={job.status} "
            f"attempts={job.attempt_count}/{job.max_attempts} action={action}"
        )
        if not apply_changes:
            continue
        if retryable:
            job.status = "retrying"
            job.available_at = dt.utcnow()
            job.safe_error_code = "STALE_JOB_RETRY"
            job.safe_error_summary = "Stale processing job marked eligible for retry."
        else:
            job.status = "failed"
            job.failed_at = dt.utcnow()
            job.completed_at = dt.utcnow()
            job.safe_error_code = "STALE_JOB_FAILED"
            job.safe_error_summary = "Stale processing job exceeded retry budget."
        job.error_code = job.safe_error_code
        job.error_message = job.safe_error_summary
        job.last_heartbeat_at = dt.utcnow()
    if apply_changes:
        db.session.commit()


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
            try:
                _delete_project_files_and_rows(project)
            except ProjectDeletionBlocked as exc:
                # Dev-fixture cleanup must not force-resolve a live ownership
                # workflow; stop and let the operator deal with it explicitly.
                raise click.ClickException(f"Project {project.id}: {exc}") from exc
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
# Per-file upload limits (P0D). These are the IMMUTABLE SERVER SAFETY CEILINGS
# and now live in entitlements.py so the resolver and these upload paths share
# one definition rather than two that can drift. They are deployment config,
# never admin-editable plan fields: a plan can only ever lower them, via
# entitlements.cap() -> min(plan policy, ceiling).
# Re-exported here under their original names ONLY for sizing the absolute
# request cap below (an import-time computation) and for tests that compare
# against the value. They are import-time SNAPSHOTS, so every enforcement site
# reads _ent.MAX_* / the resolver at call time instead - entitlements.py is the
# single canonical definition and therefore the only correct patch point.
MAX_IMAGE_SIZE = _ent.MAX_IMAGE_SIZE
MAX_VIDEO_SIZE = _ent.MAX_VIDEO_SIZE
MAX_IMAGE_DIMENSION_PX = _ent.MAX_IMAGE_DIMENSION_PX
MAX_IMAGE_PIXELS = _ent.MAX_IMAGE_PIXELS
# Optional; unset/0 disables the duration check entirely.
MAX_VIDEO_DURATION_SECONDS = _ent.MAX_VIDEO_DURATION_SECONDS

# ---------------------------------------------------------------------------
# Whole-request ingest cap (P0-7).
#
# Previously MAX_CONTENT_LENGTH was left unset by default, so absolute ingest
# was UNBOUNDED at every layer this repository controls, and per-file size is
# only checked after the body has been fully spooled to disk. A single small
# global cap is not correct either: a legitimate multi-pair project create can
# genuinely approach MAX_VIDEO_SIZE x pairs-per-project.
#
# The contract implemented here is two-tier:
#   1. A finite ABSOLUTE ceiling derived from the hard per-file limits already
#      enforced in this codebase, applied globally by Flask/Werkzeug (which
#      rejects on Content-Length before the body is read).
#   2. A much smaller DEFAULT per-request cap applied in before_request to every
#      endpoint that is not a known large-multipart upload route, so an
#      oversized body is rejected early instead of being spooled.
# Both are env-overridable; the reverse-proxy side of the contract is documented
# in docs/production/README.md (client_max_body_size is SERVER-TEAM-VERIFY).
# ---------------------------------------------------------------------------
# Hard ceiling on pairs any plan may configure (also enforced per-plan by
# entitlements.plan_pairs_limit); used here to size the absolute request cap.
MAX_PAIRS_PER_PROJECT_CEILING = _ent.MAX_PAIRS_PER_PROJECT_CEILING
_MULTIPART_OVERHEAD_BYTES = 8 * 1024 * 1024
ABSOLUTE_MAX_REQUEST_BYTES = (
    (MAX_VIDEO_SIZE + MAX_IMAGE_SIZE) * max(1, MAX_PAIRS_PER_PROJECT_CEILING)
    + _MULTIPART_OVERHEAD_BYTES
)
_max_content_length_env = os.environ.get("MAX_CONTENT_LENGTH")
if _max_content_length_env:
    ABSOLUTE_MAX_REQUEST_BYTES = int(_max_content_length_env)
app.config["MAX_CONTENT_LENGTH"] = ABSOLUTE_MAX_REQUEST_BYTES

# Everything that is not a creator/admin multipart upload route.
DEFAULT_MAX_REQUEST_BYTES = int(
    os.environ.get("MAX_REQUEST_BODY_BYTES", str(64 * 1024 * 1024)) or str(64 * 1024 * 1024)
)
# Endpoints legitimately allowed to send a full multi-pair body.
LARGE_UPLOAD_ENDPOINTS = {
    "handle_upload",              # POST /upload  (multi-pair project create)
    "user_edit_project",          # POST /projects/<id>/edit (pair replacement)
    "admin_handle_upload",        # POST /admin/projects/upload
}


def _endpoint_body_limit(endpoint):
    if endpoint in LARGE_UPLOAD_ENDPOINTS:
        return ABSOLUTE_MAX_REQUEST_BYTES
    return min(DEFAULT_MAX_REQUEST_BYTES, ABSOLUTE_MAX_REQUEST_BYTES)


@app.before_request
def _enforce_request_body_limit():
    """Reject oversized bodies before any decode/parse/spool work happens.

    Runs on Content-Length only. The resumable chunk route keeps its own
    dedicated 413 (it is bounded by RESUMABLE_UPLOAD_CHUNK_MAX_BYTES and is far below
    the default cap), so this never contradicts it.
    """
    declared = request.content_length
    if not declared:
        return None
    limit = _endpoint_body_limit(request.endpoint)
    if declared <= limit:
        return None
    response = jsonify({
        "success": False,
        "code": "REQUEST_TOO_LARGE",
        "error": "Upload is too large.",
        "max_bytes": limit,
    })
    response.status_code = 413
    return response

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
def _load_features_cached(project_id: int, pair_index: int = 0, mtime_ns=None, file_size=None):
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

def load_features(project_id: int, pair_index: int = 0):
    try:
        project = Project.query.get(project_id)
        if project and project.owner_admin_id:
            npz = os.path.join(ADMIN_FEATURES_DIR, f"{project_id}_{pair_index}.npz")
        else:
            npz = os.path.join(FEATURES_DIR, f"{project_id}_{pair_index}.npz")
        stat = os.stat(npz)
    except FileNotFoundError:
        return _empty_features()
    except Exception as e:
        print(f"âŒ load_features stat error for project={project_id}, pair={pair_index}: {e}")
        return _empty_features()
    return _load_features_cached(project_id, pair_index, stat.st_mtime_ns, stat.st_size)


load_features.cache_clear = _load_features_cached.cache_clear


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
    plans = purchasable_plans_query().order_by(SubscriptionPlan.display_order.asc()).all()
    return render_template("user/landing.html", plans=plans)

@app.route("/terms")
def terms_page():
    """Terms and Conditions page"""
    return render_template("user/terms.html")

@app.route("/privacy")
def privacy_page():
    return render_template("user/privacy_policy.html")


CONSENT_POLICY_ENV = {
    "TERMS": "SCANSTORY_TERMS_POLICY_VERSION",
    "PRIVACY": "SCANSTORY_PRIVACY_POLICY_VERSION",
}


def _policy_version(consent_type):
    consent_type = (consent_type or "").strip().upper()
    env_key = CONSENT_POLICY_ENV.get(consent_type)
    if not env_key:
        raise ValueError("Invalid consent type.")
    return (os.environ.get(env_key) or "v1").strip() or "v1"


def _registration_policy_consent_accepted():
    value = (request.form.get("terms") or "").strip().lower()
    return value in {"1", "true", "yes", "on", "accepted"}


def _record_registration_consent_evidence(user, accepted_at):
    metadata = json.dumps(
        {
            "route": "register",
            "form_field": "terms",
            "legal_consent_only": True,
        },
        sort_keys=True,
    )
    for consent_type in ("TERMS", "PRIVACY"):
        policy_version = _policy_version(consent_type)
        existing = UserConsentEvidence.query.filter_by(
            user_id=user.id,
            consent_type=consent_type,
            policy_version=policy_version,
            source_context="registration",
        ).first()
        if existing:
            continue
        db.session.add(
            UserConsentEvidence(
                user_id=user.id,
                consent_type=consent_type,
                policy_version=policy_version,
                accepted_at=accepted_at,
                source_context="registration",
                evidence_metadata=metadata,
            )
        )

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
                    # Use exact values from plan, no defaults. Project limit is
                    # plan + purchased PAYG capacity so a plan re-sync never
                    # silently erases an auditable capacity entitlement.
                    plan_projects = reconciled_project_limit(user, trial_plan.total_project_limit)
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
            entitlement_summary=user_entitlement_summary(user),
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

        # Email content. Both interpolated pieces are user-controlled
        # (enquiry_label falls back to the raw enquiry_type), so a CRLF here is
        # exactly the header-injection case. send_email_smtp() is the
        # enforcement point; this only turns its refusal into a 400 for the
        # submitter instead of a 500.
        subject = f"[{enquiry_label}] Contact Form — {name}"
        try:
            safe_email_header(subject, "subject")
        except ValueError:
            return jsonify({
                'success': False,
                'error': 'Please remove line breaks from the name and enquiry type fields.',
            }), 400

        # Escaped: this HTML goes to a staff inbox, and an f-string with raw
        # form input is how a submitter gets to author markup inside it.
        name, phone, email, project, message, enquiry_label = (
            escape(value) for value in (name, phone, email, project, message, enquiry_label)
        )

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
        
    except Exception:
        # Full detail to the operator log only. The raw exception text can carry
        # the SMTP host and gateway response and went straight to an
        # unauthenticated caller before (P1-1).
        app.logger.exception("contact_form_send_failed")
        return jsonify({
            'success': False,
            'error': 'We could not send your message right now. Please try again shortly.',
        }), 500

@app.route("/profile")
@login_required
def user_profile():
    user = current_user()
    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    projects = Project.query.filter(project_user_access_filter(user.id)).order_by(Project.created_at.desc()).all()
    
    return render_template(
        "user/profile.html",
        user=user,
        trial=trial,
        projects=projects,
        # Every slot number on screen comes from this one authoritative
        # summary - the page never derives a total or a remainder itself.
        capacity=project_capacity_summary(user),
        entitlement_summary=user_entitlement_summary(user),
        get_system_config=get_system_config
    )

@app.route("/projects", methods=["GET"])
@login_required
def projects_page():
    user = current_user()
    q = (request.args.get("q") or "").strip()
    status_filter = request.args.get("status", "all")

    # Same pair-aggregation shape as admin_projects() (readiness by ProjectPair
    # rollup) - reused here rather than inventing a new status concept.
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

    # Presentation passthrough only: the readiness columns below are ALREADY
    # computed by the pair_counts subquery above (they drive the status filter).
    # Selecting them costs no extra query and adds no rule - it just lets the
    # card render the state the filter can already select on, instead of the
    # list showing a "Processing" filter with no per-card processing state.
    query = (
        db.session.query(
            Project,
            pair_counts.c.pair_count,
            pair_counts.c.ready_pair_count,
            pair_counts.c.failed_pair_count,
            pair_counts.c.processing_pair_count,
        )
        .filter(project_user_access_filter(user.id))
        .outerjoin(pair_counts, Project.id == pair_counts.c.project_id)
    )

    if q:
        query = query.filter(Project.name.ilike(f"%{q}%"))

    if status_filter == "ready":
        query = query.filter(func.coalesce(pair_counts.c.pair_count, 0) > 0)
        query = query.filter(func.coalesce(pair_counts.c.pair_count, 0) == func.coalesce(pair_counts.c.ready_pair_count, 0))
    elif status_filter == "processing":
        query = query.filter(func.coalesce(pair_counts.c.processing_pair_count, 0) > 0)
    elif status_filter == "pending":
        query = query.filter(func.coalesce(pair_counts.c.pair_count, 0) > 0)
        query = query.filter(func.coalesce(pair_counts.c.ready_pair_count, 0) == 0)
        query = query.filter(func.coalesce(pair_counts.c.failed_pair_count, 0) == 0)
        query = query.filter(func.coalesce(pair_counts.c.processing_pair_count, 0) == 0)
    elif status_filter == "failed":
        query = query.filter(func.coalesce(pair_counts.c.failed_pair_count, 0) > 0)

    # created_at is the reliable "recent activity" signal here: updated_at exists
    # but the main edit/reprocess flows only mutate ProjectPair rows, not the
    # Project row itself, so it doesn't move on the actions users take most.
    # id DESC breaks ties deterministically instead of relying on DB order.
    rows = query.order_by(Project.created_at.desc(), Project.id.desc()).all()

    # One grouped lookup instead of a per-card query: a transfer in flight has
    # to be visible from the list, but 40 cards must not mean 40 round trips.
    active_transfers = {}
    if rows:
        for transfer in ProjectOwnershipTransfer.query.filter(
            ProjectOwnershipTransfer.project_id.in_([row[0].id for row in rows]),
            ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES),
        ).all():
            active_transfers[transfer.project_id] = transfer.status

    projects = []
    for project, pair_count, ready_pair_count, failed_pair_count, processing_pair_count in rows:
        project.pairs_count = int(pair_count or 0)
        # Same rollup the status filter above already applies, handed to the
        # template so a card can say which state it is in. No new rule: these
        # are the identical aggregates, just no longer discarded.
        project.ready_pairs_count = int(ready_pair_count or 0)
        project.failed_pairs_count = int(failed_pair_count or 0)
        project.processing_pairs_count = int(processing_pair_count or 0)
        # Card-level ownership summary only (one badge). The full ownership /
        # coverage detail lives on the project detail page so a list of 40
        # cards doesn't turn into 40 stacked panels.
        project.viewer_relationship = (
            "owner" if project_current_owner_user_id(project) == user.id else "manager"
        )
        project.is_suspended = not project.is_active
        project.active_transfer_status = active_transfers.get(project.id)
        # P1-9: the per-card coverage summary the list template could not
        # truthfully render before, from the SAME authoritative resolver the
        # project detail page and /api use. The coverage rules are deliberately
        # not re-derived here or in the template.
        # ponytail: one resolver call per card (a few indexed reads each) rather
        # than a hand-rolled bulk query that would duplicate those rules. If a
        # user ever has hundreds of projects on one page, batch it inside
        # project_public_access_state, not here.
        project.coverage_summary = project_coverage_summary(project)
        projects.append(project)

    has_any_projects = db.session.query(Project.id).filter(project_user_access_filter(user.id)).first() is not None

    return render_template(
        "user/projects.html",
        user=user,
        projects=projects,
        q=q,
        status_filter=status_filter,
        has_any_projects=has_any_projects,
    )

@app.route("/projects/<int:project_id>/qr")
@login_required
def download_project_qr(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not user_can_manage_project(user, project):
        abort(404)
    if not project.qr_code_filename:
        abort(404)
    return send_from_directory(
        QR_DIR,
        project.qr_code_filename,
        as_attachment=True,
        download_name=_build_qr_download_filename(project)
    )

# ===========================================================================
# V1.1 Wave 4: ownership transfer / claim HTTP surface.
#
# Every mutation is POST and therefore CSRF-protected by the app-wide
# CSRFProtect (no @csrf.exempt anywhere in this block - these are ordinary
# authenticated browser forms). Authorization is re-derived from the row on
# every request, never from the form, so guessing a transfer or claim id gets
# a 404 rather than someone else's project.
# ===========================================================================
def _notify_ownership(to_user, subject, message):
    """Best-effort ownership notification. NEVER blocks the transaction.

    Matches the existing payment-success pattern exactly: transactional
    correctness has already been committed by the time this runs, and a dead
    SMTP server must not undo an ownership transfer.
    """
    email = (getattr(to_user, "email", "") or "").strip()
    if not email:
        return
    try:
        send_email_smtp(
            email,
            f"ScanStory - {subject}",
            "<html><body style=\"font-family:Arial,sans-serif;padding:20px;\">"
            f"<p>{message}</p>"
            "<p style=\"color:#888;font-size:12px;\">You can review this from your ScanStory account.</p>"
            "</body></html>",
        )
    except Exception as exc:  # pragma: no cover - depends on live SMTP
        print(f"Failed to send ownership notification: {exc}")


def _transfer_for_party(transfer_id, user):
    """A transfer row only if this user is actually a party to it."""
    transfer = ProjectOwnershipTransfer.query.get(transfer_id)
    if not transfer or not user or user.id not in {
        transfer.from_owner_user_id, transfer.to_user_id, transfer.initiated_by_user_id
    }:
        abort(404)
    return transfer


def _claim_for_party(claim_id, user):
    claim = ProjectOwnershipClaim.query.get(claim_id)
    if not claim or not user:
        abort(404)
    if claim.claimant_user_id != user.id and not user_can_respond_to_claim(user, claim):
        abort(404)
    return claim


def _ownership_flash_redirect(exc=None, ok_message=None):
    if exc is not None:
        flash(str(exc) or "That ownership action is not available.", "error")
    elif ok_message:
        flash(ok_message, "success")
    return redirect(url_for("ownership_center"))


@app.route("/ownership", methods=["GET"])
@login_required
def ownership_center():
    """The one place a user can see and act on ownership state.

    A transfer RECIPIENT does not manage the project yet, so the project detail
    page cannot be that place - they would have nowhere to accept from.
    """
    user = current_user()
    incoming = ProjectOwnershipTransfer.query.filter(
        ProjectOwnershipTransfer.to_user_id == user.id,
        ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES),
    ).order_by(ProjectOwnershipTransfer.id.desc()).all()
    outgoing = ProjectOwnershipTransfer.query.filter(
        or_(
            ProjectOwnershipTransfer.from_owner_user_id == user.id,
            ProjectOwnershipTransfer.initiated_by_user_id == user.id,
        ),
        ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES),
    ).order_by(ProjectOwnershipTransfer.id.desc()).all()

    # EXPIRED became a reachable status in P1-4, and `incoming`/`outgoing`
    # deliberately mean "still actionable", so an expired handover had nowhere to
    # appear at all. Listed separately (never merged into the actionable lists)
    # so the terminal state cannot inherit an action control.
    expired_transfers = ProjectOwnershipTransfer.query.filter(
        ProjectOwnershipTransfer.status == "EXPIRED",
        or_(
            ProjectOwnershipTransfer.to_user_id == user.id,
            ProjectOwnershipTransfer.from_owner_user_id == user.id,
            ProjectOwnershipTransfer.initiated_by_user_id == user.id,
        ),
    ).order_by(ProjectOwnershipTransfer.id.desc()).limit(25).all()

    blocked_transfer_ids = {t.id for t in incoming if t.status == "PENDING_CAPACITY"}
    capacity_blocks = {t.id: transfer_capacity_snapshot(t) for t in incoming if t.id in blocked_transfer_ids}

    busy_project_ids = {
        row.project_id
        for row in ProjectOwnershipTransfer.query.filter(
            ProjectOwnershipTransfer.status.in_(PROJECT_ACTIVE_TRANSFER_STATUSES)
        ).all()
    }
    transferable = [
        project
        for project in Project.query.filter(project_user_access_filter(user.id))
        .order_by(Project.id.desc()).limit(100).all()
        if project.id not in busy_project_ids and user_can_transfer_project(user, project)
    ]

    my_claims = ProjectOwnershipClaim.query.filter(
        ProjectOwnershipClaim.claimant_user_id == user.id
    ).order_by(ProjectOwnershipClaim.id.desc()).limit(25).all()
    incoming_claims = [
        claim
        for claim in ProjectOwnershipClaim.query.filter(
            ProjectOwnershipClaim.status.in_(PROJECT_ACTIVE_CLAIM_STATUSES)
        ).order_by(ProjectOwnershipClaim.id.desc()).limit(100).all()
        if user_can_respond_to_claim(user, claim)
    ]

    projects_by_id = {
        p.id: p
        for p in Project.query.filter(
            Project.id.in_(
                {t.project_id for t in incoming + outgoing + expired_transfers}
                | {c.project_id for c in my_claims + incoming_claims}
            )
        ).all()
    } if (incoming or outgoing or expired_transfers or my_claims or incoming_claims) else {}

    return render_template(
        "user/ownership.html",
        user=user,
        incoming=incoming,
        outgoing=outgoing,
        expired_transfers=expired_transfers,
        transferable=transferable,
        my_claims=my_claims,
        incoming_claims=incoming_claims,
        projects_by_id=projects_by_id,
        capacity_blocks=capacity_blocks,
    )


@app.route("/projects/<int:project_id>/transfer", methods=["POST"])
@login_required
def start_project_ownership_transfer(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not user_can_transfer_project(user, project):
        abort(404)
    recipient_email = (request.form.get("recipient_email") or "").strip().lower()
    recipient = User.query.filter(func.lower(User.email) == recipient_email).first() if recipient_email else None
    if not recipient:
        return _ownership_flash_redirect(ValueError("No ScanStory account exists for that email address."))
    try:
        transfer = initiate_project_ownership_transfer(
            project,
            initiated_by_user=user,
            recipient_user=recipient,
            retain_vendor_management=bool(request.form.get("retain_vendor_management")),
            reason=(request.form.get("reason") or "").strip()[:500] or None,
        )
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    _notify_ownership(
        recipient,
        "A ScanStory is being handed over to you",
        f"{user.email} has offered to transfer the ScanStory \"{project.name}\" to your account.",
    )
    return _ownership_flash_redirect(ok_message=f"Transfer request sent to {recipient.email}. Reference #{transfer.id}.")


@app.route("/ownership/transfers/<int:transfer_id>/accept", methods=["POST"])
@app.route("/ownership/transfers/<int:transfer_id>/retry", methods=["POST"])
@login_required
def accept_ownership_transfer_route(transfer_id):
    """Accept, and re-attempt a PENDING_CAPACITY transfer, are the same call.

    Retry is not a separate lifecycle - it re-runs the identical gated
    completion against the SAME row, which is what makes it idempotent.
    """
    user = current_user()
    transfer = _transfer_for_party(transfer_id, user)
    if user.id != transfer.to_user_id:
        abort(404)
    try:
        result = accept_project_ownership_transfer(transfer, acting_user=user)
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    if result.status == "COMPLETED":
        _notify_ownership(
            User.query.get(result.from_owner_user_id),
            "Ownership handover completed",
            f"The ScanStory you offered has been accepted. Transfer #{result.id} is complete.",
        )
        return _ownership_flash_redirect(ok_message="Ownership transferred. The QR code and all media are unchanged.")
    return _ownership_flash_redirect(
        ok_message="Nothing was moved: your account cannot absorb this ScanStory yet. "
                   "Free up capacity and try again - this request stays open."
    )


@app.route("/ownership/transfers/<int:transfer_id>/reject", methods=["POST"])
@login_required
def reject_ownership_transfer_route(transfer_id):
    user = current_user()
    transfer = _transfer_for_party(transfer_id, user)
    try:
        reject_project_ownership_transfer(transfer, user, reason=(request.form.get("reason") or "").strip()[:500] or None)
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    _notify_ownership(
        User.query.get(transfer.from_owner_user_id),
        "Ownership handover declined",
        f"Transfer #{transfer.id} was declined by the recipient. Nothing about the ScanStory changed.",
    )
    return _ownership_flash_redirect(ok_message="Transfer declined.")


@app.route("/ownership/transfers/<int:transfer_id>/cancel", methods=["POST"])
@login_required
def cancel_ownership_transfer_route(transfer_id):
    user = current_user()
    transfer = _transfer_for_party(transfer_id, user)
    try:
        cancel_project_ownership_transfer(
            transfer, acting_user=user, reason=(request.form.get("reason") or "").strip()[:500] or None
        )
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    return _ownership_flash_redirect(ok_message="Transfer withdrawn.")


@app.route("/projects/<int:project_id>/ownership-claim", methods=["POST"])
@login_required
def submit_project_ownership_claim(project_id):
    user = current_user()
    ok, retry_after = _check_rate_limit("ownership_claim", _rate_limit_key("ownership_claim", user.id))
    if not ok:
        flash(f"Too many ownership review requests. Try again in {retry_after} seconds.", "error")
        return redirect(url_for("ownership_center"))
    project = Project.query.get(project_id)
    if not project or project.owner_admin_id:
        abort(404)
    try:
        claim = create_project_ownership_claim(
            project,
            user,
            evidence_summary=(request.form.get("evidence_summary") or "").strip()[:2000] or None,
        )
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    owner = User.query.get(project_current_owner_user_id(project))
    _notify_ownership(
        owner,
        "Someone has asked to review ownership of a ScanStory",
        f"An ownership review request (#{claim.id}) was filed for \"{project.name}\". "
        "Nothing changes unless you agree or the ScanStory team approves it.",
    )
    return _ownership_flash_redirect(
        ok_message=f"Request #{claim.id} submitted. A person reviews every request - nothing moves on its own."
    )


@app.route("/api/ownership/claim-lookup/<int:project_id>", methods=["GET"])
@login_required
def ownership_claim_lookup(project_id):
    """Claim-submission discovery for a project the caller holds a reference to.

    THE REFERENCE. project_id is the identifier already printed into every QR
    code and served by the PUBLIC /scanner/<project_id> page, which anyone -
    logged in or not - can already use to learn that a project exists and to read
    its name and creator display name. This endpoint therefore adds NO new
    disclosure: it answers "eligible / not eligible" and echoes back only the
    fields that public page already shows, and only while that public page would
    itself serve them. Nothing owner-identifying, nothing private, no media, no
    counts.

    Everything the caller is not entitled to - a project that does not exist, an
    admin-owned platform project, a project that is not publicly available, or
    one the caller already owns/manages - returns ONE identical shape with
    eligible=false and no project block, so a near miss and a total miss are
    indistinguishable.

    Read-only. Ownership cannot change here, and filing the claim still goes
    through the existing POST route with its own rate limit and its own
    active-claim dedupe.
    """
    user = current_user()
    ok, retry_after = _check_rate_limit(
        "ownership_claim_lookup", _rate_limit_key("ownership_claim_lookup", user.id)
    )
    if not ok:
        response = jsonify({
            "success": False,
            "code": "RATE_LIMITED",
            "error": "Too many lookups. Please wait before trying again.",
        })
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response

    not_eligible = {
        "success": True,
        "eligible": False,
        "reason_code": "NOT_CLAIMABLE",
        "reason": (
            "This ScanStory cannot be claimed from here. Check the link or QR code you scanned, "
            "or contact ScanStory support."
        ),
        "project": None,
        "claim_url": None,
    }

    project = Project.query.get(project_id)
    if not project or project.owner_admin_id or not _project_is_available(project):
        return jsonify(not_eligible)
    if user_can_manage_project(user, project) or project_current_owner_user_id(project) == user.id:
        return jsonify(not_eligible)

    existing = ProjectOwnershipClaim.query.filter(
        ProjectOwnershipClaim.project_id == project.id,
        ProjectOwnershipClaim.claimant_user_id == user.id,
        ProjectOwnershipClaim.status.in_(PROJECT_ACTIVE_CLAIM_STATUSES),
    ).first()
    return jsonify({
        "success": True,
        "eligible": existing is None,
        "reason_code": "ALREADY_OPEN" if existing else "CLAIMABLE",
        "reason": (
            "You already have an open ownership review request for this ScanStory."
            if existing else
            "You can file an ownership review request. Nothing changes until it is reviewed."
        ),
        # Exactly the two fields /scanner/<project_id> already renders publicly.
        "project": {"id": project.id, "name": project.name},
        "existing_claim_id": existing.id if existing else None,
        "claim_url": url_for("submit_project_ownership_claim", project_id=project.id),
    })


@app.route("/ownership/claims/<int:claim_id>/respond", methods=["POST"])
@login_required
def respond_ownership_claim_route(claim_id):
    user = current_user()
    claim = _claim_for_party(claim_id, user)
    accept = (request.form.get("decision") or "").strip().lower() == "accept"
    try:
        claim, transfer = respond_to_project_ownership_claim(
            claim, user, accept, response_note=(request.form.get("note") or "").strip()[:500] or None
        )
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    _notify_ownership(
        User.query.get(claim.claimant_user_id),
        "Your ownership review request has a response",
        "The current owner agreed - accept the handover from your account to finish it."
        if accept else
        "The current owner did not agree. The ScanStory team will review your request.",
    )
    return _ownership_flash_redirect(
        ok_message="Handover opened for the claimant to accept." if accept
        else "Refused. The request now goes to the ScanStory team for review."
    )


@app.route("/ownership/claims/<int:claim_id>/cancel", methods=["POST"])
@login_required
def cancel_ownership_claim_route(claim_id):
    user = current_user()
    claim = _claim_for_party(claim_id, user)
    try:
        cancel_project_ownership_claim(claim, user)
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _ownership_flash_redirect(exc)
    return _ownership_flash_redirect(ok_message="Request withdrawn.")


@app.route("/projects/delete/<int:project_id>", methods=["POST"])
@login_required
def user_delete_project(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not user_can_manage_project(user, project):
        abort(404)
    
    # Decrement projects count
    blocked = project_deletion_block_reason(project)
    if blocked:
        flash(blocked, "error")
        return redirect(url_for("projects_page"))

    owner = User.query.get(project_current_owner_user_id(project)) if project_current_owner_user_id(project) else None
    if owner:
        owner.projects_used = max(0, (owner.projects_used or 0) - 1)

    _delete_project_files_and_rows(project)
    db.session.commit()

    flash("Project deleted successfully.", "success")
    return redirect(url_for("projects_page"))


@app.route("/projects/<int:project_id>/edit", methods=["GET"])
@login_required
def user_edit_project_page(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not user_can_manage_project(user, project):
        abort(404)

    pairs = ProjectPair.query.filter_by(project_id=project_id).order_by(ProjectPair.pair_index).all()
    return render_template("user/edit_project.html", project=project, pairs=pairs, user=user)


@app.route("/projects/<int:project_id>/edit", methods=["POST"])
@login_required
def user_edit_project(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not user_can_manage_project(user, project):
        abort(404)

    pairs = ProjectPair.query.filter_by(project_id=project_id).order_by(ProjectPair.pair_index).all()
    updated = 0

    # Replacement media must satisfy the CURRENT per-file policy (grandfathering
    # protects what is already stored, never what is newly uploaded). Replacing
    # a pair is count-neutral, so no pair-limit check belongs on this path.
    _ents = user_entitlements(user)
    _img_max, _img_dim, _img_px = image_limits(_ents)
    _vid_max, _vid_dur = video_limits(_ents)

    # STORAGE ACCOUNT for the replacement (Wave 3). Bytes are charged to the
    # CURRENT owner, not the editing manager - a vendor replacing media on a
    # transferred project spends the owner's allowance, matching who the ledger
    # already bills. Usage/allowance are read once and then walked forward
    # locally as each swap is approved, so a multi-pair edit cannot approve two
    # growths that only fit individually.
    storage_owner = User.query.get(project_current_owner_user_id(project)) if project_current_owner_user_id(project) else None
    storage_used, storage_allowance = account_storage_state(storage_owner)
    projected_used = storage_used
    swaps = []

    # PHASE 1 - validate and decide. Nothing is written to disk, no old media is
    # touched, and no expensive reprocessing is scheduled until every requested
    # replacement has passed BOTH the per-file policy and the storage policy.
    staged = []
    try:
        for pair in pairs:
            new_image = request.files.get(f"image_{pair.pair_index}")
            new_video = request.files.get(f"video_{pair.pair_index}")

            for kind, upload, limits in (
                ("images", new_image, (_img_max, _img_dim, _img_px)),
                ("videos", new_video, (_vid_max, _vid_dur)),
            ):
                if not upload or not upload.filename:
                    continue
                filename = pair.image_filename if kind == "images" else pair.video_filename
                if not filename:
                    # Direct-QR pairs have no trigger image to replace.
                    continue
                label = "Image" if kind == "images" else "Video"
                try:
                    if kind == "images":
                        temp_path, _ext = validate_image(upload, TMP_UPLOADS_DIR, *limits)
                    else:
                        temp_path, _ext = validate_video(upload, TMP_UPLOADS_DIR, *limits)
                except UploadValidationError as exc:
                    app.logger.warning(f"Replacement {kind} rejected (pair {pair.pair_index}): {exc.detail}")
                    flash(f"{label} for pair {pair.pair_index + 1}: {exc.safe_message}", "error")
                    raise _ReplacementRejected()
                staged.append(temp_path)

                storage_key = build_media_storage_key(project, kind, filename)
                old_object = _storage.active_media_object_for_key(storage_key) if storage_key else None
                old_bytes = int(getattr(old_object, "size_bytes", 0) or 0)
                new_bytes = os.path.getsize(temp_path)

                allowed, projected_used = _storage.evaluate_replacement(
                    projected_used, storage_allowance, old_bytes, new_bytes
                )
                if not allowed:
                    message = (
                        STORAGE_REPLACEMENT_OVER_LIMIT_MESSAGE
                        if storage_allowance is not None and storage_used > storage_allowance
                        else STORAGE_LIMIT_MESSAGE
                    )
                    flash(f"{label} for pair {pair.pair_index + 1}: {message}", "error")
                    raise _ReplacementRejected()

                swaps.append({
                    "pair": pair, "kind": kind, "temp_path": temp_path,
                    "storage_key": storage_key, "old_object": old_object,
                    "old_bytes": old_bytes, "new_bytes": new_bytes,
                })
    except _ReplacementRejected:
        for temp_path in staged:
            _safe_remove(temp_path)
        return redirect(url_for("user_edit_project_page", project_id=project_id))

    # PHASE 2 - commit the approved swaps. os.replace onto the SAME storage key
    # is the physical delete of the old bytes: it is atomic, and it succeeds or
    # leaves the old file intact, so there is no window where accounting has
    # been freed but the old media survives. QR, pair count and the project's
    # grandfathered experience mode are untouched by all of this.
    for swap in swaps:
        pair, kind = swap["pair"], swap["kind"]
        directory = IMAGES_DIR if kind == "images" else VIDEOS_DIR
        filename = pair.image_filename if kind == "images" else pair.video_filename
        final_path = os.path.join(directory, filename)
        os.replace(swap["temp_path"], final_path)  # already-validated content only
        if kind == "images":
            standardize_uploaded_image(final_path, target_size=1200)
            pair.is_processed = False
            pair.processing_status = "uploaded"
            pair.feature_extraction_status = "pending"
            pair.processing_error = None
        updated += 1

        # standardize_uploaded_image() rewrites the file, so re-stat rather than
        # trusting the pre-swap size - the ledger records the bytes that are
        # actually on disk.
        final_bytes = os.path.getsize(final_path)
        if kind == "images":
            pair.image_size = final_bytes
        else:
            pair.video_size = final_bytes
        if swap["storage_key"]:
            _storage.supersede_media_object(swap["old_object"])
            # Flush the supersede BEFORE inserting the replacement row: both
            # claim the same storage_key, and SQLAlchemy's unit of work emits
            # INSERTs before UPDATEs, which would trip the
            # uq_media_objects_active_storage_key partial unique index.
            db.session.flush()
            _storage.record_media_object(
                storage_key=swap["storage_key"],
                size_bytes=final_bytes,
                media_role=MEDIA_KIND_ROLES[kind],
                owner_user_id=getattr(storage_owner, "id", None),
                project_id=project.id,
                pair_id=pair.id,
                counts_toward_quota=storage_owner is not None,
            )
            if storage_owner is not None:
                # Net delta only. Negative deltas release; the growth case was
                # already authorised by evaluate_replacement above, and the
                # unconditional apply keeps the column consistent with the
                # ledger rows written in this same transaction.
                _storage.reserve_account_storage(
                    storage_owner.id, final_bytes - swap["old_bytes"], None
                )

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
        job = _schedule_project_pair_processing(project_id)
        if not job:
            return redirect(url_for("user_edit_project_page", project_id=project_id))

    flash("Changes saved. Your ScanStory will be ready in about a minute.", "success")
    return redirect(url_for("projects_page"))


@app.route("/projects/<int:project_id>/reprocess", methods=["POST"])
@login_required
def user_reprocess_project(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not user_can_manage_project(user, project):
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

    job = _schedule_project_pair_processing(project_id, attempt_scope="reprocess")
    if not job:
        return redirect(url_for("projects_page"))

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
        if not _registration_policy_consent_accepted():
            flash("Please accept the Terms and Privacy Policy to create an account.", "error")
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
        db.session.flush()

        # Create trial details
        trial = TrialDetails(
            user_id=user.id,
            trial_start=now,
            trial_end=now + timedelta(days=free_trial_days),
            trial_project_limit=free_trial_projects,
            trial_scan_limit=free_trial_scans
        )

        db.session.add(trial)
        _record_registration_consent_evidence(user, now)
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

    except Exception:
        app.logger.exception("registration_failed_unexpected")
        db.session.rollback()
        
        flash("Registration could not be completed. Please try again later.", "error")
        
        return render_template("user/register.html")


@app.route("/verify-email/", methods=["GET", "POST"])
def verify_email():
    email = session.get("pending_verify_email")
    if not email:
        flash("No verification session found. Please register again.", "error")
        return redirect(url_for("register"))
    has_challenge = bool(session.get("pending_verify_challenge_id"))
    
    if request.method == "GET":
        if not has_challenge:
            flash("Request a new verification code on this device before entering an OTP.", "info")
        return render_template("user/verify_email.html", email=email, has_challenge=has_challenge)
    
    otp = (request.form.get("otp") or "").strip()
    challenge_id = session.get("pending_verify_challenge_id")
    if not challenge_id and not _active_otp(email, "verify_email"):
        flash("Request a new verification code on this device before entering an OTP.", "warning")
        return render_template("user/verify_email.html", email=email, has_challenge=False)
    if not _verify_otp(email, "verify_email", otp, challenge_id=challenge_id):
        flash("Verification could not be completed. Please try again or request a new code.", "error")
        return render_template("user/verify_email.html", email=email, has_challenge=has_challenge)
    
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

@app.route("/resend-otp/", methods=["POST"])
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

    # Per-IP bucket (existing) plus an identity+IP bucket, so a single abusive
    # client is throttled per account it targets without the looser network-wide
    # limit having to be tightened for everyone behind the same NAT (P0-8).
    ok, retry_after = _check_rate_limit("login_ip", _rate_limit_key("login"))
    if ok:
        ok, retry_after = _check_rate_limit(
            "login_identity", _rate_limit_key("login_identity", identity_digest(email))
        )
    if not ok:
        flash("Too many login attempts from this network. Please wait and try again.", "error")
        response = make_response(render_template("user/login.html"), 429)
        response.headers["Retry-After"] = str(retry_after)
        return response

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
    if not user.is_verified:
        _clear_normal_user_auth_session()
        session["pending_verify_email"] = user.email
        flash("Please verify your email before logging in.", "warning")
        return redirect(url_for("verify_email"))

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
            plan_projects = reconciled_project_limit(user, trial_plan.total_project_limit)
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
        # Step 1's "Creating this ScanStory for:" choice exists for vendors
        # only; an INDIVIDUAL account never renders it at all.
        viewer_is_business_vendor=is_business_vendor(user),
        video_upload_warnings=VIDEO_UPLOAD_WARNINGS,
        direct_qr_supported=direct_qr_experience_supported(),
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
    try:
        experience_type = _parse_project_experience_type()
        playback_mode = _parse_project_playback_mode(experience_type, user=user)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("user_create_project_page"))
    images = request.files.getlist("images")
    videos = request.files.getlist("videos")
    pair_count = len(videos) if experience_type == "direct_qr" else len(images)
    _upload_log("UPLOAD BODY READY", upload_id, user_id=user.id, pair_count=pair_count, duration_ms=round((time.time() - request_start) * 1000))
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
    _upload_log("UPLOAD VALIDATION START", upload_id, user_id=user.id, pair_count=pair_count)
    if experience_type == "image_video" and (not images or not videos or len(images) != len(videos)):
        flash("Error: Please upload equal number of images and videos", "error")
        return redirect(url_for("user_create_project_page"))
    if experience_type == "direct_qr" and not videos:
        flash("Error: Please upload a video for Direct QR", "error")
        return redirect(url_for("user_create_project_page"))

    # Get max pairs based on subscription plan only
    max_pairs = get_plan_pairs_limit(user)
    if max_pairs is None and not dev_test_entitled:
        flash("Pairs allowed per project is not configured for your current plan. Please contact admin.", "error")
        return redirect(url_for("user_create_project_page"))

    if max_pairs is not None and pair_count > max_pairs:
        flash(f"Your current plan allows maximum {max_pairs} pairs per project.", "error")
        return redirect(url_for("user_create_project_page"))

    if experience_type == "image_video":
        try:
            marker_metadata = [_parse_marker_meta(i) for i in range(len(images))]
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("user_create_project_page"))
    else:
        marker_metadata = [_direct_qr_marker_meta() for _ in videos]

    # Validate every file from its actual content BEFORE any quota
    # reservation or DB row is created (P0D) - a rejected upload must never
    # consume project/pair quota. All-or-nothing: every pair in the request
    # must validate before any of them are persisted.
    validated_media = []
    _ents = user_entitlements(user)
    _img_max, _img_dim, _img_px = image_limits(_ents)
    _vid_max, _vid_dur = video_limits(_ents)
    try:
        for i, video_file in enumerate(videos):
            image_file = images[i] if experience_type == "image_video" else None
            img_temp = img_ext = None
            if image_file is not None:
                try:
                    img_temp, img_ext = validate_image(
                        image_file, TMP_UPLOADS_DIR, _img_max, _img_dim, _img_px
                    )
                except UploadValidationError as exc:
                    app.logger.warning(f"Upload rejected (image, pair {i}, upload_id={upload_id}): {exc.detail}")
                    raise
            try:
                vid_temp, vid_ext = validate_video(
                    video_file, TMP_UPLOADS_DIR, _vid_max, _vid_dur
                )
            except UploadValidationError as exc:
                _safe_remove(img_temp)
                app.logger.warning(f"Upload rejected (video, pair {i}, upload_id={upload_id}): {exc.detail}")
                raise
            validated_media.append({"image_temp": img_temp, "image_ext": img_ext, "video_temp": vid_temp, "video_ext": vid_ext})
    except UploadValidationError as exc:
        for item in validated_media:
            _safe_remove(item.get("image_temp"))
            _safe_remove(item["video_temp"])
        flash(exc.safe_message, "error")
        return redirect(url_for("user_create_project_page"))

    # STORAGE GATE (Wave 3). Two separate checks, both of which must pass: the
    # per-file policy above (bytes/duration/dimensions/pixels, already capped by
    # the immutable server ceiling) and the account storage allowance here. A
    # storage allowance never substitutes for a per-file limit and never
    # relaxes one.
    #
    # The ENTIRE retained logical set is weighed at once - a multi-pair project
    # is accepted or rejected whole, so we never persist pair 1 and then reject
    # pair 2 leaving a half-created project with orphaned accounting.
    #
    # Sizes come from the validated temp files, i.e. the bytes that will
    # actually be retained, not a client-declared length. This is the cheap
    # PRECHECK; the authoritative atomic reservation runs inside the
    # transaction below.
    retained_bytes = []
    for media in validated_media:
        image_bytes = os.path.getsize(media["image_temp"]) if media.get("image_temp") else None
        retained_bytes.append((image_bytes, os.path.getsize(media["video_temp"])))
    total_new_storage_bytes = sum((image_bytes or 0) + video_bytes for image_bytes, video_bytes in retained_bytes)
    storage_used, storage_allowance = account_storage_state(user)
    if not _storage.can_consume(storage_used, storage_allowance, total_new_storage_bytes):
        for media in validated_media:
            _safe_remove(media.get("image_temp"))
            _safe_remove(media["video_temp"])
        flash(STORAGE_LIMIT_MESSAGE, "error")
        return redirect(url_for("user_create_project_page"))

    # STEP 1-3: reserve quota, create project/pairs, and commit as one unit.
    # If DB insert or file save fails, rollback releases the reserved quota and saved files are removed.
    saved_paths = []
    pairs_data = []
    created_pairs = []
    project = None
    try:
        # AUTHORITATIVE storage reservation: one conditional UPDATE on the user
        # row, so two concurrent uploads cannot both read the same headroom and
        # both proceed. A rollback below releases it, exactly like the project
        # slot - a failed upload never leaves permanent usage behind.
        if not _storage.reserve_account_storage(user.id, total_new_storage_bytes, storage_allowance):
            raise ValueError(STORAGE_LIMIT_MESSAGE)

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

        project = Project(
            name=name,
            owner_user_id=user.id,
            created_by_user_id=user.id,
            current_owner_user_id=user.id,
            user_project_index=user_project_index,
            experience_type=experience_type,
            playback_mode=playback_mode,
        )
        _upload_log("UPLOAD PERSIST START", upload_id, user_id=user.id, pair_count=pair_count)
        db.session.add(project)
        db.session.flush()

        pair_slots_ok, pair_slots_error = _reserve_pair_slots_for_project(project.id, pair_count, max_pairs)
        if not pair_slots_ok:
            raise ValueError(pair_slots_error)

        for i, video_file in enumerate(videos):
            image_file = images[i] if experience_type == "image_video" else None
            marker_meta = marker_metadata[i]
            media = validated_media[i]
            img_filename = f"{project.id}_{i}.jpg" if image_file is not None else None
            vid_filename = f"{project.id}_{i}{media['video_ext']}"

            if image_file is not None:
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
                image_path=f"/image/{project.id}/{i}" if image_file is not None else None,
                original_image_name=image_file.filename if image_file is not None else None,
                original_video_name=video_file.filename,
                image_size=(marker_meta["processed_size_bytes"] or image_file.content_length) if image_file is not None else None,
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
                is_processed=experience_type == "direct_qr",
                processing_status="completed" if experience_type == "direct_qr" else "uploaded",
                feature_extraction_status="not_required" if experience_type == "direct_qr" else "pending",
                processing_error=None,
            )
            db.session.add(pair)
            created_pairs.append((pair, retained_bytes[i]))

            pairs_data.append({
                "pair_index": i,
                "image_filename": img_filename,
                "video_filename": vid_filename,
                "video_size": video_size,
                "original_video_name": video_file.filename,
                "video_mime_type": video_file.mimetype,
            })

        # Ledger rows for the retained media, in the SAME transaction as the
        # reservation and the pair rows, so accounting can never be half-applied.
        db.session.flush()
        for pair, (image_bytes, video_bytes) in created_pairs:
            record_pair_media_objects(project, pair, image_bytes=image_bytes, video_bytes=video_bytes)

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
        
        if project.experience_type == "image_video":
            job = _schedule_project_pair_processing(project.id)
            if not job:
                project_pairs = ProjectPair.query.filter_by(project_id=project.id).all()
                for pair in project_pairs:
                    pair.processing_status = "failed"
                    pair.feature_extraction_status = "failed"
                    pair.processing_error = "Processing queue unavailable"
                db.session.commit()
                flash("Project was saved, but processing could not start. Please retry processing later.", "error")
                return redirect(url_for("projects_page"))
            upload_timing["jobs_scheduled_at"] = time.time()
            _upload_log("UPLOAD BG SCHEDULED", upload_id, user_id=user.id, project_id=project.id, pair_count=len(pairs_data), job_id=job.id)
        else:
            upload_timing["jobs_scheduled_at"] = time.time()
            _upload_log("UPLOAD BG SKIPPED", upload_id, user_id=user.id, project_id=project.id, pair_count=len(pairs_data), experience_type=project.experience_type)
        
        print(f"[UPLOAD] Queued background processing for {len(pairs_data)} pairs")
        
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


# --------------------------------------------------------------------------------------------
# Resumable upload API (V1 Wave 5)
#
# A chunked-upload producer into the SAME RQ processing pipeline the
# non-resumable /upload route already uses (enqueue_project_pair_processing /
# _schedule_project_pair_processing, both unmodified). Scope: one
# UploadSession = one new single-pair Project (one image + one video sent as
# a single sequential byte stream, split at `image_size`). See
# models.py's UploadSession docstring for the full design rationale and
# docs/development/resumable-upload-api-contract.md for the wire contract.
# --------------------------------------------------------------------------------------------
UPLOAD_SESSION_TTL_MINUTES = int(os.environ.get("SCANSTORY_UPLOAD_SESSION_TTL_MINUTES", "1440"))
UPLOAD_SESSION_ABANDONED_STALE_MINUTES = int(os.environ.get("SCANSTORY_UPLOAD_SESSION_ABANDONED_STALE_MINUTES", "120"))
UPLOAD_SESSION_CLEANUP_BATCH_LIMIT = int(os.environ.get("SCANSTORY_UPLOAD_CLEANUP_BATCH_LIMIT", "200"))
RESUMABLE_UPLOAD_CHUNK_MAX_BYTES = int(os.environ.get("SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES", str(1024 * 1024)))
if RESUMABLE_UPLOAD_CHUNK_MAX_BYTES <= 0:
    raise RuntimeError("SCANSTORY_RESUMABLE_CHUNK_MAX_BYTES must be a positive integer.")
app.config["RESUMABLE_UPLOAD_CHUNK_MAX_BYTES"] = RESUMABLE_UPLOAD_CHUNK_MAX_BYTES


class _ResumableQuotaLimitReached(Exception):
    """Internal control-flow marker only - never serialized to a client.

    Carries the safe client-facing code/message so the storage gate can reuse
    the same rollback-and-report handler instead of duplicating it.
    """

    def __init__(self, code="PROJECT_LIMIT_REACHED", message="Project limit reached. Please upgrade your plan."):
        super().__init__(code)
        self.code = code
        self.message = message


def _upload_identity():
    """(user, admin) for the current session - exactly one non-None, or
    both None if unauthenticated. Mirrors the same current_user()/
    current_admin() pair the existing /api/processing/jobs/<id> route
    already uses for dual user/admin ownership."""
    user = current_user()
    if user:
        return user, None
    admin = current_admin()
    if admin:
        return None, admin
    return None, None


def _upload_session_owned(session_row, user, admin):
    if user and session_row.owner_user_id == user.id:
        return True
    if admin and session_row.owner_admin_id == admin.id:
        return True
    return False


def _upload_api_error(code, message, http_status, **extra):
    """Every resumable-upload error response: a safe generic message plus a
    machine-readable code, never a raw path/stack trace/secret.

    `**extra` carries only already-safe scalars the client legitimately
    needs to recover without a second round-trip (the authoritative
    current_offset on an offset mismatch, the server chunk ceiling on an
    oversized chunk). On a 0.3 Mbps link an extra GET per recoverable
    rejection is real dead time, which is why these are inlined here
    rather than left to a follow-up status read."""
    payload = {"success": False, "code": code, "error": message}
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = http_status
    return response


def _sanitize_display_text(value, max_len=255):
    """For display-only metadata (project name, original filenames) -
    never used to build a filesystem path. Storage paths are always built
    from the server-generated storage_token instead (see
    _upload_session_temp_path)."""
    if value is None:
        return None
    text = str(value).replace("\x00", "").strip()
    return text[:max_len] or None


def _upload_session_temp_path(storage_token):
    """The ONLY place a resumable-upload temp file path is built - always
    from the server-generated UUID storage_token, never from any
    client-supplied filename."""
    return os.path.join(TMP_UPLOADS_DIR, f"resumable_{storage_token}.part")


def _safe_delete_upload_temp(path):
    """Delete a resumable-upload temp file only if it genuinely resolves
    inside TMP_UPLOADS_DIR. Defense-in-depth alongside storage_token always
    being server-generated: never delete based on an unverified path."""
    if not path:
        return
    tmp_root = os.path.abspath(TMP_UPLOADS_DIR)
    real_path = os.path.abspath(path)
    if real_path != tmp_root and not real_path.startswith(tmp_root + os.sep):
        app.logger.error("upload_session_temp_delete_blocked_outside_root")
        return
    _safe_remove(real_path)


def _lock_upload_session(session_id):
    """Row lock for the chunk-append critical section, mirroring
    _lock_project_for_pair_quota exactly: with_for_update() on
    Postgres/MySQL, relies on SQLite's whole-database write lock on SQLite
    (same _supports_row_level_locking() gate already used elsewhere)."""
    query = UploadSession.query.filter(UploadSession.id == session_id)
    if _supports_row_level_locking():
        query = query.with_for_update()
    return query.first()


def _upload_session_set_state(session_row):
    """Per-content-set state, DERIVED from columns that already exist.

    No new column and no migration: `status`, `current_offset` and
    `expected_total_size` already carry everything the brief's state list
    needs. 'paused' is deliberately NOT in this vocabulary - a paused upload
    and a very slow one are the same row server-side (status='active', a
    partial offset), so a server-reported 'paused' would be a guess the
    client would then have to keep in step with. Pausing is a client fact and
    stays a client fact; what the server owns is the offset, and it reports
    that.
    """
    status = session_row.status
    if status != "active":
        return {
            "finalizing": "finalizing",
            "assembled": "finalizing",
            "completed": "complete",
        }.get(status, "failed_requires_action")
    total = int(session_row.expected_total_size or 0)
    offset = int(session_row.current_offset or 0)
    if total and offset >= total:
        return "uploaded"
    return "uploading" if offset > 0 else "pending"


def _upload_session_group_payload(session_rows):
    """Compact per-content-set view of a multi-content-set project, so a
    client can reconcile every set from ONE response instead of N status GETs
    on a link that can least afford them. Ordered exactly as the caller
    listed the sets, which is the order they become pair_index 0..N-1."""
    return [
        {
            "id": row.id,
            "set_index": index,
            "status": row.status,
            "set_state": _upload_session_set_state(row),
            "current_offset": int(row.current_offset or 0),
            "expected_total_size": int(row.expected_total_size or 0),
            "project_id": row.project_id,
            "pair_id": row.pair_id,
            "failure_code": row.failure_code,
        }
        for index, row in enumerate(session_rows)
    ]


def _upload_session_payload(session_row):
    uploaded_bytes = int(session_row.current_offset or 0)
    total_bytes = int(session_row.expected_total_size or 0)
    remaining_bytes = max(0, total_bytes - uploaded_bytes)
    progress_percent = round((uploaded_bytes / total_bytes) * 100, 2) if total_bytes else 0
    latest_job = None
    pair_payload = None
    if session_row.project_id:
        latest_job = (
            ProcessingJob.query
            .filter_by(project_id=session_row.project_id)
            .order_by(desc(ProcessingJob.created_at), desc(ProcessingJob.id))
            .first()
        )
    if session_row.pair_id:
        pair = ProjectPair.query.get(session_row.pair_id)
        if pair:
            pair_payload = {
                "id": pair.id,
                "pair_index": pair.pair_index,
                "processing_status": pair.processing_status,
                "feature_extraction_status": pair.feature_extraction_status,
                "is_processed": bool(pair.is_processed),
                "safe_error": safe_error_summary(pair.processing_error) if pair.processing_error else None,
            }
    return {
        "id": session_row.id,
        "status": session_row.status,
        "purpose": session_row.purpose,
        "current_offset": session_row.current_offset,
        "expected_total_size": session_row.expected_total_size,
        # The server chunk ceiling, so the client's adaptive chunk sizer can
        # grow toward the real limit instead of hardcoding a copy of the
        # default that silently 413s the day the config changes.
        "max_chunk_bytes": int(
            app.config.get("RESUMABLE_UPLOAD_CHUNK_MAX_BYTES", RESUMABLE_UPLOAD_CHUNK_MAX_BYTES)
        ),
        "uploaded_bytes": uploaded_bytes,
        "remaining_bytes": remaining_bytes,
        "progress_percent": progress_percent,
        "image_size": session_row.image_size,
        "video_size": session_row.video_size,
        "experience_type": session_row.experience_type,
        "playback_mode": session_row.playback_mode,
        "project_id": session_row.project_id,
        "pair_id": session_row.pair_id,
        "pair": pair_payload,
        "processing_job": processing_job_status_payload(latest_job) if latest_job else None,
        "failure_code": session_row.failure_code,
        # Derived, not stored - see _upload_session_set_state().
        "set_state": _upload_session_set_state(session_row),
        "can_upload_chunks": session_row.status == "active" and uploaded_bytes < total_bytes,
        "can_finalize": (
            session_row.status == "assembled"
            or (session_row.status == "active" and uploaded_bytes == total_bytes and total_bytes > 0)
        ),
        "can_retry_finalize": session_row.status == "assembled",
        "can_cancel": session_row.status == "active",
        "is_terminal": session_row.status in {"completed", "cancelled", "expired", "failed"},
        "created_at": session_row.created_at.isoformat() if session_row.created_at else None,
        "updated_at": session_row.updated_at.isoformat() if session_row.updated_at else None,
        "expires_at": session_row.expires_at.isoformat() if session_row.expires_at else None,
        "completed_at": session_row.completed_at.isoformat() if session_row.completed_at else None,
    }


_UPLOAD_SESSION_CONFLICT_CODES = {
    "completed": "ALREADY_FINALIZED",
    "assembled": "SESSION_ASSEMBLED_RETRY",
    "finalizing": "FINALIZE_IN_PROGRESS",
    "cancelled": "SESSION_CANCELLED",
    "expired": "SESSION_EXPIRED",
    "failed": "SESSION_FAILED",
}


def _finalize_conflict_response(session_row):
    if session_row is None:
        return _upload_api_error("NOT_FOUND", "Upload session not found.", 404)
    if session_row.status == "active":
        return _upload_api_error(
            "INCOMPLETE_UPLOAD",
            f"Upload is incomplete ({session_row.current_offset}/{session_row.expected_total_size} bytes).",
            409,
            current_offset=session_row.current_offset,
            expected_total_size=session_row.expected_total_size,
        )
    code = _UPLOAD_SESSION_CONFLICT_CODES.get(session_row.status, "SESSION_NOT_ACTIVE")
    return _upload_api_error(code, f"Upload session is not finalizable (status={session_row.status}).", 409)


class _BoundedFileView:
    """Read-only file-like view over [start, start+length) of an on-disk
    file, so validate_image()/validate_video() (via upload_validation's
    save_to_temp, which calls .stream.seek(0) then .save()) can validate a
    resumable session's image/video slice without a redundant on-disk copy
    of up to MAX_VIDEO_SIZE bytes."""

    def __init__(self, path, start, length):
        self._length = length
        self._pos = 0
        self._fh = open(path, "rb")
        self._start = start
        self._fh.seek(start)

    def read(self, size=-1):
        remaining = self._length - self._pos
        if remaining <= 0:
            return b""
        if size is None or size < 0:
            size = remaining
        size = min(size, remaining)
        data = self._fh.read(size)
        self._pos += len(data)
        return data

    def seek(self, offset, whence=0):
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self._pos + offset
        elif whence == 2:
            target = self._length + offset
        else:
            raise ValueError("invalid whence")
        target = max(0, min(target, self._length))
        self._pos = target
        self._fh.seek(self._start + target)
        return self._pos

    def tell(self):
        return self._pos

    def close(self):
        self._fh.close()


def _finalize_enqueue_and_complete(session_rows):
    """Attempt the existing RQ enqueue EXACTLY ONCE for an
    already-assembled/validated project, via the very same
    _schedule_project_pair_processing() the non-resumable /upload route
    calls - never a second/parallel enqueue mechanism.

    `session_rows` is the ordered content-set list that produced the project:
    one row for a single-pair project, N rows for a multi-content-set one.
    The enqueue is per PROJECT, not per content set, exactly as
    handle_upload() does it for an N-pair multipart POST - so N content sets
    produce ONE job, and every row settles to the same terminal state
    together.

    Recovery semantics (documented, see Phase 3 finalize spec): if the
    enqueue call itself fails/throws, this endpoint must NOT report a
    successful finalization. Project/ProjectPair already exist and quota
    is already consumed at this point (matching the non-resumable path's
    own behavior when ITS enqueue attempt fails - it also keeps the
    already-created Project/Pair rows and just marks pairs failed rather
    than un-creating the project). The sessions are left in status
    'assembled' rather than 'completed'; calling finalize again on the same
    session id(s) retries ONLY this enqueue step (see the 'assembled'
    branch in finalize_upload_session / finalize_upload_project) - that is
    the operator/client recovery path, not a separate CLI.
    """
    session_rows = list(session_rows)
    primary = session_rows[0]

    def settle(status, failure_code):
        completed_at = get_utc_now() if status == "completed" else None
        for row in session_rows:
            row.status = status
            row.failure_code = failure_code
            if completed_at:
                row.completed_at = completed_at
        db.session.commit()

    project = Project.query.get(primary.project_id)
    if project and project.experience_type == "direct_qr":
        settle("completed", None)
        return True

    job = _schedule_project_pair_processing(primary.project_id)
    if job:
        settle("completed", None)
        return True
    settle("assembled", "QUEUE_ENQUEUE_FAILED")
    return False


@app.route("/api/uploads/sessions", methods=["POST"])
def create_upload_session():
    """1. Create upload session. Validates declared sizes against the
    existing max-size config BEFORE allocating anything (no DB row, no
    temp file) - see resumable-upload-api-contract.md."""
    request_start = time.perf_counter()
    user, admin = _upload_identity()
    if not user and not admin:
        return _upload_api_error("UNAUTHENTICATED", "Login required.", 401)

    payload = request.get_json(silent=True) or {}

    try:
        experience_type, playback_mode = _resolve_project_experience_playback(
            payload.get("experience_type"),
            payload.get("playback_mode"),
            user=user,  # None for an admin-owned upload: admins are not plan-gated.
        )
    except ValueError as exc:
        return _upload_api_error("INVALID_EXPERIENCE_PLAYBACK", str(exc), 400)

    try:
        image_size = int(payload.get("image_size"))
        video_size = int(payload.get("video_size"))
    except (TypeError, ValueError):
        return _upload_api_error("INVALID_SIZE", "image_size and video_size must be provided as integers.", 400)

    if video_size <= 0 or image_size < 0 or (experience_type == "image_video" and image_size <= 0):
        return _upload_api_error("INVALID_SIZE", "image_size and video_size must be positive for Image → Video; Direct QR may omit image bytes.", 400)
    if experience_type == "direct_qr" and image_size != 0:
        return _upload_api_error("INVALID_SIZE", "Direct QR resumable uploads must not include marker image bytes.", 400)
    # Plan policy, hard-capped by the server ceiling. Admin-owned sessions have
    # no plan, so they fall back to the ceiling exactly as before.
    _sess_ents = user_entitlements(user) if user else None
    _sess_max_image = _sess_ents["image_policy"]["max_bytes"] if _sess_ents else _ent.MAX_IMAGE_SIZE
    _sess_max_video = _sess_ents["video_policy"]["max_bytes"] if _sess_ents else _ent.MAX_VIDEO_SIZE
    if _sess_max_image is not None and image_size > _sess_max_image:
        return _upload_api_error("IMAGE_TOO_LARGE", "Declared image size exceeds the allowed limit.", 400)
    if _sess_max_video is not None and video_size > _sess_max_video:
        return _upload_api_error("VIDEO_TOO_LARGE", "Declared video size exceeds the allowed limit.", 400)

    expected_total_size = image_size + video_size
    max_content_length = app.config.get("MAX_CONTENT_LENGTH")
    if max_content_length and expected_total_size > max_content_length:
        return _upload_api_error("TOTAL_TOO_LARGE", "Declared total upload size exceeds the allowed limit.", 400)

    client_checksum = payload.get("client_checksum_sha256")
    if client_checksum is not None:
        client_checksum = str(client_checksum).strip().lower()
        if len(client_checksum) != 64 or any(c not in "0123456789abcdef" for c in client_checksum):
            return _upload_api_error(
                "INVALID_CHECKSUM", "client_checksum_sha256 must be a 64-character hex sha256 digest.", 400
            )

    if user:
        if user.is_blocked:
            return _upload_api_error("ACCOUNT_BLOCKED", "Account is blocked.", 403)
        dev_test_entitled = has_dev_test_entitlement(user)
        if not user.can_create_project and not dev_test_entitled:
            return _upload_api_error("PROJECT_LIMIT_REACHED", "Project limit reached. Please upgrade your plan.", 403)
        max_pairs = get_plan_pairs_limit(user)
        if max_pairs is None and not dev_test_entitled:
            return _upload_api_error(
                "PLAN_NOT_CONFIGURED",
                "Pairs allowed per project is not configured for your current plan. Please contact admin.",
                403,
            )
        ok, _redirect_url, message = check_user_limits(user)
        if not ok:
            return _upload_api_error("SUBSCRIPTION_LIMIT", message or "Subscription limit reached.", 403)

    # A content set of a multi-set project is marked as such at creation, so
    # the single-session finalize route can refuse it and it can only ever be
    # finalized together with its siblings.
    purpose = _sanitize_display_text(payload.get("purpose"), max_len=30) or "project_pair"
    if purpose not in UPLOAD_SESSION_PURPOSES:
        return _upload_api_error("INVALID_PURPOSE", "Unsupported upload session purpose.", 400)

    storage_token = str(uuid.uuid4())
    now = get_utc_now()
    session_row = UploadSession(
        owner_user_id=user.id if user else None,
        owner_admin_id=admin.id if admin else None,
        purpose=purpose,
        project_name=_sanitize_display_text(payload.get("project_name")) or "Untitled Project",
        original_image_name=_sanitize_display_text(payload.get("original_image_name")),
        original_video_name=_sanitize_display_text(payload.get("original_video_name")),
        image_content_type=_sanitize_display_text(payload.get("image_content_type"), max_len=100),
        video_content_type=_sanitize_display_text(payload.get("video_content_type"), max_len=100),
        experience_type=experience_type,
        playback_mode=playback_mode,
        image_size=image_size,
        video_size=video_size,
        expected_total_size=expected_total_size,
        current_offset=0,
        status="active",
        storage_token=storage_token,
        client_checksum_sha256=client_checksum,
        expires_at=now + timedelta(minutes=UPLOAD_SESSION_TTL_MINUTES),
    )
    db.session.add(session_row)
    db.session.commit()

    # Create the empty backing temp file up front so the chunk route can
    # always open-and-append without a first-chunk special case.
    open(_upload_session_temp_path(storage_token), "wb").close()

    _log_upload_timing(
        "upload_session_create",
        upload_session_id=session_row.id,
        owner_type="user" if user else "admin",
        pair_count=1,
        total_bytes=expected_total_size,
        image_bytes=image_size,
        video_bytes=video_size,
        request_duration_ms=_elapsed_ms(request_start),
        status=session_row.status,
    )
    response = jsonify({"success": True, "session": _upload_session_payload(session_row)})
    response.status_code = 201
    return response


@app.route("/api/uploads/sessions/<int:session_id>/chunk", methods=["POST"])
def upload_session_chunk(session_id):
    """2. Upload next sequential chunk. Client always appends at the
    session's current_offset (X-Chunk-Offset header + raw body). Resending
    an already-accepted chunk (same claimed offset, offset < current
    recorded offset) is a safe idempotent no-op - see
    resumable-upload-api-contract.md."""
    request_start = time.perf_counter()
    user, admin = _upload_identity()
    if not user and not admin:
        return _upload_api_error("UNAUTHENTICATED", "Login required.", 401)

    offset_header = request.headers.get("X-Chunk-Offset")
    try:
        claimed_offset = int(offset_header)
        if claimed_offset < 0:
            raise ValueError("negative offset")
    except (TypeError, ValueError):
        return _upload_api_error("INVALID_OFFSET", "X-Chunk-Offset header must be a non-negative integer.", 400)

    max_chunk_bytes = app.config.get("RESUMABLE_UPLOAD_CHUNK_MAX_BYTES", RESUMABLE_UPLOAD_CHUNK_MAX_BYTES)
    if request.content_length is not None and request.content_length > max_chunk_bytes:
        return _upload_api_error(
            "CHUNK_TOO_LARGE", "Chunk body exceeds the allowed size.", 413, max_chunk_bytes=max_chunk_bytes
        )

    body = request.get_data(cache=False)
    if len(body) > max_chunk_bytes:
        return _upload_api_error(
            "CHUNK_TOO_LARGE", "Chunk body exceeds the allowed size.", 413, max_chunk_bytes=max_chunk_bytes
        )
    if not body:
        return _upload_api_error("EMPTY_CHUNK", "Chunk body must not be empty.", 400)

    session_row = _lock_upload_session(session_id)
    if not session_row or not _upload_session_owned(session_row, user, admin):
        db.session.rollback()
        return _upload_api_error("NOT_FOUND", "Upload session not found.", 404)

    now = get_utc_now()
    if session_row.status == "active" and session_row.expires_at < now:
        session_row.status = "expired"
        session_row.failure_code = "SESSION_TTL_EXPIRED"
        db.session.commit()
        return _upload_api_error("SESSION_EXPIRED", "This upload session has expired.", 409)

    if session_row.status != "active":
        db.session.rollback()
        code = _UPLOAD_SESSION_CONFLICT_CODES.get(session_row.status, "SESSION_NOT_ACTIVE")
        return _upload_api_error(code, f"Upload session is not active (status={session_row.status}).", 409)

    temp_path = _upload_session_temp_path(session_row.storage_token)

    # Self-heal a prior crash between file-append and DB-commit: the file
    # is only ever allowed to be ahead of current_offset transiently
    # (append happens before the offset commit within this same lock).
    actual_size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
    if actual_size > session_row.current_offset:
        with open(temp_path, "r+b") as fh:
            fh.truncate(session_row.current_offset)
    elif actual_size < session_row.current_offset:
        db.session.rollback()
        app.logger.error(f"upload_session_file_behind_offset session_id={session_row.id}")
        return _upload_api_error(
            "STORAGE_INCONSISTENT", "Upload session storage is inconsistent. Please cancel and retry.", 500
        )

    if claimed_offset == session_row.current_offset:
        new_offset = session_row.current_offset + len(body)
        if new_offset > session_row.expected_total_size:
            resulting_offset = session_row.current_offset
            expected_total_size = session_row.expected_total_size
            db.session.rollback()
            return _upload_api_error(
                "CHUNK_EXCEEDS_EXPECTED_SIZE", "This chunk would exceed the declared upload size.", 400,
                current_offset=resulting_offset, expected_total_size=expected_total_size,
            )
        write_start = time.perf_counter()
        with open(temp_path, "ab") as fh:
            fh.write(body)
        write_duration_ms = _elapsed_ms(write_start)
        session_row.current_offset = os.path.getsize(temp_path)
        # Sliding expiry: expires_at is an INACTIVITY deadline, not a
        # wall-clock one measured from session creation. A creator who
        # pauses an upload overnight (weak mobile link, tab left open)
        # must still be able to resume the same session's confirmed bytes
        # rather than being told to start from zero. Genuinely abandoned
        # sessions are still reaped by cleanup-upload-sessions, which
        # keys off the (much shorter) updated_at staleness window.
        session_row.expires_at = now + timedelta(minutes=UPLOAD_SESSION_TTL_MINUTES)
        db.session.commit()
        _log_upload_timing(
            "upload_session_chunk",
            upload_session_id=session_row.id,
            chunk_size=len(body),
            claimed_offset=claimed_offset,
            resulting_offset=session_row.current_offset,
            request_duration_ms=_elapsed_ms(request_start),
            server_write_duration_ms=write_duration_ms,
            duplicate_chunk=False,
            offset_mismatch=False,
            status="accepted",
        )
        return jsonify({
            "success": True,
            "current_offset": session_row.current_offset,
            "expected_total_size": session_row.expected_total_size,
        })

    if claimed_offset < session_row.current_offset and claimed_offset + len(body) <= session_row.current_offset:
        resulting_offset = session_row.current_offset
        expected_total_size = session_row.expected_total_size
        db.session.rollback()
        _log_upload_timing(
            "upload_session_chunk",
            upload_session_id=session_row.id,
            chunk_size=len(body),
            claimed_offset=claimed_offset,
            resulting_offset=resulting_offset,
            request_duration_ms=_elapsed_ms(request_start),
            server_write_duration_ms=0,
            duplicate_chunk=True,
            offset_mismatch=False,
            status="duplicate",
        )
        return jsonify({
            "success": True,
            "current_offset": resulting_offset,
            "expected_total_size": expected_total_size,
            "note": "duplicate_chunk_ignored",
        })

    # Everything else - a gap/future offset, or a PARTIALLY overlapping
    # replay whose tail would extend past current_offset - is rejected
    # rather than spliced. Accepting a partial overlap would mean trusting
    # that the overlapping prefix is byte-identical to what is already on
    # disk, which nothing in this protocol proves. Rejecting keeps the
    # "every accepted byte stays accepted" invariant, and the authoritative
    # offset travels back in the SAME response so the client re-slices in
    # one round-trip instead of two.
    resulting_offset = session_row.current_offset
    expected_total_size = session_row.expected_total_size
    db.session.rollback()
    _log_upload_timing(
        "upload_session_chunk",
        upload_session_id=session_id,
        chunk_size=len(body),
        claimed_offset=claimed_offset,
        resulting_offset=resulting_offset,
        request_duration_ms=_elapsed_ms(request_start),
        server_write_duration_ms=0,
        duplicate_chunk=False,
        offset_mismatch=True,
        status="offset_mismatch",
        safe_error_code="OFFSET_MISMATCH",
    )
    return _upload_api_error(
        "OFFSET_MISMATCH",
        f"Chunk offset does not match the session's current offset ({resulting_offset}).",
        409,
        current_offset=resulting_offset,
        expected_total_size=expected_total_size,
    )


@app.route("/api/uploads/sessions/<int:session_id>", methods=["GET"])
def upload_session_status(session_id):
    """3. Query session status/offset. Never returns a raw filesystem path."""
    user, admin = _upload_identity()
    if not user and not admin:
        return _upload_api_error("UNAUTHENTICATED", "Login required.", 401)
    session_row = UploadSession.query.get(session_id)
    if not session_row or not _upload_session_owned(session_row, user, admin):
        return _upload_api_error("NOT_FOUND", "Upload session not found.", 404)
    response = jsonify({"success": True, "session": _upload_session_payload(session_row)})
    response.headers["Cache-Control"] = "no-store"
    return response


def _finalize_assemble_and_validate(session_rows, user, admin):
    """The validate -> quota -> Project/ProjectPair -> QR -> enqueue
    sequence, run exactly once per winning atomic 'active'->'finalizing'
    transition. Mirrors handle_upload()/admin_handle_upload() step for
    step: same validate_image()/validate_video() calls, same
    _reserve_project_quota_atomic() authoritative point (skipped entirely
    for admin owners, exactly like admin_handle_upload), same
    os.replace() atomic-move convention, same QR helpers, same
    _schedule_project_pair_processing() enqueue call.

    `session_rows` is the ORDERED content-set list: one row for a
    single-pair project, N rows for a multi-content-set one. The length
    changes none of the invariants - one Project, one project-quota unit,
    N ProjectPairs at pair_index 0..N-1 in exactly this order, one QR
    image, one processing job. That is precisely what handle_upload()
    already does for an N-pair multipart POST, which is why multi-pair was
    converged onto this one function instead of growing a parallel
    multi-pair finalizer that would have to be kept in step with it.
    """
    finalize_start = time.perf_counter()
    session_rows = list(session_rows)
    primary = session_rows[0]
    multi = len(session_rows) > 1
    checksum_duration_ms = 0
    validation_duration_ms = 0
    project_create_duration_ms = 0
    qr_duration_ms = 0
    enqueue_duration_ms = 0
    combined_paths = {row.id: _upload_session_temp_path(row.storage_token) for row in session_rows}
    declared_total_bytes = sum(int(row.expected_total_size or 0) for row in session_rows)

    def _timing(**extra):
        fields = {
            "upload_session_id": primary.id,
            "project_id": primary.project_id,
            "pair_count": len(session_rows),
            "set_count": len(session_rows),
            "total_bytes": declared_total_bytes,
            "checksum_duration_ms": checksum_duration_ms,
            "validation_duration_ms": validation_duration_ms,
            "project_create_duration_ms": project_create_duration_ms,
            "qr_duration_ms": qr_duration_ms,
            "enqueue_duration_ms": enqueue_duration_ms,
            "finalize_duration_ms": _elapsed_ms(finalize_start),
            "recovered_existing_completion": False,
            "status": primary.status,
        }
        fields.update(extra)
        _log_upload_timing("upload_session_finalize", **fields)

    def fail(code, message, http_status=422, offender=None):
        """Park the offending content set - and, when it has siblings, hand
        every sibling back its 'active' state with its confirmed bytes
        untouched.

        FAILURE ISOLATION. A later content set failing validation must never
        cost a creator the bytes an earlier one already got across, so only
        the offender's own assembled temp file is discarded and only the
        offender becomes 'failed'. The creator re-selects one file, not
        three. Single-set behaviour is byte-for-byte what it was: there is
        no sibling to preserve, so the session is simply marked failed.
        """
        offender = offender or primary
        offender_index = next(i for i, row in enumerate(session_rows) if row.id == offender.id)
        _safe_delete_upload_temp(combined_paths[offender.id])
        for row in session_rows:
            if row.id == offender.id:
                row.status = "failed"
                row.failure_code = code
            elif row.status == "finalizing":
                row.status = "active"
        db.session.commit()
        _timing(status=offender.status, safe_error_code=code, set_index=offender_index)
        extra = {"failed_session_id": offender.id, "failed_set_index": offender_index} if multi else {}
        return _upload_api_error(code, message, http_status, **extra)

    def fail_group(code, message, http_status):
        """Terminal for every content set. Used only for failures that
        happen AFTER the assembled temp files have been consumed by
        validation (quota, storage allowance, project creation) - at that
        point there are no confirmed bytes left to preserve for anyone, so
        pretending a set is still resumable would be a lie. Same behaviour
        the single-pair path has always had, now applied uniformly.
        """
        for row in session_rows:
            row.status = "failed"
            row.failure_code = code
        db.session.commit()
        _timing(status="failed", safe_error_code=code)
        return _upload_api_error(code, message, http_status)

    # ---- 1. Every set's assembled bytes must be present and the declared length.
    for row in session_rows:
        path = combined_paths[row.id]
        if not os.path.exists(path) or os.path.getsize(path) != row.expected_total_size:
            return fail("STORAGE_INCONSISTENT", "Uploaded data is inconsistent with the declared size.", 500, offender=row)
        if row.client_checksum_sha256:
            checksum_start = time.perf_counter()
            digest = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            computed = digest.hexdigest()
            row.computed_checksum_sha256 = computed
            checksum_duration_ms += _elapsed_ms(checksum_start)
            if computed != row.client_checksum_sha256:
                return fail("CHECKSUM_MISMATCH", "Uploaded data failed checksum verification.", offender=row)

    # Experience/playback is a PROJECT fact, so it is read from the first set
    # only; the route that got here already refused a group whose sets
    # disagreed about it.
    experience_type = primary.experience_type or "image_video"
    playback_mode = primary.playback_mode or _default_playback_mode_for_experience(experience_type)
    try:
        _validate_project_experience_playback(experience_type, playback_mode)
    except ValueError as exc:
        return fail("INVALID_EXPERIENCE_PLAYBACK", str(exc), 400)

    # ---- 2. Validate every set's image/video slice from its own assembled file.
    validation_start = time.perf_counter()
    # Same plan-effective policy as the direct upload path; admin-owned
    # sessions have no plan and fall back to the server ceiling.
    _fin_ents = user_entitlements(user) if user else None
    _img_max, _img_dim, _img_px = (
        image_limits(_fin_ents) if _fin_ents else (_ent.MAX_IMAGE_SIZE, _ent.MAX_IMAGE_DIMENSION_PX, _ent.MAX_IMAGE_PIXELS)
    )
    _vid_max, _vid_dur = video_limits(_fin_ents) if _fin_ents else (_ent.MAX_VIDEO_SIZE, _ent.MAX_VIDEO_DURATION_SECONDS)
    validated = []
    failed_row = failed_code = failed_message = None
    for row in session_rows:
        path = combined_paths[row.id]
        image_view = _BoundedFileView(path, 0, row.image_size) if experience_type == "image_video" else None
        video_view = _BoundedFileView(path, row.image_size, row.video_size)
        img_temp = vid_temp = img_ext = vid_ext = None
        image_error = video_error = None
        try:
            if image_view is not None:
                image_storage = FileStorage(
                    stream=image_view,
                    filename=row.original_image_name or "upload.jpg",
                    content_type=row.image_content_type or "application/octet-stream",
                )
                try:
                    img_temp, img_ext = validate_image(
                        image_storage, TMP_UPLOADS_DIR, _img_max, _img_dim, _img_px
                    )
                except UploadValidationError as exc:
                    image_error = exc

            if image_error is None:
                video_storage = FileStorage(
                    stream=video_view,
                    filename=row.original_video_name or "upload.mp4",
                    content_type=row.video_content_type or "application/octet-stream",
                )
                try:
                    vid_temp, vid_ext = validate_video(
                        video_storage, TMP_UPLOADS_DIR, _vid_max, _vid_dur
                    )
                except UploadValidationError as exc:
                    video_error = exc
        finally:
            # Close both view handles BEFORE any attempt to delete this set's
            # assembled file below (via `fail()`) - on Windows, a file with an
            # open handle cannot be deleted, so doing this any later would
            # silently no-op the cleanup on failure.
            if image_view:
                image_view.close()
            video_view.close()

        if image_error is not None:
            app.logger.warning(f"resumable_upload_rejected image session_id={row.id}: {image_error.detail}")
            failed_row, failed_code, failed_message = row, "IMAGE_VALIDATION_FAILED", image_error.safe_message
            break
        if video_error is not None:
            _safe_remove(img_temp)
            app.logger.warning(f"resumable_upload_rejected video session_id={row.id}: {video_error.detail}")
            failed_row, failed_code, failed_message = row, "VIDEO_VALIDATION_FAILED", video_error.safe_message
            break
        validated.append({
            "session": row, "image_temp": img_temp, "image_ext": img_ext,
            "video_temp": vid_temp, "video_ext": vid_ext,
        })
    validation_duration_ms = _elapsed_ms(validation_start)

    if failed_row is not None:
        # The copies made for the sets that DID validate are throwaway; their
        # authoritative bytes are still in their own assembled temp files,
        # which fail() deliberately leaves alone.
        for item in validated:
            _safe_remove(item["image_temp"])
            _safe_remove(item["video_temp"])
        return fail(failed_code, failed_message, offender=failed_row)

    # Every set validated and was copied out by validate_image/validate_video's
    # own save_to_temp - the assembled temp files are no longer needed.
    for row in session_rows:
        _safe_delete_upload_temp(combined_paths[row.id])

    # ---- 3. One project, N pairs, one quota unit, in one transaction.
    is_admin_owner = admin is not None
    saved_paths = []
    project_create_start = time.perf_counter()
    retained = [
        (
            os.path.getsize(item["image_temp"]) if item["image_temp"] else None,
            os.path.getsize(item["video_temp"]),
        )
        for item in validated
    ]
    total_new_storage_bytes = sum((image_bytes or 0) + video_bytes for image_bytes, video_bytes in retained)
    _storage_used, storage_allowance = account_storage_state(user) if user else (0, None)

    try:
        if not is_admin_owner:
            # Same authoritative atomic reservation as handle_upload(); a
            # rollback in either except block below releases it. Admin-owned
            # sessions bill no account and are recorded uncounted. The ENTIRE
            # retained set is weighed at once, exactly like the multipart
            # path - never set 1 persisted and set 2 rejected.
            if not _storage.reserve_account_storage(user.id, total_new_storage_bytes, storage_allowance):
                raise _ResumableQuotaLimitReached("STORAGE_LIMIT_REACHED", STORAGE_LIMIT_MESSAGE)
            if not _reserve_project_quota_atomic(user):
                raise _ResumableQuotaLimitReached()

        images_dir = ADMIN_IMAGES_DIR if is_admin_owner else IMAGES_DIR
        videos_dir = ADMIN_VIDEOS_DIR if is_admin_owner else VIDEOS_DIR

        if is_admin_owner:
            max_index = db.session.query(func.max(Project.user_project_index)).filter(
                Project.owner_admin_id == admin.id
            ).scalar()
            project_index = (int(max_index) if max_index and int(max_index) > 0 else 0) + 1
            project = Project(
                name=primary.project_name or "Untitled Project",
                owner_admin_id=admin.id,
                owner_user_id=None,
                user_project_index=project_index,
                experience_type=experience_type,
                playback_mode=playback_mode,
            )
        else:
            max_index = db.session.query(func.max(Project.user_project_index)).filter(
                Project.owner_user_id == user.id
            ).scalar()
            project_index = (int(max_index) if max_index and int(max_index) > 0 else 0) + 1
            project = Project(
                name=primary.project_name or "Untitled Project",
                owner_user_id=user.id,
                owner_admin_id=None,
                created_by_user_id=user.id,
                current_owner_user_id=user.id,
                user_project_index=project_index,
                experience_type=experience_type,
                playback_mode=playback_mode,
            )
        db.session.add(project)
        db.session.flush()
        if is_admin_owner:
            add_project_service_coverage(
                project,
                "ADMIN_GRANT",
                created_by_admin=admin,
                reason="Admin-created project public service coverage.",
            )

        if not is_admin_owner:
            max_pairs = get_plan_pairs_limit(user)
            pair_slots_ok, pair_slots_error = _reserve_pair_slots_for_project(project.id, len(validated), max_pairs)
            if not pair_slots_ok:
                raise ValueError(pair_slots_error)

        created_pairs = []
        for index, item in enumerate(validated):
            img_filename = f"{project.id}_{index}.jpg" if experience_type == "image_video" else None
            vid_filename = f"{project.id}_{index}{item['video_ext']}"
            img_path = None
            if img_filename:
                img_path = os.path.join(images_dir, img_filename)
                os.replace(item["image_temp"], img_path)  # atomic move: already-validated content only
                saved_paths.append(img_path)
            vid_path = os.path.join(videos_dir, vid_filename)
            os.replace(item["video_temp"], vid_path)  # atomic move: already-validated content only
            saved_paths.append(vid_path)

            image_path_url = None
            if img_filename:
                image_path_url = f"/admin/image/{project.id}/{index}" if is_admin_owner else f"/image/{project.id}/{index}"
            pair = ProjectPair(
                project_id=project.id,
                pair_index=index,
                image_filename=img_filename,
                video_filename=vid_filename,
                image_path=image_path_url,
                original_image_name=item["session"].original_image_name,
                original_video_name=item["session"].original_video_name,
                image_size=os.path.getsize(img_path) if img_path else None,
                video_size=os.path.getsize(vid_path),
                is_processed=experience_type == "direct_qr",
                processing_status="completed" if experience_type == "direct_qr" else "uploaded",
                feature_extraction_status="not_required" if experience_type == "direct_qr" else "pending",
                processing_error=None,
            )
            db.session.add(pair)
            created_pairs.append((item["session"], pair, img_path, vid_path))

        # Ledger rows for the retained media, in the SAME transaction as the
        # reservation and the pair rows, so accounting can never be
        # half-applied across content sets.
        db.session.flush()
        for row, pair, img_path, vid_path in created_pairs:
            record_pair_media_objects(
                project, pair,
                image_bytes=os.path.getsize(img_path) if img_path else None,
                video_bytes=os.path.getsize(vid_path),
            )
            row.project_id = project.id
            row.pair_id = pair.id

        db.session.commit()
        project_create_duration_ms = _elapsed_ms(project_create_start)
    except _ResumableQuotaLimitReached as limit_exc:
        db.session.rollback()
        for item in validated:
            _safe_remove(item["image_temp"])
            _safe_remove(item["video_temp"])
        project_create_duration_ms = _elapsed_ms(project_create_start)
        return fail_group(limit_exc.code, limit_exc.message, 403)
    except Exception as exc:
        db.session.rollback()
        for saved_path in saved_paths:
            try:
                if saved_path and os.path.exists(saved_path):
                    os.remove(saved_path)
            except Exception:
                pass
        for item in validated:
            _safe_remove(item["image_temp"])
            _safe_remove(item["video_temp"])
        project_create_duration_ms = _elapsed_ms(project_create_start)
        app.logger.error(f"resumable_upload_project_creation_failed session_id={primary.id}: {exc}")
        return fail_group("PROJECT_CREATION_FAILED", "Project creation failed. Please try again.", 500)

    # QR code generation - same helpers/convention the non-resumable path
    # uses, unmodified. One QR per project, never one per content set.
    qr_start = time.perf_counter()
    if is_admin_owner:
        admin_name = admin.name or admin.email.split("@")[0]
        scanner_url = url_for(
            "scanner", project_id=project.id, admin_id=admin.id, admin_name=admin_name,
            _external=True, _scheme="https",
        )
        qr_filename = f"project_{project.id}_admin.png"
        qr_dir = ADMIN_QR_DIR
    else:
        user_name = (user.first_name or user.email.split("@")[0]).strip()
        public_host = get_system_config('public_host')
        if public_host:
            base = public_host.rstrip('/')
            scanner_path = url_for("scanner", project_id=project.id, user_id=user.id, user_name=user_name)
            scanner_url = f"{base}{scanner_path}"
        else:
            scanner_url = url_for(
                "scanner", project_id=project.id, user_id=user.id, user_name=user_name,
                _external=True, _scheme="https",
            )
        qr_filename = f"project_{project.id}_main.png"
        qr_dir = QR_DIR

    qr_path = os.path.join(qr_dir, qr_filename)
    ok = generate_custom_qr(scanner_url, qr_path, project_name=project.name)
    if not ok or not os.path.exists(qr_path):
        generate_basic_qr(scanner_url, "black", "white", qr_path, project_name=project.name)

    project.scanner_url = scanner_url
    project.qr_code_filename = qr_filename
    project.qr_code_path = (f"/admin/qr/{qr_filename}" if is_admin_owner else f"/qr/{qr_filename}")
    db.session.commit()
    qr_duration_ms = _elapsed_ms(qr_start)

    enqueue_start = time.perf_counter()
    if _finalize_enqueue_and_complete(session_rows):
        enqueue_duration_ms = _elapsed_ms(enqueue_start)
        _timing(status=primary.status)
        return jsonify({"success": True, "session": _upload_session_payload(primary), "sessions": _upload_session_group_payload(session_rows)})
    enqueue_duration_ms = _elapsed_ms(enqueue_start)
    _timing(status=primary.status, safe_error_code="QUEUE_ENQUEUE_FAILED")
    return _upload_api_error(
        "QUEUE_ENQUEUE_FAILED",
        "Upload was assembled and validated but could not be queued for processing. Retry finalize to try again.",
        502,
    )


@app.route("/api/uploads/sessions/<int:session_id>/finalize", methods=["POST"])
def finalize_upload_session(session_id):
    """4. Finalize upload. Atomic conditional status transition guards
    against double finalization (mirrors the payment-activation /
    quota-reservation atomic-UPDATE pattern elsewhere in this codebase) -
    see _finalize_conflict_response for every rejection code."""
    finalize_start = time.perf_counter()
    user, admin = _upload_identity()
    if not user and not admin:
        return _upload_api_error("UNAUTHENTICATED", "Login required.", 401)

    session_row = UploadSession.query.get(session_id)
    if not session_row or not _upload_session_owned(session_row, user, admin):
        return _upload_api_error("NOT_FOUND", "Upload session not found.", 404)

    if session_row.purpose == "project_content_set":
        # Finalizing one content set of a multi-set project on its own would
        # produce a stray single-pair project, consume a project-quota unit for
        # it, and leave the siblings orphaned. The group route is the only way
        # in for these, and it is atomic across all of them.
        return _upload_api_error(
            "GROUP_FINALIZE_REQUIRED",
            "This content set belongs to a multi-content-set project and must be finalized with it.",
            409,
        )

    if session_row.status == "assembled":
        enqueue_start = time.perf_counter()
        updated = UploadSession.query.filter(
            UploadSession.id == session_row.id, UploadSession.status == "assembled"
        ).update({UploadSession.status: "finalizing"}, synchronize_session=False)
        if updated != 1:
            db.session.rollback()
            return _finalize_conflict_response(UploadSession.query.get(session_row.id))
        db.session.commit()
        session_row = UploadSession.query.get(session_row.id)
        if _finalize_enqueue_and_complete([session_row]):
            _log_upload_timing(
                "upload_session_finalize",
                upload_session_id=session_row.id,
                project_id=session_row.project_id,
                pair_count=1,
                total_bytes=session_row.expected_total_size,
                enqueue_duration_ms=_elapsed_ms(enqueue_start),
                finalize_duration_ms=_elapsed_ms(finalize_start),
                recovered_existing_completion=True,
                status=session_row.status,
            )
            return jsonify({"success": True, "session": _upload_session_payload(session_row)})
        _log_upload_timing(
            "upload_session_finalize",
            upload_session_id=session_row.id,
            project_id=session_row.project_id,
            pair_count=1,
            total_bytes=session_row.expected_total_size,
            enqueue_duration_ms=_elapsed_ms(enqueue_start),
            finalize_duration_ms=_elapsed_ms(finalize_start),
            recovered_existing_completion=True,
            status=session_row.status,
            safe_error_code="QUEUE_ENQUEUE_FAILED",
        )
        return _upload_api_error(
            "QUEUE_ENQUEUE_FAILED",
            "Upload was assembled and validated but could not be queued for processing. Retry finalize to try again.",
            502,
        )

    now = get_utc_now()
    if session_row.status == "active" and session_row.expires_at < now:
        session_row.status = "expired"
        session_row.failure_code = "SESSION_TTL_EXPIRED"
        db.session.commit()
        return _upload_api_error("SESSION_EXPIRED", "This upload session has expired.", 409)

    updated = UploadSession.query.filter(
        UploadSession.id == session_row.id,
        UploadSession.status == "active",
        UploadSession.current_offset == UploadSession.expected_total_size,
    ).update({UploadSession.status: "finalizing"}, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        current = UploadSession.query.get(session_row.id)
        if current and current.status == "completed":
            _log_upload_timing(
                "upload_session_finalize",
                upload_session_id=current.id,
                project_id=current.project_id,
                pair_count=1,
                total_bytes=current.expected_total_size,
                finalize_duration_ms=_elapsed_ms(finalize_start),
                recovered_existing_completion=True,
                status=current.status,
                safe_error_code="ALREADY_FINALIZED",
            )
        return _finalize_conflict_response(current)
    db.session.commit()
    session_row = UploadSession.query.get(session_row.id)

    return _finalize_assemble_and_validate([session_row], user, admin)


# Widest a single project-finalize request may declare. This is a
# REQUEST-SHAPE guard at the trust boundary, not a product limit - the real
# per-project ceiling is the plan's own get_plan_pairs_limit(), enforced
# below and again inside _reserve_pair_slots_for_project().
_UPLOAD_PROJECT_MAX_CONTENT_SETS = 100


def _parse_content_set_ids(payload):
    """(ids, error_response). Order is meaningful - it becomes pair_index
    0..N-1 - so it is preserved exactly as sent, and duplicates are rejected
    rather than silently collapsed: a repeated id would otherwise ask for the
    same bytes to become two pairs."""
    raw = payload.get("session_ids")
    if not isinstance(raw, list) or not raw:
        return None, _upload_api_error("INVALID_SESSION_IDS", "session_ids must be a non-empty list of upload session ids.", 400)
    if len(raw) > _UPLOAD_PROJECT_MAX_CONTENT_SETS:
        return None, _upload_api_error("TOO_MANY_CONTENT_SETS", "Too many content sets in one project.", 400)
    ids = []
    for value in raw:
        if isinstance(value, bool):
            return None, _upload_api_error("INVALID_SESSION_IDS", "session_ids must be a non-empty list of upload session ids.", 400)
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            return None, _upload_api_error("INVALID_SESSION_IDS", "session_ids must be a non-empty list of upload session ids.", 400)
    if len(set(ids)) != len(ids):
        return None, _upload_api_error("DUPLICATE_SESSION_IDS", "A content set may only appear once in a project.", 400)
    return ids, None


@app.route("/api/uploads/projects/finalize", methods=["POST"])
def finalize_upload_project():
    """4b. Finalize a MULTI-CONTENT-SET project: N upload sessions in, one
    Project out.

    The "group" is defined by THIS REQUEST, not by a new parent row and not by
    a new column - which is why this whole feature needed no migration. Every
    guarantee comes from the same atomic conditional UPDATE the single-pair
    route already uses, widened to N rows: the claim
    `WHERE id IN (...) AND status='active' AND current_offset=expected_total_size`
    must move EXACTLY N rows or nobody finalizes and nothing is created. That
    one statement is what makes a double-clicked Create, a request retried
    after a lost response, and two racing tabs all resolve to a single
    project.

    A single-set project may use this route too; it just delegates to the
    same _finalize_assemble_and_validate() the /sessions/<id>/finalize route
    does, with a one-element list.
    """
    finalize_start = time.perf_counter()
    user, admin = _upload_identity()
    if not user and not admin:
        return _upload_api_error("UNAUTHENTICATED", "Login required.", 401)

    payload = request.get_json(silent=True) or {}
    ids, error = _parse_content_set_ids(payload)
    if error is not None:
        return error

    rows_by_id = {
        row.id: row
        for row in UploadSession.query.filter(UploadSession.id.in_(ids)).all()
        if _upload_session_owned(row, user, admin)
    }
    if len(rows_by_id) != len(ids):
        # Same answer as a single unknown/foreign session: a 404 that tells a
        # caller nothing about whether the id exists on another account.
        return _upload_api_error("NOT_FOUND", "Upload session not found.", 404)
    session_rows = [rows_by_id[i] for i in ids]
    primary = session_rows[0]

    # Every set must belong to the same project intent. Disagreement here means
    # two different creation flows got mixed up client-side, and guessing which
    # one the creator meant is not something a server may do.
    for row in session_rows[1:]:
        if (
            (row.project_name or "") != (primary.project_name or "")
            or row.experience_type != primary.experience_type
            or row.playback_mode != primary.playback_mode
            or row.purpose != primary.purpose
        ):
            return _upload_api_error(
                "CONTENT_SET_MISMATCH",
                "These content sets do not belong to the same project.",
                409,
                sessions=_upload_session_group_payload(session_rows),
            )

    statuses = {row.status for row in session_rows}
    project_ids = {row.project_id for row in session_rows}

    # REPLAY: this exact group already produced a project. Report that project
    # instead of creating a second one. This is the browser-resent-after-a-lost-
    # response case and the double-clicked-Create case, and it must be a
    # success, not an error, or the client will think it has to retry.
    if statuses == {"completed"} and len(project_ids) == 1 and primary.project_id:
        _log_upload_timing(
            "upload_project_finalize",
            upload_session_id=primary.id,
            project_id=primary.project_id,
            pair_count=len(session_rows),
            set_count=len(session_rows),
            finalize_duration_ms=_elapsed_ms(finalize_start),
            recovered_existing_completion=True,
            status="completed",
            safe_error_code="ALREADY_FINALIZED",
        )
        return jsonify({
            "success": True,
            "session": _upload_session_payload(primary),
            "sessions": _upload_session_group_payload(session_rows),
            "recovered_existing_completion": True,
        })

    # RETRY-ENQUEUE-ONLY: the project, its pairs, its media rows and its quota
    # all already exist; only the queue handoff failed. Re-running validation
    # or quota here would duplicate both.
    if statuses == {"assembled"} and len(project_ids) == 1 and primary.project_id:
        enqueue_start = time.perf_counter()
        claimed = UploadSession.query.filter(
            UploadSession.id.in_(ids), UploadSession.status == "assembled"
        ).update({UploadSession.status: "finalizing"}, synchronize_session=False)
        if claimed != len(ids):
            db.session.rollback()
            fresh = [UploadSession.query.get(i) for i in ids]
            return _upload_api_error(
                "FINALIZE_IN_PROGRESS", "This project is already being finalized.", 409,
                sessions=_upload_session_group_payload([row for row in fresh if row]),
            )
        db.session.commit()
        session_rows = [UploadSession.query.get(i) for i in ids]
        ok = _finalize_enqueue_and_complete(session_rows)
        _log_upload_timing(
            "upload_project_finalize",
            upload_session_id=session_rows[0].id,
            project_id=session_rows[0].project_id,
            pair_count=len(session_rows),
            set_count=len(session_rows),
            enqueue_duration_ms=_elapsed_ms(enqueue_start),
            finalize_duration_ms=_elapsed_ms(finalize_start),
            recovered_existing_completion=True,
            status=session_rows[0].status,
            safe_error_code=None if ok else "QUEUE_ENQUEUE_FAILED",
        )
        if ok:
            return jsonify({
                "success": True,
                "session": _upload_session_payload(session_rows[0]),
                "sessions": _upload_session_group_payload(session_rows),
            })
        return _upload_api_error(
            "QUEUE_ENQUEUE_FAILED",
            "Upload was assembled and validated but could not be queued for processing. Retry finalize to try again.",
            502,
        )

    # Expire anything genuinely past its inactivity deadline before judging the
    # group, so the client is told SESSION_EXPIRED for that set rather than an
    # opaque "incomplete".
    now = get_utc_now()
    expired_any = False
    for row in session_rows:
        if row.status == "active" and row.expires_at < now:
            row.status = "expired"
            row.failure_code = "SESSION_TTL_EXPIRED"
            expired_any = True
    if expired_any:
        db.session.commit()
        return _upload_api_error(
            "SESSION_EXPIRED", "One or more content sets in this project expired.", 409,
            sessions=_upload_session_group_payload(session_rows),
        )

    # Some sets are already past the claim gate. Either another request is
    # finalizing this group right now, or a previous attempt settled part of it.
    # Either way the answer is "re-read the state", never "start again" - and
    # the per-set payload is what lets the client see which sets are already
    # done instead of guessing.
    settled = [row for row in session_rows if row.status in {"finalizing", "assembled", "completed"}]
    if settled:
        return _upload_api_error(
            "FINALIZE_IN_PROGRESS", "This project is already being finalized.", 409,
            sessions=_upload_session_group_payload(session_rows),
        )

    # Some sets are already past the claim gate. Either another request is
    # finalizing this group right now, or a previous attempt settled part of it.
    # Either way the answer is "re-read the state", never "start again" - and
    # the per-set payload is what lets the client see which sets are already
    # done instead of guessing.
    settled = [row for row in session_rows if row.status in {"finalizing", "assembled", "completed"}]
    if settled:
        return _upload_api_error(
            "FINALIZE_IN_PROGRESS", "This project is already being finalized.", 409,
            sessions=_upload_session_group_payload(session_rows),
        )

    # Not every set is byte-complete yet. The response carries every set's
    # authoritative offset so the client resumes exactly the sets that need it
    # and re-sends nothing for the ones that are already done.
    incomplete = [
        row for row in session_rows
        if row.status != "active" or int(row.current_offset or 0) != int(row.expected_total_size or 0)
    ]
    if incomplete:
        return _upload_api_error(
            "INCOMPLETE_UPLOAD",
            f"{len(incomplete)} of {len(session_rows)} content sets are not fully uploaded yet.",
            409,
            sessions=_upload_session_group_payload(session_rows),
        )

    if user:
        max_pairs = get_plan_pairs_limit(user)
        if max_pairs is not None and len(session_rows) > int(max_pairs):
            # Refused BEFORE the claim, so a plan-limited request leaves every
            # set 'active' and every uploaded byte exactly where it was.
            return _upload_api_error(
                "PAIR_LIMIT_REACHED",
                f"Your current plan allows maximum {max_pairs} pairs per project.",
                403,
                sessions=_upload_session_group_payload(session_rows),
            )

    # THE atomic gate. All N or none.
    claimed = UploadSession.query.filter(
        UploadSession.id.in_(ids),
        UploadSession.status == "active",
        UploadSession.current_offset == UploadSession.expected_total_size,
    ).update({UploadSession.status: "finalizing"}, synchronize_session=False)
    if claimed != len(ids):
        db.session.rollback()
        fresh = [row for row in (UploadSession.query.get(i) for i in ids) if row]
        if fresh and all(row.status == "completed" for row in fresh) and fresh[0].project_id:
            return jsonify({
                "success": True,
                "session": _upload_session_payload(fresh[0]),
                "sessions": _upload_session_group_payload(fresh),
                "recovered_existing_completion": True,
            })
        return _upload_api_error(
            "FINALIZE_IN_PROGRESS", "This project is already being finalized.", 409,
            sessions=_upload_session_group_payload(fresh),
        )
    db.session.commit()
    session_rows = [UploadSession.query.get(i) for i in ids]

    return _finalize_assemble_and_validate(session_rows, user, admin)


@app.route("/api/uploads/sessions/<int:session_id>/cancel", methods=["POST"])
def cancel_upload_session(session_id):
    """5. Cancel upload. Only valid from 'active' (documented choice: once
    a session reaches 'assembled'/'finalizing' it has already consumed
    quota and created a Project/Pair - the recovery path for those is
    retrying finalize, not cancel). No quota release: quota is never
    consumed for a non-finalized session."""
    user, admin = _upload_identity()
    if not user and not admin:
        return _upload_api_error("UNAUTHENTICATED", "Login required.", 401)

    session_row = UploadSession.query.get(session_id)
    if not session_row or not _upload_session_owned(session_row, user, admin):
        return _upload_api_error("NOT_FOUND", "Upload session not found.", 404)

    updated = UploadSession.query.filter(
        UploadSession.id == session_row.id, UploadSession.status == "active"
    ).update({UploadSession.status: "cancelled"}, synchronize_session=False)
    if updated != 1:
        db.session.rollback()
        fresh = UploadSession.query.get(session_row.id)
        code = _UPLOAD_SESSION_CONFLICT_CODES.get(fresh.status if fresh else None, "SESSION_NOT_ACTIVE")
        return _upload_api_error(code, "Upload session cannot be cancelled from its current state.", 409)

    db.session.commit()
    _safe_delete_upload_temp(_upload_session_temp_path(session_row.storage_token))
    return jsonify({"success": True, "session": _upload_session_payload(UploadSession.query.get(session_row.id))})


@app.cli.command("cleanup-upload-sessions")
@click.option("--apply", "apply_changes", is_flag=True, help="Persist expirations and delete temp files. Default is dry-run.")
@click.option(
    "--limit", default=UPLOAD_SESSION_CLEANUP_BATCH_LIMIT, show_default=True, type=int,
    help="Max sessions processed per invocation (bounded batch).",
)
def cleanup_upload_sessions(apply_changes, limit):
    """Expire stale resumable UploadSession rows (TTL passed, or 'active'
    but no chunk activity beyond the abandoned-staleness window) and clean
    up their temp files. Only ever queries status='active' rows - NEVER
    touches 'completed' sessions, blind or otherwise (see models.py's
    UploadSession lifecycle docstring). Dry-run by default; --apply
    persists. Bounded via --limit so this never runs an unbounded query."""
    now = get_utc_now()
    abandoned_cutoff = now - timedelta(minutes=UPLOAD_SESSION_ABANDONED_STALE_MINUTES)
    candidates = UploadSession.query.filter(
        UploadSession.status == "active",
        or_(UploadSession.expires_at < now, UploadSession.updated_at < abandoned_cutoff),
    ).order_by(UploadSession.id.asc()).limit(limit).all()

    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"Candidates found (bounded to limit={limit}): {len(candidates)}")
    processed = 0
    for sess in candidates:
        reason = "SESSION_TTL_EXPIRED" if sess.expires_at < now else "SESSION_ABANDONED_STALE"
        click.echo(
            f"session_id={sess.id} owner_user_id={sess.owner_user_id} "
            f"owner_admin_id={sess.owner_admin_id} reason={reason} "
            f"expires_at={sess.expires_at} updated_at={sess.updated_at}"
        )
        if apply_changes:
            updated = UploadSession.query.filter(
                UploadSession.id == sess.id, UploadSession.status == "active"
            ).update({UploadSession.status: "expired", UploadSession.failure_code: reason}, synchronize_session=False)
            if updated == 1:
                _safe_delete_upload_temp(_upload_session_temp_path(sess.storage_token))
                processed += 1
    if apply_changes:
        db.session.commit()
        click.echo(f"Expired: {processed}")


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
        if not user_can_manage_project(user, project):
            abort(404)
    
    # Redirect to projects list or preview
    return redirect(url_for("project_preview", project_id=project_id, admin_view=admin_view, user_id=view_user_id))


def _apply_fallback_pair_selection(project):
    """Shared body for set_project_fallback_pair()/admin_set_project_fallback_pair()
    below. Designates (or clears) `project.fallback_pair_id` - always
    re-checked against `project_id=project.id` so a pair belonging to a
    DIFFERENT project can never be selected, no matter what pair_index is
    submitted. No new upload flow: this only ever references one of the
    project's own already-uploaded pairs."""
    data = request.get_json(silent=True) if request.is_json else request.form
    raw = data.get("pair_index") if data else None
    if raw in (None, ""):
        project.fallback_pair_id = None
        db.session.commit()
        return jsonify({"success": True, "fallback_pair_index": None})

    try:
        pair_index = int(raw)
    except (TypeError, ValueError):
        return jsonify({"success": False, "code": "INVALID_PAIR_INDEX", "error": "pair_index must be an integer or null."}), 400

    pair = ProjectPair.query.filter_by(project_id=project.id, pair_index=pair_index).first()
    if not pair:
        return jsonify({"success": False, "code": "PAIR_NOT_FOUND", "error": "No such pair on this project."}), 404

    project.fallback_pair_id = pair.id
    db.session.commit()
    return jsonify({"success": True, "fallback_pair_index": pair.pair_index})


@app.route("/project/<int:project_id>/fallback-pair", methods=["POST"])
@login_required
def set_project_fallback_pair(project_id):
    """Creator-only (V1 Wave 6): designate or clear this project's
    project-level default fallback video, reusing one of the project's own
    existing pairs. Same ownership-check shape as project_view() above - a
    project not owned by the calling user 404s (never a 403, so existence
    is never leaked to a non-owner)."""
    user = current_user()
    project = Project.query.get_or_404(project_id)
    if not user_can_manage_project(user, project):
        abort(404)
    return _apply_fallback_pair_selection(project)


@app.route("/admin/project/<int:project_id>/fallback-pair", methods=["POST"])
@admin_required
def admin_set_project_fallback_pair(project_id):
    """Admin equivalent of set_project_fallback_pair() - same pattern as
    admin_scanner_test_entry() below."""
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    if project.owner_admin_id != admin.id:
        abort(404)
    return _apply_fallback_pair_selection(project)


# --------------------------------------------------------------------------------------------
# Subscription & Payment Routes
# --------------------------------------------------------------------------------------------
ADDON_PURCHASABLE_TYPES = {"EXTRA_SCANS", "VALIDITY_EXTENSION", "PROJECT_CAPACITY", "PROJECT_SERVICE_COVERAGE", "ACCOUNT_STORAGE"}
# Add-on types that target exactly one project. Everything else is
# account-level and must NOT carry a project_id.
ADDON_PROJECT_TARGETED_TYPES = {"PROJECT_SERVICE_COVERAGE"}
ENTITLEMENT_PROJECT_TARGETED_TYPES = {"PROJECT_SERVICE_COVERAGE"}


def _addon_catalog_payload(item):
    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "description": item.description,
        "addon_type": item.addon_type,
        "unit_amount": item.unit_amount,
        "currency": item.currency,
        "scan_delta": item.scan_delta,
        "validity_days_delta": item.validity_days_delta,
        "project_delta": item.project_delta,
        "storage_bytes_delta": item.storage_bytes_delta,
        "is_active": item.is_active,
        "is_commercially_available": item.is_commercially_available,
    }


def _addon_effect(item, quantity=1):
    quantity = max(1, int(quantity or 1))
    if item.addon_type == "EXTRA_SCANS":
        return "EXTRA_SCANS", int(item.scan_delta or 0) * quantity
    if item.addon_type == "VALIDITY_EXTENSION":
        return "VALIDITY_EXTENSION", int(item.validity_days_delta or 0) * quantity
    if item.addon_type == "PROJECT_CAPACITY":
        return "PROJECT_CAPACITY", int(item.project_delta or 0) * quantity
    if item.addon_type == "PROJECT_SERVICE_COVERAGE":
        # Reuses validity_days_delta as the catalog-driven duration; a
        # separate column would carry the same integer.
        return "PROJECT_SERVICE_COVERAGE", int(item.validity_days_delta or 0) * quantity
    if item.addon_type == "ACCOUNT_STORAGE":
        # Canonical quantity is BYTES, straight off the catalog row. No SKU
        # size and no price is defaulted anywhere in the code.
        return "ACCOUNT_STORAGE", int(item.storage_bytes_delta or 0) * quantity
    raise ValueError("Unsupported add-on type.")


def _validate_addon_catalog_for_purchase(item):
    if not item or not item.is_active or not item.is_commercially_available:
        return False, "ADDON_UNAVAILABLE", "This add-on is not available."
    if item.addon_type not in ADDON_PURCHASABLE_TYPES:
        return False, "ADDON_DISABLED", "This add-on type is not commercially available yet."
    entitlement_type, delta = _addon_effect(item, 1)
    if delta <= 0:
        return False, "ADDON_INVALID", "This add-on is not configured correctly."
    if item.unit_amount <= 0:
        return False, "ADDON_INVALID", "This add-on price is not configured correctly."
    return True, None, None


def _apply_entitlement_transaction(user, entitlement_type, delta, source_type, source_id, reason, metadata=None, project=None):
    existing = EntitlementTransaction.query.filter_by(
        source_type=source_type,
        source_id=source_id,
        entitlement_type=entitlement_type,
    ).first()
    if existing:
        return existing, True

    if entitlement_type in ENTITLEMENT_PROJECT_TARGETED_TYPES and project is None:
        raise ValueError(f"{entitlement_type} requires a target project.")

    now = dt.utcnow()
    tx = EntitlementTransaction(
        user_id=user.id,
        project_id=project.id if project else None,
        entitlement_type=entitlement_type,
        delta_value=int(delta),
        source_type=source_type,
        source_id=source_id,
        reason=reason,
        metadata_json=json.dumps(metadata or {}, sort_keys=True),
        valid_from=now,
    )
    db.session.add(tx)

    if entitlement_type == "EXTRA_SCANS":
        if user.subscribed_scan_limit not in (None, 0):
            # Clamped at zero: a negative adjustment (admin revoke) lowers the
            # allowance but must never drive the materialized column negative,
            # which _limit_reached would read as "unlimited".
            user.subscribed_scan_limit = max(0, int(user.subscribed_scan_limit or 0) + int(delta))
    elif entitlement_type == "VALIDITY_EXTENSION":
        base = user.subscription_expires_at if user.subscription_expires_at and user.subscription_expires_at > now else now
        user.subscription_expires_at = base + timedelta(days=int(delta))
        if user.subscription_status in ("expired", "limit_reached"):
            user.subscription_status = "active"
    elif entitlement_type == "PROJECT_CAPACITY":
        # Reusable account-level slot capacity, never a one-use creation
        # token: it raises the materialized effective limit and the ledger row
        # above is the permanent audit trail (never deleted on lapse).
        # Clamped at zero for the same reason EXTRA_SCANS is.
        if user.subscribed_project_limit not in (None, 0):
            user.subscribed_project_limit = max(0, int(user.subscribed_project_limit or 0) + int(delta))
    elif entitlement_type == "ACCOUNT_STORAGE":
        # Nothing materialized to bump: the effective storage allowance is
        # composed at read time by get_effective_entitlements() from plan base +
        # this ledger's purchased rows + this ledger's admin_grant rows, which
        # is what keeps the three sources separately auditable and stops either
        # from silently overwriting the other. The ledger row IS the entitlement.
        # Purchased storage therefore survives upgrade, downgrade and lapse for
        # free - no re-materialization path can drop it.
        pass
    elif entitlement_type == "PROJECT_SERVICE_COVERAGE":
        apply_standalone_project_renewal(
            project,
            user,
            delta,
            source_id=source_id,
            source_reference=f"{source_type}:{source_id}",
            reason=reason,
            now=now,
        )
    else:
        raise ValueError("Unsupported entitlement type.")

    return tx, False


def fulfill_addon_purchase(purchase):
    """Idempotently fulfill a paid AddonPurchase through the entitlement ledger."""
    item = AddonCatalog.query.get(purchase.catalog_id)
    if not item:
        return {"success": False, "code": "ADDON_NOT_FOUND", "error": "Add-on catalog item not found."}
    if purchase.status == "fulfilled":
        return {"success": True, "purchase_id": purchase.id, "replay": True}
    if purchase.status != "pending":
        return {"success": False, "code": "PURCHASE_NOT_PENDING", "error": "Add-on purchase is not pending."}

    entitlement_type, delta = _addon_effect(item, purchase.quantity)
    if delta <= 0:
        return {"success": False, "code": "ADDON_INVALID", "error": "Add-on entitlement is invalid."}

    user = User.query.get(purchase.user_id)
    if not user:
        return {"success": False, "code": "USER_NOT_FOUND", "error": "User not found."}

    project = None
    if item.addon_type in ADDON_PROJECT_TARGETED_TYPES:
        project = Project.query.get(purchase.project_id) if purchase.project_id else None
        if not project:
            return {"success": False, "code": "PROJECT_NOT_FOUND", "error": "Target ScanStory not found."}
        if not user_can_manage_project(user, project):
            return {"success": False, "code": "PROJECT_FORBIDDEN", "error": "You cannot renew this ScanStory."}
        eligible, code, message = project_renewal_eligibility(project)
        if not eligible:
            return {"success": False, "code": code, "error": message}
    elif purchase.project_id:
        return {"success": False, "code": "PROJECT_TARGET_INVALID", "error": "This add-on is account-level and cannot target a project."}

    try:
        _tx, replay = _apply_entitlement_transaction(
            user,
            entitlement_type,
            delta,
            "addon_purchase",
            purchase.id,
            f"Self-service add-on purchase {purchase.order_id}",
            metadata={"catalog_code": item.code, "quantity": purchase.quantity},
            project=project,
        )
        now = dt.utcnow()
        purchase.status = "fulfilled"
        purchase.paid_at = purchase.paid_at or now
        purchase.fulfilled_at = purchase.fulfilled_at or now
        purchase.failure_code = None
        db.session.commit()
        return {
            "success": True,
            "purchase_id": purchase.id,
            "entitlement_type": entitlement_type,
            "delta": delta,
            "replay": replay,
        }
    except IntegrityError:
        db.session.rollback()
        existing = EntitlementTransaction.query.filter_by(
            source_type="addon_purchase",
            source_id=purchase.id,
            entitlement_type=entitlement_type,
        ).first()
        fresh = AddonPurchase.query.get(purchase.id)
        if existing and fresh and fresh.status == "fulfilled":
            return {"success": True, "purchase_id": purchase.id, "replay": True}
        return {"success": False, "code": "ENTITLEMENT_CONFLICT", "error": "Entitlement was already recorded."}


REFUND_TERMINAL_STATUSES = {"REFUNDED"}
REFUND_ACTIVE_STATUSES = {"REFUND_REQUESTED", "REFUND_PROCESSING"}
REFUND_MANUAL_SUBSCRIPTION_MESSAGE = (
    "Subscription entitlement reconciliation requires manual admin review; "
    "subscription dates and limits were not changed automatically."
)
REFUND_MANUAL_VALIDITY_MESSAGE = (
    "Validity-extension reconciliation requires manual review; subscription expiry was not changed automatically."
)


def _refund_source_kind(refund):
    return "payment_order" if refund.payment_order_id else "addon_purchase"


def _existing_refund_for_source(payment_order=None, addon_purchase=None):
    if payment_order is not None:
        return PaymentRefund.query.filter_by(payment_order_id=payment_order.id).first()
    if addon_purchase is not None:
        return PaymentRefund.query.filter_by(addon_purchase_id=addon_purchase.id).first()
    return None


def _payment_refund_payload(refund):
    return {
        "id": refund.id,
        "payment_order_id": refund.payment_order_id,
        "addon_purchase_id": refund.addon_purchase_id,
        "user_id": refund.user_id,
        "project_id": refund.project_id,
        "provider": refund.provider,
        "provider_refund_id": refund.provider_refund_id,
        "provider_payment_id": refund.provider_payment_id,
        "provider_status": refund.provider_status,
        "amount": refund.amount,
        "currency": refund.currency,
        "status": refund.status,
        "reconciliation_status": refund.reconciliation_status,
        "reconciliation_message_safe": refund.reconciliation_message_safe,
        "requested_by_admin_id": refund.requested_by_admin_id,
        "requested_at": refund.requested_at.isoformat() if refund.requested_at else None,
        "completed_at": refund.completed_at.isoformat() if refund.completed_at else None,
        "failed_at": refund.failed_at.isoformat() if refund.failed_at else None,
        "failure_code": refund.failure_code,
        "failure_message_safe": refund.failure_message_safe,
    }


def _refund_amount_paise(amount):
    return max(100, int(round(float(amount or 0) * 100)))


def _refund_eligibility_payload(eligible, code, text, commercial_type, amount=None, currency=None, refund=None, effects=None):
    return {
        "eligible": bool(eligible),
        "reason_code": code,
        "reason_text": text,
        "commercial_type": commercial_type,
        "amount": amount,
        "currency": currency,
        "already_refunded": bool(refund and refund.status == "REFUNDED"),
        "refund_id": refund.id if refund else None,
        "refund_status": refund.status if refund else None,
        "reconciliation_status": refund.reconciliation_status if refund else None,
        "entitlement_effects": effects or {},
    }


def refund_eligibility_for_payment_order(payment_order):
    if not payment_order:
        return _refund_eligibility_payload(False, "NOT_FOUND", "Payment order not found.", "subscription")
    existing = _existing_refund_for_source(payment_order=payment_order)
    if existing:
        if existing.status in REFUND_ACTIVE_STATUSES:
            return _refund_eligibility_payload(False, "REFUND_ALREADY_PROCESSING", "A refund is already processing.", "subscription", refund=existing)
        if existing.status == "REFUND_FAILED":
            return _refund_eligibility_payload(False, "REFUND_PREVIOUSLY_FAILED", "The previous refund attempt failed and requires admin review.", "subscription", refund=existing)
        return _refund_eligibility_payload(False, "ALREADY_REFUNDED", "This payment has already been refunded.", "subscription", refund=existing)
    if payment_order.status != "success":
        return _refund_eligibility_payload(False, "PAYMENT_NOT_SUCCESSFUL", "Only successful paid orders can be refunded.", "subscription")
    if not (payment_order.razorpay_payment_id or "").strip():
        return _refund_eligibility_payload(False, "PROVIDER_PAYMENT_MISSING", "Provider payment id is missing.", "subscription")
    return _refund_eligibility_payload(
        True,
        None,
        None,
        "subscription",
        amount=payment_order.total_amount,
        currency=payment_order.currency,
        effects={"subscription_policy": "MANUAL_REVIEW_REQUIRED"},
    )


def _latest_standalone_renewal_coverage_for_project(project_id):
    return (
        ProjectServiceCoverage.query
        .filter_by(project_id=project_id, source_type="STANDALONE_PROJECT_RENEWAL", status="ACTIVE")
        .filter(ProjectServiceCoverage.coverage_end.isnot(None))
        .order_by(ProjectServiceCoverage.coverage_start.desc(), ProjectServiceCoverage.id.desc())
        .first()
    )


def _coverage_for_addon_purchase(purchase):
    return ProjectServiceCoverage.query.filter_by(
        source_type="STANDALONE_PROJECT_RENEWAL",
        source_id=purchase.id,
        source_reference=f"addon_purchase:{purchase.id}",
    ).first()


def refund_eligibility_for_addon_purchase(purchase, now=None):
    now = now or get_utc_now()
    if not purchase:
        return _refund_eligibility_payload(False, "NOT_FOUND", "Add-on purchase not found.", "addon")
    item = AddonCatalog.query.get(purchase.catalog_id)
    commercial_type = item.addon_type if item else "addon"
    existing = _existing_refund_for_source(addon_purchase=purchase)
    if existing:
        if existing.status in REFUND_ACTIVE_STATUSES:
            return _refund_eligibility_payload(False, "REFUND_ALREADY_PROCESSING", "A refund is already processing.", commercial_type, refund=existing)
        if existing.status == "REFUND_FAILED":
            return _refund_eligibility_payload(False, "REFUND_PREVIOUSLY_FAILED", "The previous refund attempt failed and requires admin review.", commercial_type, refund=existing)
        return _refund_eligibility_payload(False, "ALREADY_REFUNDED", "This purchase has already been refunded.", commercial_type, refund=existing)
    if purchase.status != "fulfilled":
        return _refund_eligibility_payload(False, "PURCHASE_NOT_FULFILLED", "Only fulfilled purchases can be refunded.", commercial_type)
    if not (purchase.razorpay_payment_id or "").strip():
        return _refund_eligibility_payload(False, "PROVIDER_PAYMENT_MISSING", "Provider payment id is missing.", commercial_type)
    if not item:
        return _refund_eligibility_payload(False, "ADDON_NOT_FOUND", "Add-on catalog item not found.", commercial_type)

    entitlement_type, delta = _addon_effect(item, purchase.quantity)
    effects = {"entitlement_type": entitlement_type, "delta": -int(delta)}
    if entitlement_type == "PROJECT_SERVICE_COVERAGE":
        coverage = _coverage_for_addon_purchase(purchase)
        if not coverage or coverage.status != "ACTIVE":
            return _refund_eligibility_payload(False, "COVERAGE_NOT_ACTIVE", "Renewal coverage is not active.", commercial_type)
        latest = _latest_standalone_renewal_coverage_for_project(purchase.project_id)
        if latest and latest.id != coverage.id:
            return _refund_eligibility_payload(
                False,
                "SUPERSEDED_BY_LATER_RENEWAL",
                "A later standalone renewal depends on this coverage.",
                commercial_type,
            )
        if coverage.coverage_start <= now:
            return _refund_eligibility_payload(
                False,
                "INELIGIBLE_CONSUMED_SERVICE",
                "This renewal period has already started and requires manual review.",
                commercial_type,
            )
        effects["coverage_id"] = coverage.id
    return _refund_eligibility_payload(True, None, None, commercial_type, purchase.total_amount, purchase.currency, effects=effects)


def _create_refund_row_for_source(admin, reason, idempotency_key, payment_order=None, addon_purchase=None):
    if not (reason or "").strip():
        raise ValueError("Refund reason is required.")
    if payment_order is not None:
        source = payment_order
        user_id = payment_order.user_id
        project_id = None
        provider_payment_id = payment_order.razorpay_payment_id
        amount = payment_order.total_amount
        currency = payment_order.currency
    else:
        source = addon_purchase
        user_id = addon_purchase.user_id
        project_id = addon_purchase.project_id
        provider_payment_id = addon_purchase.razorpay_payment_id
        amount = addon_purchase.total_amount
        currency = addon_purchase.currency
    return PaymentRefund(
        payment_order_id=payment_order.id if payment_order else None,
        addon_purchase_id=addon_purchase.id if addon_purchase else None,
        user_id=user_id,
        project_id=project_id,
        provider="RAZORPAY",
        provider_payment_id=provider_payment_id,
        amount=amount,
        currency=currency,
        status="REFUND_REQUESTED",
        reconciliation_status="PENDING",
        reason=reason.strip(),
        requested_by_admin_id=admin.id,
        requested_at=get_utc_now(),
        idempotency_key=idempotency_key,
        metadata_json=json.dumps({"source": source.__tablename__}, sort_keys=True),
    )


def _safe_provider_failure_message(exc):
    return "Payment gateway refund request failed."


def _refund_replay_response(refund):
    """Idempotent-replay response that reflects the row's AUTHORITATIVE state.

    The old version returned a flat success for any row matching the
    idempotency key, so replaying a request whose refund had FAILED at the
    provider answered "success" and the money never moved (V1.1 P0-1). Success
    now means "the provider refund is not in a failed state"; a failed row
    answers with its real state plus the recovery path.
    """
    payload = _payment_refund_payload(refund)
    if refund.status == "REFUND_FAILED":
        return {
            "success": False,
            "code": "REFUND_PREVIOUSLY_FAILED",
            "error": (
                "The refund for this payment failed at the payment gateway and was not retried. "
                "Use refund recovery to re-drive this refund; a second refund record is never created."
            ),
            "refund": payload,
            "replay": True,
        }
    response = {"success": True, "refund": payload, "replay": True}
    if refund.status == "REFUNDED" and refund.reconciliation_status in ("FAILED", "PENDING"):
        # Money HAS moved - this is genuinely a success - but the local
        # entitlement bookkeeping did not complete, so say so instead of
        # letting the caller read a bare success and assume it did.
        response["code"] = "REFUND_RECONCILIATION_INCOMPLETE"
    elif refund.status in REFUND_ACTIVE_STATUSES:
        response["code"] = "REFUND_ALREADY_PROCESSING"
    return response


def _call_razorpay_full_refund(refund):
    if not razorpay_client:
        raise RuntimeError("Payment gateway not configured.")
    data = {
        "amount": _refund_amount_paise(refund.amount),
        "notes": {
            "refund_id": str(refund.id),
            "source": _refund_source_kind(refund),
            "admin_id": str(refund.requested_by_admin_id),
        },
    }
    return razorpay_client.payment.refund(refund.provider_payment_id, data)


def _provider_refund_status_to_local(provider_status):
    status = (provider_status or "").strip().lower()
    if status == "processed":
        return "REFUNDED"
    if status == "failed":
        return "REFUND_FAILED"
    return "REFUND_PROCESSING"


def _original_entitlement_for_refund(refund):
    if not refund.addon_purchase_id:
        return None
    return EntitlementTransaction.query.filter_by(
        source_type="addon_purchase",
        source_id=refund.addon_purchase_id,
    ).first()


def _apply_refund_reconciliation(refund):
    if refund.reconciliation_status in {"APPLIED", "MANUAL_REVIEW_REQUIRED"}:
        return True
    if refund.payment_order_id:
        order = PaymentOrder.query.get(refund.payment_order_id)
        if order:
            order.status = "refunded"
        refund.reconciliation_status = "MANUAL_REVIEW_REQUIRED"
        refund.reconciliation_message_safe = REFUND_MANUAL_SUBSCRIPTION_MESSAGE
        return True

    purchase = AddonPurchase.query.get(refund.addon_purchase_id)
    item = AddonCatalog.query.get(purchase.catalog_id) if purchase else None
    original = _original_entitlement_for_refund(refund)
    if not purchase or not item or not original:
        refund.reconciliation_status = "FAILED"
        refund.reconciliation_message_safe = "Original add-on entitlement could not be found."
        return False

    if item.addon_type == "VALIDITY_EXTENSION":
        refund.reconciliation_status = "MANUAL_REVIEW_REQUIRED"
        refund.reconciliation_message_safe = REFUND_MANUAL_VALIDITY_MESSAGE
        purchase.status = "refunded"
        return True

    existing_reversal = EntitlementTransaction.query.filter_by(
        source_type="refund",
        source_id=refund.id,
        entitlement_type=original.entitlement_type,
    ).first()
    if not existing_reversal:
        reversal = EntitlementTransaction(
            user_id=refund.user_id,
            project_id=original.project_id,
            entitlement_type=original.entitlement_type,
            delta_value=-int(original.delta_value or 0),
            source_type="refund",
            source_id=refund.id,
            reason=f"Refund reversal for {refund.id}",
            metadata_json=json.dumps({
                "original_transaction_id": original.id,
                "refund_id": refund.id,
                "commercial_source": "addon_purchase",
                "addon_purchase_id": purchase.id,
            }, sort_keys=True),
            valid_from=get_utc_now(),
        )
        db.session.add(reversal)
        if original.entitlement_type == "EXTRA_SCANS" and purchase.user.subscribed_scan_limit not in (None, 0):
            purchase.user.subscribed_scan_limit = int(purchase.user.subscribed_scan_limit or 0) - int(original.delta_value or 0)
        elif original.entitlement_type == "PROJECT_CAPACITY" and purchase.user.subscribed_project_limit not in (None, 0):
            purchase.user.subscribed_project_limit = int(purchase.user.subscribed_project_limit or 0) - int(original.delta_value or 0)
        elif original.entitlement_type == "ACCOUNT_STORAGE":
            # NON-DESTRUCTIVE BY CONSTRUCTION. The negative ledger row above is
            # the whole reversal - there is no materialized column to unwind and
            # no media is touched. If the account's usage now exceeds the
            # reduced allowance it simply becomes over-storage: existing content
            # keeps working, and only NEW consumption is blocked.
            pass
        elif original.entitlement_type == "PROJECT_SERVICE_COVERAGE":
            coverage = _coverage_for_addon_purchase(purchase)
            if coverage and coverage.status == "ACTIVE":
                coverage.status = "REVOKED"
                coverage.revoked_at = get_utc_now()
                coverage.revoked_by_refund_id = refund.id
            else:
                refund.reconciliation_status = "FAILED"
                refund.reconciliation_message_safe = "Original project coverage could not be revoked."
                return False
    purchase.status = "refunded"
    refund.reconciliation_status = "APPLIED"
    refund.reconciliation_message_safe = "Refund reconciliation applied."
    return True


def mark_refund_provider_result(refund, provider_refund_id=None, provider_status=None, failure_code=None, failure_message_safe=None):
    now = get_utc_now()
    if provider_refund_id:
        refund.provider_refund_id = provider_refund_id
    if provider_status:
        refund.provider_status = provider_status
    local_status = _provider_refund_status_to_local(provider_status)
    refund.status = local_status
    if local_status == "REFUNDED":
        refund.completed_at = refund.completed_at or now
        refund.failed_at = None
        refund.failure_code = None
        refund.failure_message_safe = None
        _apply_refund_reconciliation(refund)
    elif local_status == "REFUND_FAILED":
        refund.failed_at = now
        refund.failure_code = failure_code or "PROVIDER_REFUND_FAILED"
        refund.failure_message_safe = failure_message_safe or "Provider reported refund failure."
    return refund


def initiate_admin_refund(admin, payment_order=None, addon_purchase=None, reason=None, idempotency_key=None):
    if not (reason or "").strip():
        return {"success": False, "code": "REFUND_REASON_REQUIRED", "error": "Refund reason is required."}

    source_name = "payment_order" if payment_order is not None else "addon_purchase"
    source_id = payment_order.id if payment_order is not None else addon_purchase.id
    idempotency_key = (idempotency_key or f"refund:{source_name}:{source_id}").strip()[:120]
    existing = PaymentRefund.query.filter_by(idempotency_key=idempotency_key).first()
    if existing:
        return _refund_replay_response(existing)

    eligibility = (
        refund_eligibility_for_payment_order(payment_order)
        if payment_order is not None
        else refund_eligibility_for_addon_purchase(addon_purchase)
    )
    if not eligibility["eligible"]:
        return {"success": False, "code": eligibility["reason_code"], "eligibility": eligibility}

    refund = _create_refund_row_for_source(admin, reason, idempotency_key, payment_order, addon_purchase)
    db.session.add(refund)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = _existing_refund_for_source(payment_order=payment_order, addon_purchase=addon_purchase)
        if existing:
            # A DIFFERENT idempotency key racing the same source. The source
            # uniqueness constraint is the guarantee that one payment gets one
            # refund record; it stays, and the caller is told the real state of
            # the record that already exists rather than a fabricated success.
            return _refund_replay_response(existing)
        raise

    log_admin_activity(
        admin.id,
        "refund_requested",
        f"Refund {refund.id} requested for {source_name} {source_id}: {refund.reason[:150]}",
    )
    refund.status = "REFUND_PROCESSING"
    refund.processing_started_at = get_utc_now()
    db.session.commit()
    log_admin_activity(admin.id, "refund_provider_attempted", f"Refund {refund.id} sent to Razorpay.")

    try:
        provider_result = _call_razorpay_full_refund(refund)
    except Exception as exc:
        refund.status = "REFUND_FAILED"
        refund.failed_at = get_utc_now()
        refund.failure_code = "PROVIDER_REQUEST_FAILED"
        refund.failure_message_safe = _safe_provider_failure_message(exc)
        db.session.commit()
        log_admin_activity(admin.id, "refund_failed", f"Refund {refund.id} provider request failed.")
        return {"success": False, "code": "PROVIDER_REQUEST_FAILED", "refund": _payment_refund_payload(refund)}

    provider_refund_id = provider_result.get("id") if isinstance(provider_result, dict) else None
    provider_status = provider_result.get("status") if isinstance(provider_result, dict) else None
    try:
        mark_refund_provider_result(refund, provider_refund_id, provider_status)
        db.session.commit()
    except Exception:
        db.session.rollback()
        refund = PaymentRefund.query.get(refund.id)
        refund.reconciliation_status = "FAILED"
        refund.reconciliation_message_safe = "Refund provider accepted, but local reconciliation failed."
        db.session.commit()
        return {"success": True, "refund": _payment_refund_payload(refund), "replay": False}

    if refund.status == "REFUNDED":
        log_admin_activity(admin.id, "refund_confirmed", f"Refund {refund.id} confirmed by provider.")
        log_admin_activity(
            admin.id,
            "refund_reconciliation",
            f"Refund {refund.id} reconciliation status {refund.reconciliation_status}.",
        )
    elif refund.status == "REFUND_FAILED":
        log_admin_activity(admin.id, "refund_failed", f"Refund {refund.id} failed at provider.")
    else:
        log_admin_activity(admin.id, "refund_provider_accepted", f"Refund {refund.id} accepted by provider.")
    return {"success": True, "refund": _payment_refund_payload(refund), "replay": False}


# ---------------------------------------------------------------------
# Refund recovery / reconciliation (V1.1 P0-1)
# ---------------------------------------------------------------------
# THE PROVIDER IS THE ONLY AUTHORITY ON WHETHER MONEY MOVED.
# Every mutating outcome below is derived from a provider READ first, never
# from a local guess, and a read that cannot be completed produces
# "unresolved" (a human looks) instead of a second refund call. Recovery
# always re-drives the EXISTING PaymentRefund row: no second row is created,
# the four uniqueness constraints are untouched, entitlements are only ever
# reversed by _apply_refund_reconciliation (which runs only after the provider
# reports "processed"), and no code path here deletes media.

# Rows whose provider outcome is not yet a confirmed success.
REFUND_UNCONFIRMED_STATUSES = ("REFUND_REQUESTED", "REFUND_PROCESSING", "REFUND_FAILED")
# Webhook failure code for a provider-dashboard refund we correlated to a local
# payment but that has no local PaymentRefund record. Deliberately its own code
# so `flask reconcile-refunds` can list it instead of it hiding in "unknown".
OUT_OF_BAND_REFUND_FAILURE_CODE = "out_of_band_refund_no_local_record"


def stuck_refund_filter():
    """The ONE definition of "this refund needs attention".

    Shared by `flask reconcile-refunds` and the admin read API (P1-6) so the
    operator queue and the recovery command can never disagree about which
    refunds are outstanding. A settled refund (REFUNDED + APPLIED) matches
    neither branch and is therefore excluded.
    """
    return or_(
        PaymentRefund.status.in_(REFUND_UNCONFIRMED_STATUSES),
        and_(
            PaymentRefund.status == "REFUNDED",
            PaymentRefund.reconciliation_status != "APPLIED",
        ),
    )


def stuck_refund_query():
    """PaymentRefund rows that are not in a finished, self-consistent state."""
    return PaymentRefund.query.filter(stuck_refund_filter()).order_by(PaymentRefund.id.asc())


def _provider_refunds_for_payment(refund):
    """Every provider refund recorded against this payment. Raises on API error.

    A read, never a write - this is what makes it safe to call before deciding
    whether a retry would double-refund.
    """
    if not razorpay_client:
        raise RuntimeError("Payment gateway not configured.")
    result = razorpay_client.payment.fetch_multiple_refund(refund.provider_payment_id)
    if isinstance(result, dict):
        items = result.get("items") or []
    elif isinstance(result, list):
        items = result
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _matching_provider_refund(refund, items):
    """Classify the provider's view of this record: one / none / ambiguous.

    "one" means exactly one provider refund is unmistakably this record (same
    provider refund id, or the only refund on the payment for the full amount).
    Anything else on the payment - a partial refund, two refunds, a full refund
    for a different amount - is "ambiguous" and must never be auto-resolved.
    """
    if refund.provider_refund_id:
        exact = [item for item in items if item.get("id") == refund.provider_refund_id]
        if exact:
            return "one", exact[0]
    if not items:
        return "none", None
    expected_paise = _refund_amount_paise(refund.amount)
    candidates = []
    for item in items:
        try:
            if int(item.get("amount")) == expected_paise:
                candidates.append(item)
        except (TypeError, ValueError):
            continue
    if len(candidates) == 1 and len(items) == 1:
        return "one", candidates[0]
    return "ambiguous", None


def _refund_recovery_result(refund, outcome, message, changed=False):
    return {
        "refund_id": refund.id,
        "source": _refund_source_kind(refund),
        "outcome": outcome,
        "message": message,
        "changed": bool(changed),
        "status": refund.status,
        "reconciliation_status": refund.reconciliation_status,
    }


# Outcomes that mean "a human still has to do something".
REFUND_RECOVERY_UNRESOLVED_OUTCOMES = {"unresolved", "retry_failed", "manual_review"}


def _recover_payment_refund(refund, apply_changes):
    # 1. Finished and self-consistent. Idempotent no-op.
    if refund.status == "REFUNDED" and refund.reconciliation_status == "APPLIED":
        return _refund_recovery_result(
            refund, "already_settled", "Provider refund and local reconciliation are both complete."
        )

    # 2. Manual review is a human decision. Recovery reports it and stops - it
    #    never silently revokes a subscription or resolves the review itself.
    if refund.reconciliation_status == "MANUAL_REVIEW_REQUIRED":
        return _refund_recovery_result(
            refund,
            "manual_review",
            "Reconciliation is parked for admin review and is not resolved automatically.",
        )

    # 3. Provider already confirmed the refund -> re-drive LOCAL reconciliation
    #    ONLY. No provider call: the money has moved and asking for it again is
    #    the one mistake that cannot be undone.
    if refund.status == "REFUNDED":
        if not apply_changes:
            return _refund_recovery_result(
                refund, "would_reconcile", "Local reconciliation would be retried; no provider call is made."
            )
        applied = _apply_refund_reconciliation(refund)
        db.session.commit()
        if applied:
            outcome = "manual_review" if refund.reconciliation_status == "MANUAL_REVIEW_REQUIRED" else "reconciled"
            return _refund_recovery_result(
                refund, outcome, f"Local reconciliation re-driven; reconciliation status {refund.reconciliation_status}.", changed=True
            )
        return _refund_recovery_result(
            refund, "unresolved", "Local reconciliation could not be completed; provider refund remains confirmed.", changed=True
        )

    # 4. Provider outcome unknown or failed -> ASK THE PROVIDER FIRST.
    try:
        items = _provider_refunds_for_payment(refund)
    except Exception:
        # Full detail to the operator log, nothing provider-shaped to the caller.
        app.logger.exception("refund_recovery_provider_read_failed refund_id=%s", refund.id)
        return _refund_recovery_result(
            refund, "unresolved", "Provider refund state could not be read; the record was left unchanged for manual review."
        )

    match, item = _matching_provider_refund(refund, items)

    if match == "ambiguous":
        return _refund_recovery_result(
            refund,
            "unresolved",
            "The provider reports refunds on this payment that do not unambiguously match this record; left for manual review.",
        )

    if match == "one":
        # The provider already accepted a refund for this record. Adopt its
        # state - never issue another one.
        if not apply_changes:
            return _refund_recovery_result(
                refund,
                "would_adopt_provider_state",
                "The provider already holds a matching refund; its state would be adopted and no new refund issued.",
            )
        mark_refund_provider_result(refund, item.get("id"), item.get("status"))
        db.session.commit()
        return _refund_recovery_result(
            refund,
            "adopted_provider_state",
            f"Adopted the provider's existing refund state; status {refund.status}, reconciliation {refund.reconciliation_status}.",
            changed=True,
        )

    # match == "none": the provider holds NO refund for this payment.
    if refund.status == "REFUND_PROCESSING":
        # We recorded that the create call was accepted; the provider disagrees.
        # Re-issuing on that contradiction is precisely how a double refund
        # happens if the read was stale, so a human resolves it instead.
        return _refund_recovery_result(
            refund,
            "unresolved",
            "This refund is marked processing but the provider holds no matching refund; left for manual review.",
        )

    if not apply_changes:
        return _refund_recovery_result(
            refund,
            "would_retry_provider",
            "The provider holds no refund for this payment; the refund call would be re-attempted on this same record.",
        )

    # Re-attempt on the SAME row. The row is deliberately NOT pre-marked
    # REFUND_PROCESSING: if this process dies mid-call the row stays as it was
    # and the next recovery run re-reads the provider and either adopts the
    # refund that landed or retries - a pre-marked row would instead be stuck in
    # the "processing but provider has nothing" manual-review branch forever.
    try:
        provider_result = _call_razorpay_full_refund(refund)
    except Exception as exc:
        refund.status = "REFUND_FAILED"
        refund.failed_at = get_utc_now()
        refund.failure_code = "PROVIDER_REQUEST_FAILED"
        refund.failure_message_safe = _safe_provider_failure_message(exc)
        db.session.commit()
        return _refund_recovery_result(
            refund,
            "retry_failed",
            "The provider refund re-attempt failed; no entitlement was reversed.",
            changed=True,
        )
    refund.processing_started_at = refund.processing_started_at or get_utc_now()
    mark_refund_provider_result(
        refund,
        provider_result.get("id") if isinstance(provider_result, dict) else None,
        provider_result.get("status") if isinstance(provider_result, dict) else None,
    )
    db.session.commit()
    return _refund_recovery_result(
        refund,
        "retried",
        f"Provider refund re-attempted on the existing record; status {refund.status}, reconciliation {refund.reconciliation_status}.",
        changed=True,
    )


def recover_payment_refund(refund, admin=None, apply_changes=False):
    """Re-drive one stuck PaymentRefund. Read-only unless apply_changes=True."""
    result = _recover_payment_refund(refund, apply_changes)
    if result["changed"] or result["outcome"] in REFUND_RECOVERY_UNRESOLVED_OUTCOMES:
        detail = f"Refund {refund.id} recovery outcome {result['outcome']}: {result['message']}"
        if admin is not None:
            log_admin_activity(admin.id, "refund_recovery", detail[:500])
        else:
            app.logger.info(
                "refund_recovery refund_id=%s outcome=%s changed=%s status=%s reconciliation=%s",
                refund.id, result["outcome"], result["changed"], result["status"], result["reconciliation_status"],
            )
    return result


def unlinked_out_of_band_refund_events():
    """Provider-dashboard refunds correlated to a local payment but unrecorded."""
    return (
        RazorpayWebhookEvent.query
        .filter(RazorpayWebhookEvent.failure_code == OUT_OF_BAND_REFUND_FAILURE_CODE)
        .order_by(RazorpayWebhookEvent.id.asc())
        .all()
    )


@app.route("/api/addons/catalog", methods=["GET"])
@login_required
def addon_catalog():
    items = (
        AddonCatalog.query.filter_by(is_active=True, is_commercially_available=True)
        .filter(AddonCatalog.addon_type.in_(ADDON_PURCHASABLE_TYPES))
        .order_by(AddonCatalog.id.asc())
        .all()
    )
    return jsonify({"success": True, "addons": [_addon_catalog_payload(item) for item in items]})


@app.route("/api/addons/orders", methods=["POST"])
@login_required
def create_addon_order():
    user = current_user()
    payload = request.get_json(silent=True) or request.form
    try:
        catalog_id = int(payload.get("catalog_id"))
    except (TypeError, ValueError, AttributeError):
        catalog_id = None
    try:
        quantity = int(payload.get("quantity", 1))
    except (TypeError, ValueError, AttributeError):
        quantity = 1
    if quantity < 1 or quantity > 20:
        return jsonify({"success": False, "code": "INVALID_QUANTITY", "error": "Invalid quantity."}), 400
    item = AddonCatalog.query.get(catalog_id)
    ok, code, message = _validate_addon_catalog_for_purchase(item)
    if not ok:
        return jsonify({"success": False, "code": code, "error": message}), 400

    # Project targeting (Domain 2B): renewal add-ons bind to exactly one
    # project the buyer is authorised to manage; account-level add-ons must not
    # carry a project at all.
    try:
        project_id = int(payload.get("project_id"))
    except (TypeError, ValueError, AttributeError):
        project_id = None
    project = None
    if item.addon_type in ADDON_PROJECT_TARGETED_TYPES:
        project = Project.query.get(project_id) if project_id else None
        if not project or not user_can_manage_project(user, project):
            return jsonify({"success": False, "code": "PROJECT_NOT_FOUND", "error": "ScanStory not found."}), 404
        eligible, ecode, emessage = project_renewal_eligibility(project)
        if not eligible:
            return jsonify({"success": False, "code": ecode, "error": emessage}), 400
    elif project_id:
        return jsonify({"success": False, "code": "PROJECT_TARGET_INVALID", "error": "This add-on is account-level and cannot target a project."}), 400

    if not razorpay_client:
        return jsonify({"success": False, "code": "PAYMENT_NOT_CONFIGURED", "error": "Payment gateway not configured."}), 503

    total_amount = round(float(item.unit_amount) * quantity, 2)
    amount_paise = int(round(total_amount * 100))
    if amount_paise < 100:
        amount_paise = 100
    purchase = AddonPurchase(
        order_id=f"ADDON_{user.id}_{int(time.time())}_{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        catalog_id=item.id,
        project_id=project.id if project else None,
        quantity=quantity,
        amount=item.unit_amount,
        total_amount=total_amount,
        currency=item.currency,
        status="pending",
    )
    db.session.add(purchase)
    db.session.flush()
    try:
        razorpay_order = razorpay_client.order.create(data={
            "amount": amount_paise,
            "currency": item.currency,
            "payment_capture": 1,
            "notes": {
                "user_id": str(user.id),
                "addon_purchase_id": str(purchase.id),
                "addon_code": item.code,
                "project_id": str(project.id) if project else "",
            },
        })
        purchase.razorpay_order_id = razorpay_order["id"]
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"success": False, "code": "ORDER_CREATE_FAILED", "error": "Could not create add-on order."}), 502

    return jsonify({
        "success": True,
        "purchase_id": purchase.id,
        "order_id": purchase.razorpay_order_id,
        "amount": amount_paise,
        "currency": purchase.currency,
        "key": RAZORPAY_KEY_ID,
        "name": "ScanStory",
        "description": item.name,
    }), 201


@app.route("/api/addons/purchases/<int:purchase_id>/verify", methods=["POST"])
@login_required
def verify_addon_purchase(purchase_id):
    user = current_user()
    purchase = AddonPurchase.query.get_or_404(purchase_id)
    if purchase.user_id != user.id:
        return jsonify({"success": False, "code": "NOT_FOUND", "error": "Add-on purchase not found."}), 404
    razorpay_payment_id = request.form.get("razorpay_payment_id")
    razorpay_order_id = request.form.get("razorpay_order_id")
    razorpay_signature = request.form.get("razorpay_signature")
    if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature]):
        return jsonify({"success": False, "code": "MISSING_PAYMENT_DETAILS", "error": "Missing payment details."}), 400
    if razorpay_order_id != purchase.razorpay_order_id:
        return jsonify({"success": False, "code": "ORDER_MISMATCH", "error": "Payment order does not match this add-on purchase."}), 400
    # Re-bind catalog item + amount (and, for renewals, the target project) to
    # the stored purchase before any entitlement is applied.
    catalog_item = AddonCatalog.query.get(purchase.catalog_id)
    if not catalog_item or round(float(catalog_item.unit_amount) * int(purchase.quantity or 1), 2) != round(float(purchase.total_amount), 2):
        return jsonify({"success": False, "code": "AMOUNT_MISMATCH", "error": "Add-on pricing changed; please start a new order."}), 409
    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        })
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"success": False, "code": "SIGNATURE_INVALID", "error": "Invalid payment signature."}), 400

    if not purchase.razorpay_payment_id:
        purchase.razorpay_payment_id = razorpay_payment_id
        purchase.razorpay_signature = razorpay_signature
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return jsonify({"success": False, "code": "PAYMENT_REUSED", "error": "This payment has already been used."}), 409
    elif purchase.razorpay_payment_id != razorpay_payment_id:
        return jsonify({"success": False, "code": "PAYMENT_MISMATCH", "error": "Payment does not match this add-on purchase."}), 409

    result = fulfill_addon_purchase(purchase)
    status = 200 if result.get("success") else 409
    return jsonify(result), status


@app.route("/api/account/capacity", methods=["GET"])
@login_required
def account_capacity_summary():
    user = current_user()
    return jsonify({
        "success": True,
        "capacity": project_capacity_summary(user),
        "entitlement_summary": user_entitlement_summary(user),
    })


@app.route("/api/projects/<int:project_id>/coverage", methods=["GET"])
@login_required
def project_service_coverage_summary(project_id):
    user = current_user()
    project = Project.query.get(project_id)
    if not project or not user_can_manage_project(user, project):
        return jsonify({"success": False, "code": "NOT_FOUND", "error": "ScanStory not found."}), 404
    return jsonify({"success": True, "coverage": project_coverage_summary(project)})


# ---------------------------------------------------------------------------
# Public content reporting (Domain 2B). Creating a report NEVER suspends,
# deletes or notifies - it only queues a row for explicit human review.
# CONTENT_REPORT_DETAILS_MAX is declared with the V1.1 label maps above, so the
# viewer's textarea maxlength and this route's validation are the same number.
# ---------------------------------------------------------------------------


def _privacy_hash(value):
    """Same one-way sha256 convention used elsewhere in this codebase, salted
    with the app secret so a raw IP/session id is never stored or recoverable."""
    if not value:
        return None
    return hashlib.sha256(f"{app.secret_key}:{value}".encode("utf-8")).hexdigest()


@app.route("/api/projects/<int:project_id>/report", methods=["POST"])
@csrf.exempt  # Public, unauthenticated viewer endpoint - no browser session/cookie to bind a CSRF token to (same class as the scanner endpoints).
def report_project_content(project_id):
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"success": False, "code": "NOT_FOUND", "error": "ScanStory not found."}), 404

    session_id = (request.headers.get("X-Scan-Session") or session.get("scan_session_id") or "")[:120]
    ip_hash = _privacy_hash(_client_ip())
    session_hash = _privacy_hash(session_id) if session_id else None

    ok, retry_after = _check_rate_limit(
        "content_report",
        _rate_limit_key("content_report", project_id, session_hash or "-"),
    )
    if not ok:
        response = jsonify({"success": False, "code": "RATE_LIMITED", "error": "Too many reports. Please try again later."})
        response.status_code = 429
        response.headers["Retry-After"] = str(retry_after)
        return response

    payload = request.get_json(silent=True) or request.form
    reason = (payload.get("reason") or "").strip().upper()
    if reason not in CONTENT_REPORT_REASONS:
        return jsonify({"success": False, "code": "INVALID_REASON", "error": "Please choose a valid report reason."}), 400
    details = (payload.get("details") or "").strip()
    if len(details) > CONTENT_REPORT_DETAILS_MAX:
        return jsonify({"success": False, "code": "DETAILS_TOO_LONG", "error": f"Details must be {CONTENT_REPORT_DETAILS_MAX} characters or fewer."}), 400
    reporter_email = (payload.get("reporter_email") or "").strip()[:255] or None

    reporter = current_user()
    report = ContentReport(
        project_id=project.id,
        reporter_user_id=reporter.id if reporter else None,
        reporter_email=reporter_email or (reporter.email if reporter else None),
        reporter_session_hash=session_hash,
        reporter_ip_hash=ip_hash,
        reason=reason,
        details=details or None,
        status="OPEN",
        metadata_json=json.dumps({
            "user_agent": (request.headers.get("User-Agent") or "")[:300],
            "referrer": (request.referrer or "")[:300],
        }, sort_keys=True),
    )
    db.session.add(report)
    db.session.commit()
    # Deliberately generic: no report id, no counts, no moderation state.
    return jsonify({"success": True, "message": "Thanks - our team will review this."}), 201


@app.route("/pricing")
def pricing_page():
    """Public pricing page — no login required. Passes user=None for guests."""
    plans = purchasable_plans_query().order_by(SubscriptionPlan.display_order.asc()).all()
    user = current_user()  # None for guests, User object if logged in
    return render_template(
        "user/subscribe.html",
        plans=plans,
        user=user,
        get_system_config=get_system_config,
        dev_test_entitled=has_dev_test_entitlement(user),
        entitlement_summary=user_entitlement_summary(user),
        v11_experience_options=V11_EXPERIENCE_PRESENTATION,
    )


@app.route("/subscribe", methods=["GET"])
@login_required
def subscribe_page():
    """Show subscription plans"""
    user = current_user()
    plans = purchasable_plans_query().order_by(SubscriptionPlan.display_order.asc()).all()
    
    return render_template("user/subscribe.html", 
                         plans=plans, 
                         user=user,
                         get_system_config=get_system_config,
                         dev_test_entitled=has_dev_test_entitlement(user),
                         entitlement_summary=user_entitlement_summary(user),
                         v11_experience_options=V11_EXPERIENCE_PRESENTATION)

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
    user = User.query.get(payment_order.user_id)
    current_plan = getattr(user, "subscription_plan", None)

    # ---------------------------------------------------------------
    # Wave 2: is this a downgrade? A confirmed paid change to a LOWER
    # commercial policy is never applied mid-term - it is parked on the user
    # and applied at the current term's natural expiry by
    # apply_pending_plan_change_if_due(). Nothing is rolled back or deleted.
    # A downgrade with no paid term left to wait for is just an ordinary
    # activation, so it applies immediately.
    # ---------------------------------------------------------------
    has_paid_term_remaining = bool(
        user
        and user.subscription_status == "active"
        and user.subscription_expires_at
        and user.subscription_expires_at > now
    )
    defer_change = has_paid_term_remaining and is_downgrade(current_plan, plan)

    if plan.duration_type == "time":
        # Real calendar months, not duration_value * 30 (P0-1 / ANM-41): a plan
        # whose duration_display advertises "1 Year" (duration_value == 12)
        # granted 360 days.
        subscription_end = _add_calendar_months(now, plan.duration_value or 0)
        # Wave 2 (deferred from Wave 1): CHAIN unused paid validity. An early
        # upgrade must not silently discard the remainder of a term the user
        # already paid for, so the new term is appended to it rather than
        # replacing it. Only paid ("active") time chains - a trial has no paid
        # validity to preserve. Computed BEFORE the conditional order UPDATE
        # below, whose "was still pending" guard is what keeps a replayed
        # activation from chaining a second time.
        if has_paid_term_remaining and not defer_change:
            subscription_end += (user.subscription_expires_at - now)
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
            PaymentOrder.plan_policy_snapshot_json: json.dumps(plan.policy_snapshot(), sort_keys=True, default=str),
            PaymentOrder.is_deferred_plan_change: defer_change,
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

    if defer_change:
        # Downgrade: the purchase is confirmed and recorded, but the LOWER
        # policy does not touch this account until the paid term it already
        # holds runs out. Current plan, limits, projects, media, pairs and
        # playback modes are all left exactly as they are - a downgrade is
        # never destructive and never applies retroactively.
        user.pending_plan_id = plan.id
        user.pending_plan_effective_at = user.subscription_expires_at
        if reservation:
            PaymentReservation.query.filter(
                PaymentReservation.id == reservation.id,
                PaymentReservation.status == "reserved",
            ).update({PaymentReservation.status: "activated"}, synchronize_session=False)
        db.session.commit()
        app.logger.info(
            f"payment_activated_deferred_downgrade order_id={payment_order.id} "
            f"user_id={user.id} plan_id={plan.id} effective_at={user.pending_plan_effective_at}"
        )
        return {
            "success": True,
            "order_id": payment_order.order_id,
            "plan_name": plan.plan_name,
            "replay": False,
            "deferred": True,
            "effective_at": user.pending_plan_effective_at,
        }

    user.subscription_id = plan.id
    user.subscription_taken_at = now
    user.subscription_expires_at = subscription_end
    user.subscription_status = "active"
    materialize_plan_entitlements(user, plan.total_project_limit, plan.total_scan_limit)
    # An immediate (upgrade / like-for-like) change supersedes any downgrade
    # that was still parked and waiting for the old term to end - that term no
    # longer exists in the form it was scheduled against.
    user.pending_plan_id = None
    user.pending_plan_effective_at = None
    # projects_used / scans_used are deliberately NOT reset here (P0-1).
    # They are the materialized usage counters that _reserve_project_quota_atomic
    # and _consume_scan_quota_atomic gate against inside a single conditional
    # UPDATE. Zeroing them on every paid activation handed a user a fresh full
    # allowance on top of the projects they already own, bypassing the capacity
    # gate for real, and it did so on renewal/repurchase of the SAME plan too.
    # The counters already track reality and are decremented on project delete,
    # so the correct action on activation is to leave them alone.

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
    # is_purchasable folds in the Wave 2 lifecycle gate on top of is_active, so
    # a DRAFT / CLOSED_FOR_NEW_PURCHASE / ARCHIVED plan can no longer be bought
    # by posting its id directly at this endpoint.
    if not plan or not plan.is_purchasable or plan.is_trial_plan:
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


# ---------------------------------------------------------------------------
# Razorpay webhook (server-to-server reconciliation, no browser session).
# ---------------------------------------------------------------------------
# Supported reconciliation events are payment.captured plus Razorpay's refund
# lifecycle events. order.paid remains redundant in this app's one-order /
# one-payment flow, and chargeback/settlement/subscription-renewal events stay
# acknowledged without mutation because no local entitlement contract exists for
# them.
REFUND_WEBHOOK_EVENTS = {"refund.created", "refund.processed", "refund.failed", "refund.speed_changed"}
SUPPORTED_WEBHOOK_EVENTS = {"payment.captured"} | REFUND_WEBHOOK_EVENTS


def _razorpay_webhook_signature_valid(raw_body, signature, secret):
    """HMAC-SHA256 verification via the installed SDK's own utility
    (razorpay.Utility.verify_webhook_signature), which internally uses
    hmac.compare_digest - not hand-rolled - per Razorpay's documented webhook
    verification (HMAC-SHA256 of the raw request body, keyed by the webhook
    secret). A fresh Utility() instance is used: verify_webhook_signature
    never touches self.client, so this works even when razorpay_client is
    None (API keys unset) as long as RAZORPAY_WEBHOOK_SECRET is configured -
    the API key pair and the webhook secret are independent trust boundaries.
    """
    try:
        body_str = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        razorpay.Utility().verify_webhook_signature(body_str, signature, secret)
        return True
    except razorpay.errors.SignatureVerificationError:
        return False


def _webhook_payload_hash(raw_body):
    return hashlib.sha256(raw_body).hexdigest()


def _record_webhook_event(idempotency_key, event_type, payment_id, order_id, payload_hash):
    """Insert-first DB idempotency gate. Returns (event_row, is_replay).

    The UNIQUE index on razorpay_webhook_events.idempotency_key is the real
    replay-safety mechanism - a genuine duplicate delivery fails this INSERT
    with IntegrityError, never an in-app dict/set check. On that path this
    bumps the existing row's attempt_count and returns is_replay=True so the
    caller never calls activate_payment() again for a repeat delivery.
    """
    event = RazorpayWebhookEvent(
        idempotency_key=idempotency_key,
        event_type=event_type,
        razorpay_payment_id=payment_id,
        razorpay_order_id=order_id,
        payload_hash=payload_hash,
        processing_status="received",
    )
    db.session.add(event)
    try:
        db.session.commit()
        return event, False
    except IntegrityError:
        db.session.rollback()
        existing = RazorpayWebhookEvent.query.filter_by(idempotency_key=idempotency_key).first()
        if existing:
            RazorpayWebhookEvent.query.filter(RazorpayWebhookEvent.id == existing.id).update(
                {RazorpayWebhookEvent.attempt_count: RazorpayWebhookEvent.attempt_count + 1},
                synchronize_session=False,
            )
            db.session.commit()
            db.session.refresh(existing)
        return existing, True


def _finalize_webhook_event(event, status, failure_code=None, payment_order_id=None, addon_purchase_id=None, payment_refund_id=None):
    updates = {
        RazorpayWebhookEvent.processing_status: status,
        RazorpayWebhookEvent.processed_at: dt.utcnow(),
    }
    if failure_code is not None:
        updates[RazorpayWebhookEvent.failure_code] = failure_code
    if payment_order_id is not None:
        updates[RazorpayWebhookEvent.payment_order_id] = payment_order_id
    if addon_purchase_id is not None:
        updates[RazorpayWebhookEvent.addon_purchase_id] = addon_purchase_id
    if payment_refund_id is not None:
        updates[RazorpayWebhookEvent.payment_refund_id] = payment_refund_id
    RazorpayWebhookEvent.query.filter(RazorpayWebhookEvent.id == event.id).update(
        updates, synchronize_session=False
    )
    db.session.commit()


def _process_addon_webhook_event(event, entity, payment_id, order_id):
    purchase = AddonPurchase.query.filter_by(razorpay_order_id=order_id).first()
    if not purchase:
        return False
    try:
        expected_paise = round(purchase.total_amount * 100)
        actual_paise = int(entity.get("amount"))
    except (TypeError, ValueError):
        _finalize_webhook_event(event, "failed", failure_code="amount_unreadable", addon_purchase_id=purchase.id)
        return True
    if actual_paise != expected_paise:
        _finalize_webhook_event(event, "failed", failure_code="amount_mismatch", addon_purchase_id=purchase.id)
        return True
    if (entity.get("currency") or "").upper() != (purchase.currency or "").upper():
        _finalize_webhook_event(event, "failed", failure_code="currency_mismatch", addon_purchase_id=purchase.id)
        return True
    if not purchase.razorpay_payment_id:
        purchase.razorpay_payment_id = payment_id
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            _finalize_webhook_event(event, "failed", failure_code="payment_id_conflict", addon_purchase_id=purchase.id)
            return True
    elif purchase.razorpay_payment_id != payment_id:
        _finalize_webhook_event(event, "failed", failure_code="payment_id_conflict", addon_purchase_id=purchase.id)
        return True
    result = fulfill_addon_purchase(purchase)
    if not result.get("success"):
        _finalize_webhook_event(
            event, "failed", failure_code=result.get("code", "addon_fulfillment_failed"), addon_purchase_id=purchase.id
        )
        return True
    _finalize_webhook_event(event, "processed", addon_purchase_id=purchase.id)
    return True


def _commercial_source_for_provider_payment(payment_id):
    """(PaymentOrder, AddonPurchase) matching a provider payment id.

    Deterministic only: returns (None, None) unless exactly one local source
    owns that payment id. Two matches means the correlation is ambiguous and a
    human has to decide, which is strictly better than guessing on a money row.
    """
    key = (payment_id or "").strip()
    if not key:
        return None, None
    orders = PaymentOrder.query.filter_by(razorpay_payment_id=key).all()
    purchases = AddonPurchase.query.filter_by(razorpay_payment_id=key).all()
    if len(orders) + len(purchases) != 1:
        return None, None
    return (orders[0] if orders else None), (purchases[0] if purchases else None)


def _process_refund_webhook_event(event, refund_entity, payment_entity):
    refund_id = refund_entity.get("id") if isinstance(refund_entity, dict) else None
    payment_id = refund_entity.get("payment_id") if isinstance(refund_entity, dict) else None
    provider_status = refund_entity.get("status") if isinstance(refund_entity, dict) else None
    if not refund_id or not payment_id:
        _finalize_webhook_event(event, "failed", failure_code="malformed_refund_entity")
        return True
    refund = PaymentRefund.query.filter_by(provider_refund_id=refund_id).first()
    if not refund:
        refund = PaymentRefund.query.filter_by(provider_payment_id=payment_id, status="REFUND_PROCESSING").first()
    if not refund:
        # OUT-OF-BAND REFUND (V1.1 P0-1 case 5). Somebody refunded from the
        # provider dashboard, so there is no local PaymentRefund to update. We
        # do NOT invent one: PaymentRefund.requested_by_admin_id is the record
        # of who authorised the refund and fabricating an admin there would
        # corrupt the audit trail, and reversing entitlements off a dashboard
        # action is a business decision no webhook may take. What we can do
        # deterministically is correlate the provider payment id back to the
        # local commercial source and record that link on the event, so the
        # refund is visible and auditable (`flask reconcile-refunds` lists it)
        # instead of being dropped as "unknown".
        order, purchase = _commercial_source_for_provider_payment(payment_id)
        if order is not None or purchase is not None:
            _finalize_webhook_event(
                event,
                "failed",
                failure_code=OUT_OF_BAND_REFUND_FAILURE_CODE,
                payment_order_id=order.id if order else None,
                addon_purchase_id=purchase.id if purchase else None,
            )
        else:
            _finalize_webhook_event(event, "failed", failure_code="unknown_refund")
        return True
    try:
        expected_paise = _refund_amount_paise(refund.amount)
        actual_paise = int(refund_entity.get("amount"))
    except (TypeError, ValueError):
        _finalize_webhook_event(event, "failed", failure_code="refund_amount_unreadable", payment_refund_id=refund.id)
        return True
    if actual_paise != expected_paise:
        _finalize_webhook_event(event, "failed", failure_code="refund_amount_mismatch", payment_refund_id=refund.id)
        return True
    if refund_entity.get("currency") and (refund_entity.get("currency") or "").upper() != (refund.currency or "").upper():
        _finalize_webhook_event(event, "failed", failure_code="refund_currency_mismatch", payment_refund_id=refund.id)
        return True
    if payment_entity and payment_entity.get("id") and payment_entity.get("id") != refund.provider_payment_id:
        _finalize_webhook_event(event, "failed", failure_code="refund_payment_mismatch", payment_refund_id=refund.id)
        return True

    if event.event_type == "refund.speed_changed":
        refund.provider_refund_id = refund.provider_refund_id or refund_id
        refund.provider_status = provider_status or refund.provider_status
        db.session.commit()
        _finalize_webhook_event(event, "ignored", failure_code="refund_speed_changed", payment_refund_id=refund.id)
        return True

    mark_refund_provider_result(
        refund,
        provider_refund_id=refund_id,
        provider_status=provider_status,
        failure_code="PROVIDER_REFUND_FAILED" if event.event_type == "refund.failed" else None,
        failure_message_safe="Provider reported refund failure." if event.event_type == "refund.failed" else None,
    )
    db.session.commit()
    if refund.status == "REFUNDED":
        log_admin_activity(
            refund.requested_by_admin_id,
            "refund_confirmed",
            f"Refund {refund.id} confirmed via webhook.",
        )
        log_admin_activity(
            refund.requested_by_admin_id,
            "refund_reconciliation",
            f"Refund {refund.id} reconciliation status {refund.reconciliation_status}.",
        )
    elif refund.status == "REFUND_FAILED":
        log_admin_activity(
            refund.requested_by_admin_id,
            "refund_failed",
            f"Refund {refund.id} failed via webhook.",
        )
    _finalize_webhook_event(event, "processed", payment_refund_id=refund.id)
    return True


@app.route("/webhooks/razorpay", methods=["POST"])
@csrf.exempt  # Provider server-to-server webhook, no browser session/cookie to bind a CSRF token to - authenticity is enforced by HMAC-SHA256 signature verification (RAZORPAY_WEBHOOK_SECRET) below instead.
def razorpay_webhook():
    """Razorpay payment webhook. Session-independent (no current_user() /
    login helper anywhere in this function) and routes every successful
    reconciliation through the SAME activate_payment() service /verify-payment
    uses - it never reimplements activation.

    Deliberately NOT rate-limited by request_limiter: Razorpay legitimately
    retries from shared/rotating IPs, and signature verification + the DB
    unique-index idempotency gate (not an in-process lock, which would be
    useless across Gunicorn workers) are the real controls here.
    """
    t_start = time.time()
    raw_body = request.get_data()  # RAW bytes - verified before any JSON parsing, never re-serialized.
    signature = request.headers.get("X-Razorpay-Signature")

    if not RAZORPAY_WEBHOOK_SECRET:
        # Fail closed: an unconfigured secret means "process nothing", never
        # "skip verification".
        app.logger.warning("razorpay_webhook_rejected reason=secret_not_configured")
        return jsonify({"error": "webhook_not_configured"}), 400

    if not signature:
        app.logger.warning("razorpay_webhook_rejected reason=missing_signature")
        return jsonify({"error": "missing_signature"}), 400

    if not _razorpay_webhook_signature_valid(raw_body, signature, RAZORPAY_WEBHOOK_SECRET):
        app.logger.warning("razorpay_webhook_rejected reason=invalid_signature")
        return jsonify({"error": "invalid_signature"}), 400

    # Only now, after verified authenticity, is the body treated as JSON.
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError("payload is not a JSON object")
    except ValueError:
        app.logger.warning("razorpay_webhook_rejected reason=malformed_json")
        return jsonify({"error": "invalid_payload"}), 400

    event_type = payload.get("event")
    payload_hash = _webhook_payload_hash(raw_body)

    payment_id = None
    order_id = None
    entity = None
    refund_entity = None
    payment_entity = None
    if event_type == "payment.captured":
        try:
            entity = payload["payload"]["payment"]["entity"]
            payment_id = entity.get("id")
            order_id = entity.get("order_id")
        except (KeyError, TypeError, AttributeError):
            entity = None
    elif event_type in REFUND_WEBHOOK_EVENTS:
        try:
            refund_entity = payload["payload"]["refund"]["entity"]
            payment_entity = payload.get("payload", {}).get("payment", {}).get("entity")
            payment_id = refund_entity.get("payment_id")
            order_id = payment_entity.get("order_id") if isinstance(payment_entity, dict) else None
            entity = refund_entity
        except (KeyError, TypeError, AttributeError):
            refund_entity = None

    if event_type == "payment.captured" and payment_id and order_id:
        # Stable across Razorpay's own retries of the same logical event,
        # even if the retry re-sends a byte-different body (e.g. different
        # created_at) - see models.py's RazorpayWebhookEvent docstring.
        idempotency_key = f"{event_type}|{payment_id}|{order_id}"
    elif event_type in REFUND_WEBHOOK_EVENTS and refund_entity and refund_entity.get("id") and payment_id:
        idempotency_key = f"{event_type}|{refund_entity.get('id')}|{payment_id}"
    else:
        # No reconciliation is performed for any other event type, so a
        # payload-hash-derived fallback key is sufficient here.
        idempotency_key = f"{event_type}|{payload_hash}"

    event, is_replay = _record_webhook_event(idempotency_key, event_type, payment_id, order_id, payload_hash)

    if is_replay:
        app.logger.info(
            f"razorpay_webhook_replay event_id={event.id if event else None} event_type={event_type} "
            f"attempt_count={event.attempt_count if event else None}"
        )
        return jsonify({"status": "ok", "replay": True}), 200

    if event_type not in SUPPORTED_WEBHOOK_EVENTS:
        _finalize_webhook_event(event, "ignored", failure_code="unsupported_event_type")
        app.logger.info(f"razorpay_webhook_ignored event_id={event.id} event_type={event_type} reason=unsupported")
        return jsonify({"status": "ok"}), 200

    if event_type in REFUND_WEBHOOK_EVENTS:
        if refund_entity is None:
            _finalize_webhook_event(event, "failed", failure_code="malformed_refund_entity")
            return jsonify({"status": "ok"}), 200
        _process_refund_webhook_event(event, refund_entity, payment_entity)
        return jsonify({"status": "ok"}), 200

    if entity is None:
        _finalize_webhook_event(event, "failed", failure_code="malformed_entity")
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} event_type={event_type} reason=malformed_entity")
        return jsonify({"status": "ok"}), 200

    if entity.get("status") != "captured":
        _finalize_webhook_event(event, "ignored", failure_code="not_captured")
        app.logger.info(f"razorpay_webhook_ignored event_id={event.id} event_type={event_type} reason=not_captured")
        return jsonify({"status": "ok"}), 200

    payment_order = PaymentOrder.query.filter_by(razorpay_order_id=order_id).first()
    if not payment_order:
        if _process_addon_webhook_event(event, entity, payment_id, order_id):
            return jsonify({"status": "ok"}), 200
        # Never create an entitlement/order from an unknown external order.
        _finalize_webhook_event(event, "failed", failure_code="unknown_order")
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} reason=unknown_order")
        return jsonify({"status": "ok"}), 200

    plan = SubscriptionPlan.query.get(payment_order.plan_id)
    if not plan:
        _finalize_webhook_event(event, "failed", failure_code="plan_not_found", payment_order_id=payment_order.id)
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason=plan_not_found")
        return jsonify({"status": "ok"}), 200

    # Never trust webhook-supplied amount/currency - the stored PaymentOrder
    # row is authoritative (same principle as verify_payment()'s checks).
    try:
        expected_paise = round(payment_order.total_amount * 100)
        actual_paise = int(entity.get("amount"))
    except (TypeError, ValueError):
        _finalize_webhook_event(event, "failed", failure_code="amount_unreadable", payment_order_id=payment_order.id)
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason=amount_unreadable")
        return jsonify({"status": "ok"}), 200

    if actual_paise != expected_paise:
        _finalize_webhook_event(event, "failed", failure_code="amount_mismatch", payment_order_id=payment_order.id)
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason=amount_mismatch")
        return jsonify({"status": "ok"}), 200

    if (entity.get("currency") or "").upper() != (payment_order.currency or "").upper():
        _finalize_webhook_event(event, "failed", failure_code="currency_mismatch", payment_order_id=payment_order.id)
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason=currency_mismatch")
        return jsonify({"status": "ok"}), 200

    # Persist razorpay_payment_id if the browser path hasn't already (DB
    # unique constraint guards a genuine conflict, same as verify_payment()).
    if not payment_order.razorpay_payment_id:
        payment_order.razorpay_payment_id = payment_id
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            _finalize_webhook_event(event, "failed", failure_code="payment_id_conflict", payment_order_id=payment_order.id)
            app.logger.warning(f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason=payment_id_conflict")
            return jsonify({"status": "ok"}), 200
    elif payment_order.razorpay_payment_id != payment_id:
        _finalize_webhook_event(event, "failed", failure_code="payment_id_conflict", payment_order_id=payment_order.id)
        app.logger.warning(f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason=payment_id_conflict")
        return jsonify({"status": "ok"}), 200

    # THE single shared activation service (area 1) - never reimplemented
    # here. Its own atomic conditional UPDATE is what makes the
    # browser-vs-webhook race safe, combined with this route's DB-unique
    # idempotency gate above.
    result = activate_payment(payment_order)

    if not result["success"]:
        _finalize_webhook_event(
            event, "failed", failure_code=result.get("code", "activation_failed"), payment_order_id=payment_order.id
        )
        app.logger.warning(
            f"razorpay_webhook_failed event_id={event.id} order_id={payment_order.id} reason={result.get('code')}"
        )
        return jsonify({"status": "ok"}), 200

    if not result.get("replay"):
        try:
            user = User.query.get(payment_order.user_id)
            send_payment_success_email(user, plan, payment_order)
        except Exception:
            app.logger.warning(f"razorpay_webhook_email_failed order_id={payment_order.id}")

    _finalize_webhook_event(event, "processed", payment_order_id=payment_order.id)
    latency_ms = int((time.time() - t_start) * 1000)
    app.logger.info(
        f"razorpay_webhook_processed event_id={event.id} event_type={event_type} order_id={payment_order.id} "
        f"replay={result.get('replay')} latency_ms={latency_ms}"
    )
    return jsonify({"status": "ok"}), 200


@app.cli.command("webhook-events-status")
@click.option("--limit", default=20, show_default=True, help="Max rows to show.")
def webhook_events_status(limit):
    """Report recent failed/unprocessed Razorpay webhook events (read-only)."""
    rows = (
        RazorpayWebhookEvent.query
        .filter(RazorpayWebhookEvent.processing_status.in_(("received", "failed")))
        .order_by(RazorpayWebhookEvent.received_at.desc())
        .limit(limit)
        .all()
    )
    click.echo(f"Events shown (received/failed, most recent first): {len(rows)}")
    for row in rows:
        click.echo(
            f"event_id={row.id} type={row.event_type} status={row.processing_status} "
            f"failure_code={row.failure_code} payment_order_id={row.payment_order_id} "
            f"attempts={row.attempt_count} received_at={row.received_at}"
        )


@app.cli.command("reconcile-refunds")
@click.option("--apply", "apply_changes", is_flag=True, default=False, help="Re-drive recoverable refunds. Default is dry-run.")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Report only (the default).")
@click.option("--refund-id", "refund_id", type=int, default=None, help="Narrow to a single PaymentRefund id.")
@click.option(
    "--source", "source_kind",
    type=click.Choice(["payment_order", "addon_purchase"]),
    default=None,
    help="Narrow to one commercial source kind.",
)
def reconcile_refunds_command(apply_changes, dry_run, refund_id, source_kind):
    """Report and optionally recover refunds stuck mid-flight (V1.1 P0-1).

    READ-ONLY BY DEFAULT. --apply re-drives each recoverable refund on its
    EXISTING record: it never creates a second refund record, never issues a
    second provider refund without first reading the provider's own refund list
    for that payment, never reverses entitlements before the provider confirms,
    and never deletes media. Anything it cannot resolve safely is reported as
    unresolved for a human, and the command exits non-zero so a scheduled run
    cannot look clean while money is stuck.
    """
    if dry_run and apply_changes:
        raise click.UsageError("--dry-run and --apply are mutually exclusive.")

    query = stuck_refund_query()
    if refund_id is not None:
        query = query.filter(PaymentRefund.id == refund_id)
    if source_kind == "payment_order":
        query = query.filter(PaymentRefund.payment_order_id.isnot(None))
    elif source_kind == "addon_purchase":
        query = query.filter(PaymentRefund.addon_purchase_id.isnot(None))
    refund_ids = [row.id for row in query.all()]

    if refund_id is not None and not refund_ids:
        existing = PaymentRefund.query.get(refund_id)
        if not existing:
            raise click.ClickException(f"No PaymentRefund found with id={refund_id}")

    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"Refunds needing attention: {len(refund_ids)}")

    buckets = {
        "failed_provider_attempt": [],
        "processing": [],
        "refunded_reconciliation_failed": [],
        "manual_review": [],
    }
    outcomes = {}
    errors = []

    for candidate_id in refund_ids:
        db.session.rollback()
        refund = PaymentRefund.query.get(candidate_id)
        if refund is None:
            continue
        if refund.status == "REFUND_FAILED":
            buckets["failed_provider_attempt"].append(refund.id)
        elif refund.status in ("REFUND_REQUESTED", "REFUND_PROCESSING"):
            buckets["processing"].append(refund.id)
        elif refund.reconciliation_status == "MANUAL_REVIEW_REQUIRED":
            buckets["manual_review"].append(refund.id)
        else:
            buckets["refunded_reconciliation_failed"].append(refund.id)

        try:
            result = recover_payment_refund(refund, admin=None, apply_changes=apply_changes)
        except Exception:
            db.session.rollback()
            app.logger.exception("reconcile_refunds_failed refund_id=%s", candidate_id)
            errors.append(candidate_id)
            continue
        outcomes.setdefault(result["outcome"], []).append(refund.id)
        click.echo(
            f"  refund_id={result['refund_id']} source={result['source']} "
            f"outcome={result['outcome']} status={result['status']} "
            f"reconciliation={result['reconciliation_status']} - {result['message']}"
        )

    for label, key in (
        ("Failed provider attempts", "failed_provider_attempt"),
        ("Provider outcome still open (requested/processing)", "processing"),
        ("Refunded but local reconciliation not applied", "refunded_reconciliation_failed"),
        ("Parked for manual review", "manual_review"),
    ):
        click.echo(f"{label}: {len(buckets[key])}")

    recovered = sum(len(v) for k, v in outcomes.items() if k in ("reconciled", "retried", "adopted_provider_state"))
    unresolved = sum(len(v) for k, v in outcomes.items() if k in REFUND_RECOVERY_UNRESOLVED_OUTCOMES)
    click.echo(f"Recovered: {recovered}")
    click.echo(f"Unresolved (human action required): {unresolved}")
    click.echo(f"Errors: {len(errors)}")
    for outcome, ids in sorted(outcomes.items()):
        click.echo(f"  outcome {outcome}: {len(ids)}")

    out_of_band = unlinked_out_of_band_refund_events()
    click.echo(f"Out-of-band provider refunds with no local record: {len(out_of_band)}")
    for event in out_of_band[:20]:
        click.echo(
            f"  event_id={event.id} type={event.event_type} "
            f"payment_order_id={event.payment_order_id} addon_purchase_id={event.addon_purchase_id}"
        )

    if not apply_changes:
        click.echo("Dry run: nothing was written. Re-run with --apply to re-drive these refunds.")

    if errors or unresolved or out_of_band:
        raise SystemExit(1)


@app.cli.command("reconcile-order-webhooks")
@click.argument("order_id")
def reconcile_order_webhooks(order_id):
    """Report the webhook event history for one stored PaymentOrder.order_id (read-only)."""
    order = PaymentOrder.query.filter_by(order_id=order_id).first()
    if not order:
        raise click.ClickException(f"No PaymentOrder found with order_id={order_id}")
    events = (
        RazorpayWebhookEvent.query.filter_by(payment_order_id=order.id)
        .order_by(RazorpayWebhookEvent.received_at.asc())
        .all()
    )
    click.echo(f"PaymentOrder id={order.id} order_id={order.order_id} status={order.status}")
    click.echo(f"Webhook events linked: {len(events)}")
    for row in events:
        click.echo(
            f"event_id={row.id} type={row.event_type} status={row.processing_status} "
            f"failure_code={row.failure_code} attempts={row.attempt_count}"
        )


@app.cli.command("webhook-replay-report")
def webhook_replay_report():
    """Report total replay/duplicate webhook deliveries observed (read-only)."""
    rows = RazorpayWebhookEvent.query.all()
    total_replays = sum(max(0, (row.attempt_count or 1) - 1) for row in rows)
    click.echo(f"Distinct webhook events recorded: {len(rows)}")
    click.echo(f"Total replay/duplicate deliveries observed: {total_replays}")


@app.cli.command("reconcile-payment-activations")
@click.option("--apply", "apply_changes", is_flag=True, help="Activate eligible pending orders. Default is dry-run.")
def reconcile_payment_activations(apply_changes):
    """Recover pending orders whose verified Razorpay payment id is already
    stored, but whose subscription activation did not complete.

    The command never accepts CLI-supplied payment proof and never sends
    success email; activate_payment() remains the only entitlement path.
    """
    candidate_ids = [
        row.id for row in PaymentOrder.query.filter(
            PaymentOrder.status == "pending",
            PaymentOrder.razorpay_payment_id.isnot(None),
            PaymentOrder.razorpay_payment_id != "",
        ).order_by(PaymentOrder.id.asc()).all()
    ]
    counts = {"candidates": len(candidate_ids), "activated": 0, "skipped": 0, "failed": 0}

    click.echo("Mode: apply" if apply_changes else "Mode: dry-run")
    click.echo(f"Candidates: {counts['candidates']}")

    for order_id in candidate_ids:
        try:
            db.session.rollback()
            order = PaymentOrder.query.get(order_id)
            if (
                not order
                or order.status != "pending"
                or not (order.razorpay_payment_id or "").strip()
            ):
                counts["skipped"] += 1
                click.echo(f"payment_order_id={order_id} skipped: no_longer_eligible")
                continue

            reservation = PaymentReservation.query.filter_by(payment_order_id=order.id).first()
            if reservation and reservation.status in ("released", "expired"):
                counts["skipped"] += 1
                click.echo(f"payment_order_id={order.id} skipped: reservation_{reservation.status}")
                continue
            if reservation and reservation.status == "reserved" and reservation.expires_at < dt.utcnow():
                counts["skipped"] += 1
                click.echo(f"payment_order_id={order.id} skipped: reservation_expired")
                continue

            if not apply_changes:
                counts["skipped"] += 1
                click.echo(f"payment_order_id={order.id} dry-run: eligible")
                continue

            result = activate_payment(order)
            if result.get("success") and result.get("replay"):
                counts["skipped"] += 1
                click.echo(f"payment_order_id={order.id} skipped: replay")
            elif result.get("success"):
                counts["activated"] += 1
                click.echo(f"payment_order_id={order.id} activated")
            else:
                code = result.get("code") or "ACTIVATION_FAILED"
                if code == "ORDER_NOT_PENDING":
                    counts["skipped"] += 1
                    click.echo(f"payment_order_id={order.id} skipped: {code}")
                else:
                    counts["failed"] += 1
                    click.echo(f"payment_order_id={order.id} failed: {code}")
        except Exception as exc:
            db.session.rollback()
            counts["failed"] += 1
            app.logger.exception(f"payment_activation_reconcile_failed payment_order_id={order_id}")
            click.echo(f"payment_order_id={order_id} failed: {safe_error_summary(exc)}")

    click.echo(f"Activated: {counts['activated']}")
    click.echo(f"Skipped: {counts['skipped']}")
    click.echo(f"Failed: {counts['failed']}")


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
    
    if not user_can_manage_project(user, project):
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
    if not pair.image_filename:
        abort(404)
    
    response = send_from_directory(IMAGES_DIR, pair.image_filename)
    return _apply_short_public_cache(response)

@app.route("/qr/<filename>")
def serve_qr(filename):
    project = _project_from_qr_filename(filename, admin_project=False)
    if project and not _project_is_available(project):
        return _project_unavailable_response()
    response = send_from_directory(QR_DIR, filename)
    return _apply_short_public_cache(response)


@app.route("/api/scanner/<int:project_id>/fallback-video")
def scanner_fallback_video(project_id):
    """Public, unauthenticated: resolve the EXPLICIT fallback video (if any) a
    scanner client may offer after a recognition timeout / camera failure - see
    resolve_scanner_fallback_video() above. Available only when the project
    creator configured `fallback_pair_id` and that pair's video is servable;
    an ordinary matched pair is never an implicit fallback candidate (Fix 6,
    V1 Agent 2). No `pair_index` hint is accepted here anymore - it had no
    remaining legitimate purpose once fallback became explicit-only, and a
    stale client still sending one is silently ignored rather than erroring.

    Availability check mirrors serve_video()/serve_image()/serve_qr() above
    exactly (_project_is_available): a suspended/inactive project is
    unavailable here too. Shaped as JSON (matching this route's own JSON
    contract, and detect_init's/detect_track's existing JSON-404 style for
    the same suspended-project case) rather than those routes' plain-text
    body - the underlying availability check is identical either way.
    """
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"available": False, "error": "NOT_FOUND"}), 404
    if not _project_is_available(project):
        return jsonify({"available": False, "error": "PROJECT_UNAVAILABLE"}), 404

    ok, retry_after = _check_rate_limit(
        "scanner_fallback",
        _rate_limit_key("fallback_video", project_id),
    )
    if not ok:
        return _scanner_rate_limited_response(retry_after)

    return jsonify(resolve_scanner_fallback_video(project))


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
        if session_user_id and payload.get("user_id") == session_user_id and project_current_owner_user_id(project) == session_user_id:
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
    if not user_can_manage_project(user, project):
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
    project_owner_id = project_current_owner_user_id(project)
    if project_owner_id:
        creator_type = "user"
        owner_user = User.query.get(project_owner_id)
        creator_name = owner_user.full_name if owner_user else "User"
    else:
        creator_type = "admin"
        creator_name = project.owner_admin.name if project.owner_admin else "Admin"

    # V1.1 viewer: the target guide needs to show what the viewer should point the camera
    # at, and Direct QR Video needs a video to play without any camera at all. Both are
    # read-only projections of pairs that are already public via /image and /video.
    experience_type = project_experience_type(project)
    playback_mode = project_playback_mode(project)
    pairs = (
        ProjectPair.query.filter_by(project_id=project.id)
        .order_by(ProjectPair.pair_index)
        .all()
    )
    targets = [
        {
            "index": pair.pair_index,
            "image_url": url_for("serve_image", project_id=project.id, image_id=pair.pair_index),
            "video_url": url_for("serve_video", project_id=project.id, image_id=pair.pair_index),
            "label": "Target {}".format(pair.pair_index + 1),
        }
        for pair in pairs
    ]

    return render_template(
        "user/scanner.html",
        project_id=project_id,
        project_name=project.name,
        qr_code_url=project.qr_code_path,
        creator_type=creator_type,
        creator_name=creator_name,
        experience_type=experience_type,
        playback_mode=playback_mode,
        targets=targets,
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
        scan_attribution_owner_id = project_current_owner_user_id(project)
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

        def _scanner_guidance(**extra):
            brightness = frame_diag.get("brightness_score")
            payload = {
                "blur_score": frame_diag.get("blur_score"),
                "brightness_score": brightness,
                "likely_blurry": bool(frame_diag.get("likely_blurry")),
                "likely_glare_or_dark": bool(frame_diag.get("likely_glare_or_dark")),
                "likely_dark": bool(brightness is not None and brightness < 25.0),
                "likely_glare": bool(brightness is not None and brightness > 235.0),
            }
            for key in ("raw_keypoints", "quick_score_candidates", "good_matches", "inliers", "frame_visibility"):
                if key in extra:
                    payload[key] = extra[key]
            return payload

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
                "scanner_guidance": _scanner_guidance(raw_keypoints=len(test_kp) if test_kp else 0),
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
            _log_scanner_latency(
                "detect_init", t_start, project_id=project_id, outcome="no_match", stage="response", scan_session_id=scan_session_id,
                stage_timings={
                    "read": t_after_read - t_start,
                    "prep": t_after_prep - t_after_read,
                    "detect": t_after_detect - t_after_prep,
                    "quick_score": t_after_quick - t_after_detect,
                    "match": t_after_match - t_after_quick,
                },
            )
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
                "scanner_guidance": _scanner_guidance(
                    raw_keypoints=len(test_kp), quick_score_candidates=len(scored), good_matches=best_good
                ),
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
                "scanner_guidance": _scanner_guidance(
                    raw_keypoints=len(test_kp), quick_score_candidates=len(scored), good_matches=best_good
                ),
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
                "scanner_guidance": _scanner_guidance(
                    raw_keypoints=len(test_kp),
                    quick_score_candidates=len(scored),
                    good_matches=best_good,
                    inliers=inliers,
                    frame_visibility="partial" if homography_quality.get("code") in {"quad_out_of_bounds", "visible_area_too_small"} else "unknown",
                ),
            }), 200

        rect = np.array([[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32).reshape(-1, 1, 2)
        pts = cv2.perspectiveTransform(rect, H).reshape(4, 2)
        corners = [(float(p[0] / scale), float(p[1] / scale)) for p in pts]
        
        if not valid_corners(corners, frame_w, frame_h):
            print(f"❌ Bad corners")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({
                "detected": False,
                "reason": "Bad corners",
                "scanner_guidance": _scanner_guidance(
                    raw_keypoints=len(test_kp),
                    quick_score_candidates=len(scored),
                    good_matches=best_good,
                    inliers=inliers,
                    frame_visibility="partial",
                ),
            }), 200
        
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
        _log_scanner_latency(
            "detect_init", t_start, project_id=project_id, outcome="accepted", stage="response", scan_session_id=scan_session_id,
            stage_timings={
                "read": t_after_read - t_start,
                "prep": t_after_prep - t_after_read,
                "detect": t_after_detect - t_after_prep,
                "quick_score": t_after_quick - t_after_detect,
                "match": t_after_match - t_after_quick,
                "homography": t_after_homography - t_after_match,
            },
        )
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
            "scanner_guidance": _scanner_guidance(
                raw_keypoints=len(test_kp),
                quick_score_candidates=len(scored),
                good_matches=best_good,
                inliers=inliers,
                frame_visibility="full",
            ),
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
        scan_attribution_owner_id = project_current_owner_user_id(project) if project else None

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


@app.route("/api/scanner/<int:project_id>/fallback-event", methods=["POST"])
@csrf.exempt  # Public, unauthenticated scanner endpoint - no browser session/cookie to bind a CSRF token to (same class as detect_init/detect_track/session/end above).
def scanner_fallback_event(project_id):
    """Records one fallback/analytics event: pair_fallback_view,
    project_fallback_view, recognition_timeout, or camera_unavailable (V1
    Wave 6). Writes to the dedicated `scan_events` table ONLY - never to
    ScanLog, never touches is_successful/counted - a fallback view must be
    structurally impossible to be counted as a successful scan anywhere
    ScanLog is aggregated (admin dashboards, project.scan_count, quota
    counters). See ScanEvent's docstring in models.py for the full
    reasoning.

    Idempotent: a client-generated `client_event_id` (UUID) is the sole
    idempotency key, enforced by scan_events' DB-level UNIQUE constraint - a
    flaky-network retry that resends the same client_event_id gets a safe
    `"duplicate": true` response instead of a second row.

    "matched_scan" is deliberately NOT an accepted event_type here - a real
    detection+overlay match can only ever be recorded by the server-side
    detect_track()/detect_init() path itself, never claimed by a client
    POST to this endpoint.
    """
    project = Project.query.get(project_id)
    if not project:
        return jsonify({"success": False, "code": "NOT_FOUND", "error": "Project not found."}), 404
    if not _project_is_available(project):
        return jsonify({"success": False, "code": "PROJECT_UNAVAILABLE", "error": "This project is currently suspended or unavailable."}), 404

    if request.is_json:
        data = request.get_json()
    else:
        data = request.form
    if not data:
        return jsonify({"success": False, "code": "INVALID_REQUEST", "error": "Missing request body."}), 400

    ok, retry_after = _check_rate_limit(
        "scanner_fallback_event",
        _rate_limit_key("fallback_event", project_id, data.get("scan_session_id")),
    )
    if not ok:
        return _scanner_rate_limited_response(retry_after)

    event_type = (data.get("event_type") or "").strip()
    client_event_id = (data.get("client_event_id") or "").strip()
    scan_session_id = data.get("scan_session_id") or None
    raw_pair_index = data.get("pair_index")
    try:
        pair_index = int(raw_pair_index) if raw_pair_index not in (None, "") else None
    except (TypeError, ValueError):
        pair_index = None

    if event_type not in SCAN_EVENT_TYPES:
        return jsonify({
            "success": False,
            "code": "INVALID_EVENT_TYPE",
            "error": f"event_type must be one of: {', '.join(sorted(SCAN_EVENT_TYPES))}",
        }), 400
    if not client_event_id or len(client_event_id) > 36:
        return jsonify({
            "success": False,
            "code": "MISSING_CLIENT_EVENT_ID",
            "error": "client_event_id (a client-generated UUID) is required.",
        }), 400

    # An unrecognized pair_index for THIS project is silently dropped (pair
    # context becomes None) rather than rejected outright - this is a
    # best-effort analytics event, not a media-serving lookup, and a stale
    # client-side pair_index should never block recording that a fallback
    # genuinely happened. Scoping to project_id=project.id here is what
    # prevents a pair_index from resolving to a different project's pair.
    pair_id = None
    if pair_index is not None:
        pair = ProjectPair.query.filter_by(project_id=project.id, pair_index=pair_index).first()
        if pair:
            pair_id = pair.id

    event = ScanEvent(
        project_id=project.id,
        pair_id=pair_id,
        event_type=event_type,
        scan_session_id=scan_session_id,
        client_event_id=client_event_id,
    )
    db.session.add(event)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = ScanEvent.query.filter_by(client_event_id=client_event_id).first()
        if not existing:
            raise
        return jsonify({
            "success": True,
            "duplicate": True,
            "event": {
                "id": existing.id,
                "event_type": existing.event_type,
                "created_at": existing.created_at.isoformat(),
            },
        }), 200

    return jsonify({
        "success": True,
        "duplicate": False,
        "event": {
            "id": event.id,
            "event_type": event.event_type,
            "created_at": event.created_at.isoformat(),
        },
    }), 201


# Correction 2 (V1 Agent 2): retry_success renamed to user_retry_success and
# first_attempt_failure added - the retry shape changed from "up to 3 blind automatic
# attempts" to "1 automatic attempt, then every further attempt is a user-initiated Retry
# Camera click," so telemetry now distinguishes the automatic first attempt's own failure
# (first_attempt_failure - the user is shown the retry/fallback choice, not yet fully
# terminal) from a user-initiated retry also failing (terminal_failure).
OPENCV_TELEMETRY_OUTCOMES = {
    "first_attempt_success", "user_retry_success", "first_attempt_failure", "terminal_failure",
}


@app.route("/api/scanner/<int:project_id>/opencv-telemetry", methods=["POST"])
@csrf.exempt  # Public, unauthenticated scanner endpoint - sent via navigator.sendBeacon, same
              # class/no-cookie-CSRF-binding reasoning as scanner_session_end/fallback_event above.
def scanner_opencv_telemetry(project_id):
    """Lightweight, low-cardinality OpenCV cold-start outcome sink (V1 Agent 2, Fix 4).

    Fire-and-forget, best-effort observability only - never touches ScanLog/ScanEvent,
    never affects scan counting or fallback resolution, and writes no DB row (this is a
    log line, not a table). Deliberately narrow: just enough to answer "did the vision
    engine load, on which attempt, how long did it take, and what was the device/network
    context" without becoming a general analytics subsystem.
    """
    project = Project.query.get(project_id)
    if not project or not _project_is_available(project):
        # Best-effort telemetry for a project that no longer resolves - still 200 so the
        # client's sendBeacon isn't treated as a delivery failure, just nothing to log against.
        return jsonify({"ok": True, "logged": False}), 200

    ok, retry_after = _check_rate_limit(
        "scanner_opencv_telemetry",
        _rate_limit_key("opencv_telemetry", project_id),
    )
    if not ok:
        return _scanner_rate_limited_response(retry_after)

    data = request.get_json(silent=True) if request.is_json else request.form
    if not data:
        return jsonify({"ok": False, "error": "Missing request body."}), 400

    outcome = (data.get("outcome") or "").strip()
    if outcome not in OPENCV_TELEMETRY_OUTCOMES:
        return jsonify({"ok": False, "error": "outcome must be one of: " + ", ".join(sorted(OPENCV_TELEMETRY_OUTCOMES))}), 400

    def _clamp_int(value, lo, hi, default=None):
        if value in (None, ""):
            return default
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    def _parse_bool(value):
        # Client may send this via form-encoded sendBeacon (booleans arrive as the
        # strings "true"/"false") or JSON (real booleans) - handle both, never let a
        # truthy non-empty string like "false" evaluate to True.
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == "true"

    safe = {
        "event": "scanner_opencv_load",
        "project_id": project_id,
        "outcome": outcome,
        "attempt_count": _clamp_int(data.get("attempt_count"), 0, 10, default=0),
        "total_duration_ms": _clamp_int(data.get("total_duration_ms"), 0, 300000, default=0),
        "sw_controller": _parse_bool(data.get("sw_controller")),
        "device_memory": _clamp_int(data.get("device_memory"), 0, 128),
        "hardware_concurrency": _clamp_int(data.get("hardware_concurrency"), 0, 128),
        "connection_effective_type": (str(data.get("connection_effective_type") or "")[:16] or None),
        "scan_session_id": (str(data.get("scan_session_id") or "")[:64] or None),
    }
    app.logger.info("scanner_opencv_telemetry", extra={"scanner_opencv_telemetry": safe})
    return jsonify({"ok": True, "logged": True}), 200


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
        
        _log_scanner_latency(
            "detect_track", t_start, project_id=project_id, pair_id=pair_id, outcome="accepted", stage="response", scan_session_id=scan_session_id,
            stage_timings={
                "read": t_after_read - t_start,
                "prep": t_after_prep - t_after_read,
                "detect": t_after_detect - t_after_prep,
                "match": t_after_match - t_after_detect,
                "homography": t_after_homography - t_after_match,
            },
        )
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
        if not user_can_manage_project(user, project):
            abort(404)
    
    pairs = ProjectPair.query.filter_by(project_id=project.id).order_by(ProjectPair.pair_index).all()
    
    return render_template("user/project_preview.html",
                         user=user,
                         project=project,
                         pairs=pairs,
                         admin_view=admin_view,
                         coverage=project_coverage_summary(project),
                         ownership=project_ownership_context(project, user))

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

    # P0-8: request-layer limiting before any DB lookup or password hashing.
    # Only the (hashed) email identity is ever used in a key - never the
    # submitted password.
    ok, retry_after = _check_rate_limit("admin_login_ip", _rate_limit_key("admin_login"))
    if ok:
        ok, retry_after = _check_rate_limit(
            "admin_login_identity",
            _rate_limit_key("admin_login_identity", identity_digest(email)),
        )
    if not ok:
        return _rate_limited_html("admin/login.html", retry_after)

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

    # P0-8: this route calls _create_otp directly, bypassing the _resend_otp
    # throttles, so without a limit it is an unlimited authenticated-mail
    # trigger (mail bomb + OTP churn). Limited before any OTP is created.
    ok, retry_after = _check_rate_limit(
        "admin_forgot_password_ip", _rate_limit_key("admin_forgot_password")
    )
    if ok:
        ok, retry_after = _check_rate_limit(
            "admin_forgot_password_identity",
            _rate_limit_key("admin_forgot_password_identity", identity_digest(email)),
        )
    if not ok:
        return _rate_limited_html(
            "admin/forgot_password.html", retry_after,
            "Too many password reset requests. Please try again later.",
        )

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
    projects = Project.query.filter(project_user_access_filter(user.id)).order_by(Project.created_at.desc()).all()
    
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
                         trial=trial,
                         entitlement_summary=user_entitlement_summary(user))

@app.route("/admin/users/<int:user_id>/dashboard", methods=["GET"])
@require_admin_permission("admin.users.view")
def admin_view_user_dashboard(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    trial = TrialDetails.query.filter_by(user_id=user.id).first()
    total_projects = Project.query.filter(project_user_access_filter(user.id)).count()
    total_pairs = (
        db.session.query(func.count(ProjectPair.id))
        .join(Project, ProjectPair.project_id == Project.id)
        .filter(project_user_access_filter(user.id))
        .scalar()
        or 0
    )
    total_scans = ScanLog.query.filter_by(user_id=user.id).count()
    successful_scans = ScanLog.query.filter_by(user_id=user.id, is_successful=True).count()
    failed_scans = ScanLog.query.filter_by(user_id=user.id, is_successful=False).count()
    recent_projects = (
        Project.query.filter(project_user_access_filter(user.id))
        .order_by(Project.created_at.desc(), Project.id.desc())
        .limit(10)
        .all()
    )
    recent_scans = (
        ScanLog.query.filter_by(user_id=user.id)
        .order_by(ScanLog.created_at.desc(), ScanLog.id.desc())
        .limit(10)
        .all()
    )

    for project in recent_projects:
        project.pairs_count = ProjectPair.query.filter_by(project_id=project.id).count()
        project.scan_count = ScanLog.query.filter_by(project_id=project.id).count()

    log_admin_activity(
        admin.id,
        "view_user_dashboard",
        f"Viewed read-only dashboard context for user: {user.email}",
    )
    return render_template(
        "admin/user_dashboard_context.html",
        admin=admin,
        user=user,
        trial=trial,
        total_projects=total_projects,
        total_pairs=total_pairs,
        total_scans=total_scans,
        successful_scans=successful_scans,
        failed_scans=failed_scans,
        recent_projects=recent_projects,
        recent_scans=recent_scans,
        entitlement_summary=user_entitlement_summary(user),
    )


@app.route("/admin/users/<int:user_id>/dashboard/return", methods=["GET"])
@require_admin_permission("admin.users.view")
def admin_return_from_user_dashboard(user_id):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    log_admin_activity(
        admin.id,
        "exit_user_dashboard",
        f"Returned from read-only dashboard context for user: {user.email}",
    )
    return redirect(url_for("admin_view_user", user_id=user.id))

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
    return render_template(
        "admin/plans.html",
        admin=admin,
        plans=plans,
        v11_experience_options=V11_EXPERIENCE_PRESENTATION,
    )
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
# --------------------------------------------------------------------------------------------
# Admin Routes - Plan catalogue governance (Wave 5)
#
# Wave 2 added the real commercial policy columns to SubscriptionPlan
# (plan_family, lifecycle_status, plan_revision, per-file media policy,
# base_storage_bytes, per-experience entitlements) and plans.html renders them,
# but the Admin FORMS never wrote a single one of them and the add/edit routes
# accepted any number, including negatives. One shared parser now governs both
# routes so a plan cannot be created through one door that the other would
# reject.
# --------------------------------------------------------------------------------------------
_PLAN_UNSET = object()

# Fields that describe WHAT WAS SOLD. Changing any of them bumps plan_revision
# so a PaymentOrder policy snapshot stays traceable to the definition it was
# sold under. Presentation-only fields (name, description, features, ordering,
# popularity) deliberately do NOT bump it.
PLAN_REVISION_TRACKED_FIELDS = (
    "plan_family", "lifecycle_status", "plan_amount", "offer_price", "currency",
    "duration_type", "duration_value", "trial_days", "total_project_limit",
    "total_scan_limit", "max_pairs_per_project", "max_image_bytes",
    "max_video_bytes", "max_video_duration_seconds", "max_image_dimension_px",
    "max_image_pixels", "base_storage_bytes", "allow_direct_qr",
    "allow_detect_once", "allow_tracked_overlay",
)
PLAN_DURATION_TYPES = ("time", "count")


def _plan_number_field(form, field, cast=int, minimum=0):
    """(value, error). _PLAN_UNSET when the field was not submitted at all, so
    an older or partial form can never blank a column it does not render."""
    if field not in form:
        return _PLAN_UNSET, None
    raw = (form.get(field) or "").strip()
    if raw == "" or raw.lower() == "unlimited":
        return None, None
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return None, f"{field.replace('_', ' ').capitalize()} must be a number."
    if minimum is not None and value < minimum:
        return None, f"{field.replace('_', ' ').capitalize()} cannot be less than {minimum}."
    return value, None


def _plan_form_values(form, existing=None):
    """Parse and validate the Admin plan form. Returns (values, error).

    Only keys actually present in the submitted form appear in `values`, so an
    edit never silently resets a column the form did not render.
    """
    values = {}

    plan_name = (form.get("plan_name") or "").strip()
    if existing is None and not plan_name:
        return None, "Plan name is required."
    if plan_name:
        values["plan_name"] = plan_name
    if "plan_description" in form:
        values["plan_description"] = (form.get("plan_description") or "").strip() or None

    if "currency" in form:
        values["currency"] = (form.get("currency") or "INR").strip().upper()[:10] or "INR"

    if "duration_type" in form:
        duration_type = (form.get("duration_type") or "").strip().lower()
        if duration_type not in PLAN_DURATION_TYPES:
            return None, "Unsupported duration type."
        values["duration_type"] = duration_type

    if "plan_family" in form:
        plan_family = (form.get("plan_family") or "").strip().upper()
        if plan_family not in PLAN_FAMILIES:
            return None, "Unsupported plan family."
        values["plan_family"] = plan_family

    if "lifecycle_status" in form:
        lifecycle_status = (form.get("lifecycle_status") or "").strip().upper()
        if lifecycle_status not in PLAN_LIFECYCLE_STATUSES:
            return None, "Unsupported plan lifecycle status."
        values["lifecycle_status"] = lifecycle_status

    numeric_fields = (
        ("plan_amount", float, 0),
        ("offer_price", float, 0),
        ("duration_value", int, 1),
        ("trial_days", int, 0),
        ("display_order", int, 0),
        ("max_video_duration_seconds", int, 1),
        ("max_image_dimension_px", int, 1),
        ("max_image_bytes", int, 1),
        ("max_video_bytes", int, 1),
        ("max_image_pixels", int, 1),
        ("base_storage_bytes", int, 0),
    )
    for field, cast, minimum in numeric_fields:
        value, error = _plan_number_field(form, field, cast=cast, minimum=minimum)
        if error:
            return None, error
        if value is not _PLAN_UNSET:
            values[field] = value

    # Unlimited checkboxes keep their long-standing meaning: NULL column.
    for field, unlimited_field in (
        ("total_project_limit", "unlimited_projects"),
        ("total_scan_limit", "unlimited_scans"),
    ):
        if form.get(unlimited_field) == "on":
            values[field] = None
            continue
        value, error = _plan_number_field(form, field, cast=int, minimum=0)
        if error:
            return None, error
        if value is not _PLAN_UNSET:
            values[field] = value

    pairs_raw = (form.get("max_pairs_per_project") or "").strip()
    if existing is None or pairs_raw or "max_pairs_per_project" in form:
        if not pairs_raw:
            return None, "Pairs allowed per project is required and must be a positive integer."
        try:
            pairs_value = int(pairs_raw)
        except (TypeError, ValueError):
            return None, "Pairs allowed per project must be a positive integer."
        if pairs_value < 1:
            return None, "Pairs allowed per project must be a positive integer."
        values["max_pairs_per_project"] = pairs_value

    if "features" in form:
        features = (form.get("features") or "").strip()
        values["features_json"] = json.dumps([f.strip() for f in features.split("\n") if f.strip()])

    # Checkbox groups. An unchecked box is simply absent from a POST, so each
    # group carries a hidden marker; without the marker the existing values are
    # left alone rather than silently cleared.
    if form.get("plan_flags_form"):
        values["is_popular"] = form.get("is_popular") == "on"
        values["is_active"] = form.get("is_active") == "on"
    if form.get("plan_experience_form"):
        values["allow_direct_qr"] = form.get("allow_direct_qr") == "on"
        values["allow_detect_once"] = form.get("allow_detect_once") == "on"
        values["allow_tracked_overlay"] = form.get("allow_tracked_overlay") == "on"
        if not any(
            values[flag] for flag in ("allow_direct_qr", "allow_detect_once", "allow_tracked_overlay")
        ):
            return None, "A plan must allow at least one experience."

    offer = values.get("offer_price", getattr(existing, "offer_price", None))
    amount = values.get("plan_amount", getattr(existing, "plan_amount", None))
    if offer is not None and amount is not None and float(offer) > float(amount):
        return None, "Offer price cannot exceed the plan amount."

    return values, None


def _apply_plan_values(plan, values):
    """Write parsed values onto a plan and report whether commercial policy moved."""
    changed_policy = False
    for key, value in values.items():
        if key in PLAN_REVISION_TRACKED_FIELDS and getattr(plan, key, None) != value:
            changed_policy = True
        setattr(plan, key, value)
    return changed_policy


@app.route("/admin/plans/add", methods=["GET", "POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_add_plan():
    admin = current_admin()

    def _render(error=None):
        if error:
            flash(error, "error")
        return render_template(
            "admin/add_plan.html",
            admin=admin,
            v11_experience_options=V11_EXPERIENCE_PRESENTATION,
            plan_families=sorted(PLAN_FAMILIES),
            plan_lifecycle_statuses=sorted(PLAN_LIFECYCLE_STATUSES),
        )

    if request.method == "GET":
        return _render()

    values, error = _plan_form_values(request.form)
    if error:
        return _render(error)

    try:
        plan = SubscriptionPlan(created_by=admin.id)
        _apply_plan_values(plan, values)
        db.session.add(plan)
        db.session.commit()
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        app.logger.warning("admin_add_plan rejected: %s", exc)
        return _render("Plan configuration was rejected. Check the values and try again.")

    log_admin_activity(
        admin.id,
        "plan_add",
        f"Added plan {plan.id} ({plan.plan_name}) family={plan.plan_family} "
        f"lifecycle={plan.lifecycle_status} rev={plan.plan_revision}",
    )
    db.session.commit()
    flash("Plan created successfully.", "success")
    return redirect(url_for("admin_plans"))


@app.route("/admin/plans/<int:plan_id>/edit", methods=["GET", "POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_edit_plan(plan_id):
    admin = current_admin()
    plan = SubscriptionPlan.query.get_or_404(plan_id)

    if request.method == "GET":
        return render_template(
            "admin/edit_plan.html",
            admin=admin,
            plan=plan,
            v11_experience_options=V11_EXPERIENCE_PRESENTATION,
            plan_families=sorted(PLAN_FAMILIES),
            plan_lifecycle_statuses=sorted(PLAN_LIFECYCLE_STATUSES),
        )

    values, error = _plan_form_values(request.form, existing=plan)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_edit_plan", plan_id=plan.id))

    before = {key: getattr(plan, key, None) for key in PLAN_REVISION_TRACKED_FIELDS}
    try:
        # Historical commercial contracts are NOT touched: PaymentOrder carries
        # its own policy snapshot taken at activation, so editing the live plan
        # can never rewrite what a past customer bought. The revision bump is
        # what makes the two distinguishable afterwards.
        if _apply_plan_values(plan, values):
            plan.plan_revision = int(plan.plan_revision or 1) + 1
        db.session.commit()
    except (ValueError, SQLAlchemyError) as exc:
        db.session.rollback()
        app.logger.warning("admin_edit_plan rejected for plan %s: %s", plan_id, exc)
        flash("Plan configuration was rejected. Check the values and try again.", "error")
        return redirect(url_for("admin_edit_plan", plan_id=plan_id))

    moved = [
        f"{key}: {before[key]} -> {getattr(plan, key, None)}"
        for key in PLAN_REVISION_TRACKED_FIELDS
        if before[key] != getattr(plan, key, None)
    ]
    log_admin_activity(
        admin.id,
        "plan_edit",
        f"Edited plan {plan.id} ({plan.plan_name}) rev={plan.plan_revision}"
        + (f"; {'; '.join(moved)[:400]}" if moved else "; presentation only"),
    )
    db.session.commit()
    flash("Plan updated successfully.", "success")
    return redirect(url_for("admin_plans"))


def plan_commercial_references(plan):
    """Everything that would be orphaned by hard-deleting this plan."""
    return {
        "subscribers": User.query.filter_by(subscription_id=plan.id).count(),
        "pending_changes": User.query.filter_by(pending_plan_id=plan.id).count(),
        "payment_orders": PaymentOrder.query.filter_by(plan_id=plan.id).count(),
    }


@app.route("/admin/plans/<int:plan_id>/delete", methods=["POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_delete_plan(plan_id):
    """Hard delete only for a plan nothing commercial has ever referenced.

    The pre-Wave-5 check looked at User.subscription_id alone, so a plan whose
    every subscriber had since moved on could still be deleted out from under
    the PaymentOrder rows that reference it by id - destroying the audit trail
    for money already taken. Referenced plans are archived instead.
    """
    admin = current_admin()
    plan = SubscriptionPlan.query.get_or_404(plan_id)
    references = plan_commercial_references(plan)
    if any(references.values()):
        detail = ", ".join(f"{count} {name.replace('_', ' ')}" for name, count in references.items() if count)
        flash(
            f"Cannot delete this plan: {detail} still reference it. "
            "Archive it instead - archiving stops new purchases and keeps history intact.",
            "error",
        )
        return redirect(url_for("admin_plans"))

    log_admin_activity(admin.id, "plan_delete", f"Deleted unreferenced plan {plan.id} ({plan.plan_name})")
    db.session.delete(plan)
    db.session.commit()
    flash("Plan deleted successfully.", "success")
    return redirect(url_for("admin_plans"))


@app.route("/admin/plans/<int:plan_id>/toggle-status", methods=["POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_toggle_plan_status(plan_id):
    admin = current_admin()
    plan = SubscriptionPlan.query.get_or_404(plan_id)

    plan.is_active = not plan.is_active
    db.session.commit()

    status = "activated" if plan.is_active else "deactivated"
    log_admin_activity(admin.id, "plan_toggle", f"{status} plan {plan.id} ({plan.plan_name})")
    db.session.commit()

    flash(f"Plan {status} successfully.", "success")
    return redirect(url_for("admin_plans"))


@app.route("/admin/plans/<int:plan_id>/lifecycle", methods=["POST"])
@require_admin_permission("superadmin.plans.manage")
def admin_set_plan_lifecycle(plan_id):
    """Move a plan along its lifecycle without touching anything else.

    ARCHIVED / CLOSED_FOR_NEW_PURCHASE are non-destructive by construction:
    they only stop NEW purchases (SubscriptionPlan.is_purchasable). Existing
    subscribers keep their term, their projects, their media and their QR
    codes, and their entitlements keep resolving from the same plan row.
    """
    admin = current_admin()
    plan = SubscriptionPlan.query.get_or_404(plan_id)
    lifecycle_status = (request.form.get("lifecycle_status") or "").strip().upper()
    if lifecycle_status not in PLAN_LIFECYCLE_STATUSES:
        flash("Unsupported plan lifecycle status.", "error")
        return redirect(url_for("admin_plans"))

    previous = plan.lifecycle_status
    if previous == lifecycle_status:
        flash("Plan lifecycle status is already set to that value.", "info")
        return redirect(url_for("admin_plans"))

    plan.lifecycle_status = lifecycle_status
    plan.plan_revision = int(plan.plan_revision or 1) + 1
    db.session.commit()
    log_admin_activity(
        admin.id,
        "plan_lifecycle_change",
        f"Plan {plan.id} ({plan.plan_name}) lifecycle {previous} -> {lifecycle_status} "
        f"rev={plan.plan_revision}",
    )
    db.session.commit()
    flash(f"Plan lifecycle set to {lifecycle_status}.", "success")
    return redirect(url_for("admin_plans"))

# --------------------------------------------------------------------------------------------
# Admin Routes - Add-on catalogue (P0-3)
#
# The AddonCatalog table shipped with purchase, fulfilment, entitlement-ledger
# and refund-reversal flows all built on it, but nothing anywhere constructed a
# row: no seed, no migration insert, no Admin route. On a freshly migrated
# production database GET /api/addons/catalog therefore returned [] forever and
# the whole add-on product was dark, operable only by hand-editing the database.
#
# These routes are the governed surface. Prices and quantities are NEVER
# invented in code - they are entered by a Super Admin here, or supplied as
# explicit configured input to the `seed-addon-catalog` CLI below.
# --------------------------------------------------------------------------------------------
ADDON_CATALOG_EDITABLE_TYPES = ADDON_PURCHASABLE_TYPES


def _addon_catalog_form_values(form, existing=None):
    """Parse and validate the Admin add-on form. Returns (values, error)."""
    code = (form.get("code") or "").strip()
    name = (form.get("name") or "").strip()
    addon_type = (form.get("addon_type") or "").strip().upper()
    description = (form.get("description") or "").strip() or None
    currency = (form.get("currency") or "INR").strip().upper()[:3] or "INR"

    if not code or not name:
        return None, "Code and name are required."
    if addon_type not in ADDON_CATALOG_EDITABLE_TYPES:
        return None, "Unsupported add-on type."

    def _int_or_none(field):
        raw = (form.get(field) or "").strip()
        if raw == "":
            return None
        return int(raw)

    try:
        unit_amount = float((form.get("unit_amount") or "").strip())
        scan_delta = _int_or_none("scan_delta")
        validity_days_delta = _int_or_none("validity_days_delta")
        project_delta = _int_or_none("project_delta")
        storage_bytes_delta = _int_or_none("storage_bytes_delta")
    except (TypeError, ValueError):
        return None, "Price and delta values must be numbers."

    if unit_amount <= 0:
        return None, "Unit amount must be greater than zero."

    values = {
        "code": code,
        "name": name,
        "description": description,
        "addon_type": addon_type,
        "unit_amount": unit_amount,
        "currency": currency,
        "scan_delta": scan_delta,
        "validity_days_delta": validity_days_delta,
        "project_delta": project_delta,
        # Bytes. The admin supplies the real SKU size; nothing is defaulted.
        "storage_bytes_delta": storage_bytes_delta,
        "is_active": bool(form.get("is_active")),
        "is_commercially_available": bool(form.get("is_commercially_available")),
    }

    # Reuse the exact same effect resolution the purchase path uses, so an item
    # that would be rejected at checkout can never be saved as available.
    probe = AddonCatalog(**{k: v for k, v in values.items() if k != "code"}, code=code)
    try:
        _entitlement_type, delta = _addon_effect(probe, 1)
    except ValueError:
        return None, "Unsupported add-on type."
    if delta <= 0:
        return None, "This add-on type needs a positive quantity for its effect field."

    # An already-purchased catalog row is a commercial contract, not a draft.
    # Refund reconciliation re-reads item.addon_type to decide how to reverse
    # the entitlement, so repointing the type of a sold SKU would reverse the
    # WRONG entitlement (or hit the manual-review branch) on every historical
    # purchase. Everything else about the row stays freely editable.
    if existing is not None and addon_type != existing.addon_type:
        if AddonPurchase.query.filter_by(catalog_id=existing.id).first():
            return None, (
                "This add-on has already been purchased; its type can no longer be changed. "
                "Deactivate it and create a new add-on instead."
            )

    duplicate = AddonCatalog.query.filter(AddonCatalog.code == code)
    if existing is not None:
        duplicate = duplicate.filter(AddonCatalog.id != existing.id)
    if duplicate.first():
        return None, "That add-on code is already in use."

    return values, None


@app.route("/admin/addons", methods=["GET"])
@require_admin_permission("superadmin.addons.manage")
def admin_addons():
    admin = current_admin()
    items = AddonCatalog.query.order_by(AddonCatalog.id.asc()).all()
    purchase_counts = dict(
        db.session.query(AddonPurchase.catalog_id, func.count(AddonPurchase.id))
        .group_by(AddonPurchase.catalog_id)
        .all()
    )
    return render_template(
        "admin/addons.html",
        admin=admin,
        items=items,
        purchase_counts=purchase_counts,
        addon_types=sorted(ADDON_CATALOG_EDITABLE_TYPES),
    )


@app.route("/admin/addons/create", methods=["POST"])
@require_admin_permission("superadmin.addons.manage")
def admin_create_addon():
    admin = current_admin()
    values, error = _addon_catalog_form_values(request.form)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_addons"))

    item = AddonCatalog(**values)
    db.session.add(item)
    db.session.commit()
    log_admin_activity(admin.id, "addon_create", f"Created add-on: {item.code} ({item.addon_type})")
    flash("Add-on created.", "success")
    return redirect(url_for("admin_addons"))


@app.route("/admin/addons/<int:catalog_id>/edit", methods=["POST"])
@require_admin_permission("superadmin.addons.manage")
def admin_edit_addon(catalog_id):
    admin = current_admin()
    item = AddonCatalog.query.get_or_404(catalog_id)
    values, error = _addon_catalog_form_values(request.form, existing=item)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_addons"))

    for key, value in values.items():
        setattr(item, key, value)
    db.session.commit()
    log_admin_activity(admin.id, "addon_edit", f"Edited add-on: {item.code}")
    flash("Add-on updated.", "success")
    return redirect(url_for("admin_addons"))


@app.route("/admin/addons/<int:catalog_id>/toggle", methods=["POST"])
@require_admin_permission("superadmin.addons.manage")
def admin_toggle_addon(catalog_id):
    """Soft-deactivate only.

    There is deliberately NO delete route: AddonPurchase and the entitlement
    ledger reference catalog rows by id and the refund-reversal path re-reads
    them, so a hard delete would orphan purchase history and break refunds.
    Deactivating removes the item from the commercial API and nothing else.
    """
    admin = current_admin()
    item = AddonCatalog.query.get_or_404(catalog_id)
    field = (request.form.get("field") or "is_active").strip()
    if field not in ("is_active", "is_commercially_available"):
        flash("Unsupported add-on field.", "error")
        return redirect(url_for("admin_addons"))

    setattr(item, field, not bool(getattr(item, field)))
    db.session.commit()
    state = "enabled" if getattr(item, field) else "disabled"
    log_admin_activity(admin.id, "addon_toggle", f"{state} {field} for add-on: {item.code}")
    flash(f"Add-on {field.replace('_', ' ')} {state}.", "success")
    return redirect(url_for("admin_addons"))


def seed_addon_catalog_items(entries):
    """Idempotent upsert of catalog rows keyed on `code`. Returns (created, updated).

    Deliberately takes explicit caller-supplied entries: add-on prices and
    quantities are a commercial decision and are never hard-coded here.
    """
    created = 0
    updated = 0
    for entry in entries:
        code = (entry.get("code") or "").strip()
        if not code:
            raise ValueError("every add-on entry needs a 'code'")
        addon_type = (entry.get("addon_type") or "").strip().upper()
        if addon_type not in ADDON_CATALOG_EDITABLE_TYPES:
            raise ValueError(f"unsupported addon_type for {code}: {addon_type or '(missing)'}")

        fields = {
            "name": entry.get("name") or code,
            "description": entry.get("description"),
            "addon_type": addon_type,
            "unit_amount": float(entry["unit_amount"]),
            "currency": (entry.get("currency") or "INR").strip().upper()[:3],
            "scan_delta": entry.get("scan_delta"),
            "validity_days_delta": entry.get("validity_days_delta"),
            "project_delta": entry.get("project_delta"),
            "storage_bytes_delta": entry.get("storage_bytes_delta"),
            "is_active": bool(entry.get("is_active", True)),
            "is_commercially_available": bool(entry.get("is_commercially_available", True)),
        }

        item = AddonCatalog.query.filter_by(code=code).first()
        if item:
            for key, value in fields.items():
                setattr(item, key, value)
            updated += 1
        else:
            db.session.add(AddonCatalog(code=code, **fields))
            created += 1
    db.session.commit()
    return created, updated


@app.cli.command("seed-addon-catalog")
@click.option("--file", "source_file", default=None, help="Path to a JSON array of add-on definitions.")
@click.option("--apply", "apply_changes", is_flag=True, default=False, help="Write changes (default is dry-run).")
def seed_addon_catalog_command(source_file, apply_changes):
    """Bootstrap the add-on catalogue from explicit configured input.

    Source is --file, or the ADDON_CATALOG_SEED_FILE / ADDON_CATALOG_SEED_JSON
    environment variables. Running it twice is a no-op beyond refreshing fields
    (upsert keyed on `code`), so it is safe in a deploy script. It refuses to
    invent prices: with no configured source it explains what to provide and
    exits non-zero.
    """
    raw = None
    path = source_file or os.environ.get("ADDON_CATALOG_SEED_FILE")
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    elif os.environ.get("ADDON_CATALOG_SEED_JSON"):
        raw = os.environ["ADDON_CATALOG_SEED_JSON"]

    if not raw:
        click.echo(
            "No add-on catalogue source configured. Provide --file, "
            "ADDON_CATALOG_SEED_FILE or ADDON_CATALOG_SEED_JSON (a JSON array of "
            "{code, name, addon_type, unit_amount, ...}), or create items in the "
            "Admin UI at /admin/addons. Prices and quantities are a commercial "
            "decision and are never defaulted."
        )
        raise SystemExit(2)

    entries = json.loads(raw)
    if not isinstance(entries, list):
        click.echo("Add-on catalogue source must be a JSON array.")
        raise SystemExit(2)

    if not apply_changes:
        existing = {item.code for item in AddonCatalog.query.all()}
        would_create = [e.get("code") for e in entries if e.get("code") not in existing]
        click.echo(f"Dry run. Entries: {len(entries)}  Would create: {len(would_create)}  "
                   f"Would update: {len(entries) - len(would_create)}")
        click.echo("Re-run with --apply to write.")
        return

    created, updated = seed_addon_catalog_items(entries)
    click.echo(f"Add-on catalogue seeded. Created: {created}  Updated: {updated}")


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

    # Eligibility is decided by the same backend function the POST route uses -
    # the template never recomputes it, it only renders eligible/reason_text.
    return render_template("admin/view_payment.html",
                         admin=admin,
                         payment=payment,
                         user=user,
                         plan=plan,
                         refund=_existing_refund_for_source(payment_order=payment),
                         refund_eligibility=refund_eligibility_for_payment_order(payment))


@app.route("/admin/api/payments/<int:payment_id>/refund-eligibility", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_payment_refund_eligibility(payment_id):
    payment = PaymentOrder.query.get_or_404(payment_id)
    return jsonify({"success": True, "eligibility": refund_eligibility_for_payment_order(payment)})


@app.route("/admin/api/payments/<int:payment_id>/refund", methods=["POST"])
@require_admin_permission("admin.payments.refund")
def admin_refund_payment(payment_id):
    admin = current_admin()
    payment = PaymentOrder.query.get_or_404(payment_id)
    payload = request.get_json(silent=True) or request.form
    result = initiate_admin_refund(
        admin,
        payment_order=payment,
        reason=payload.get("reason"),
        idempotency_key=payload.get("idempotency_key"),
    )
    status = 200 if result.get("success") else 409
    return jsonify(result), status


@app.route("/admin/api/addon-purchases/<int:purchase_id>/refund-eligibility", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_addon_refund_eligibility(purchase_id):
    purchase = AddonPurchase.query.get_or_404(purchase_id)
    return jsonify({"success": True, "eligibility": refund_eligibility_for_addon_purchase(purchase)})


@app.route("/admin/api/addon-purchases/<int:purchase_id>/refund", methods=["POST"])
@require_admin_permission("admin.payments.refund")
def admin_refund_addon_purchase(purchase_id):
    admin = current_admin()
    purchase = AddonPurchase.query.get_or_404(purchase_id)
    payload = request.get_json(silent=True) or request.form
    result = initiate_admin_refund(
        admin,
        addon_purchase=purchase,
        reason=payload.get("reason"),
        idempotency_key=payload.get("idempotency_key"),
    )
    status = 200 if result.get("success") else 409
    return jsonify(result), status


@app.route("/admin/api/refunds/<int:refund_id>", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_refund_detail(refund_id):
    refund = PaymentRefund.query.get_or_404(refund_id)
    return jsonify({"success": True, "refund": _payment_refund_payload(refund)})


@app.route("/admin/api/refunds/<int:refund_id>/recover", methods=["POST"])
@require_admin_permission("admin.payments.refund")
def admin_recover_refund(refund_id):
    """Re-drive one stuck refund on its existing record (V1.1 P0-1).

    Same permission as issuing a refund, because it can result in a provider
    refund call. Read-only unless the caller passes apply=true. Deliberately
    API-only for now: the operator path this wave has to ship is
    `flask reconcile-refunds`, and no new admin UI is added here.
    """
    admin = current_admin()
    refund = PaymentRefund.query.get_or_404(refund_id)
    payload = request.get_json(silent=True) or request.form or {}
    apply_changes = str(payload.get("apply", "")).strip().lower() in ("1", "true", "yes", "on")
    result = recover_payment_refund(refund, admin=admin, apply_changes=apply_changes)
    status = 409 if result["outcome"] in REFUND_RECOVERY_UNRESOLVED_OUTCOMES else 200
    return jsonify({
        "success": status == 200,
        "recovery": result,
        "refund": _payment_refund_payload(refund),
    }), status


@app.route("/admin/api/refunds", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_refund_list():
    """Operational refund inspection.

    Refund scope is unchanged (admin-only, full refunds only). The only gap
    this closes is visibility: a refund whose provider call succeeded but whose
    entitlement reconciliation FAILED or needs MANUAL_REVIEW was reachable only
    if an operator already knew the refund id. Read-only, no state changes.
    """
    query = PaymentRefund.query
    status = (request.args.get("status") or "").strip().upper()
    if status:
        if status not in REFUND_STATUSES:
            return jsonify({"success": False, "code": "INVALID_STATUS", "error": "Unknown refund status."}), 400
        query = query.filter(PaymentRefund.status == status)

    reconciliation_status = (request.args.get("reconciliation_status") or "").strip().upper()
    if reconciliation_status:
        if reconciliation_status not in REFUND_RECONCILIATION_STATUSES:
            return jsonify({
                "success": False,
                "code": "INVALID_RECONCILIATION_STATUS",
                "error": "Unknown reconciliation status.",
            }), 400
        query = query.filter(PaymentRefund.reconciliation_status == reconciliation_status)

    # P1-6: the attention queue is now literally the same predicate
    # `flask reconcile-refunds` works from (stuck_refund_filter), instead of a
    # second hand-written status list that could drift from it.
    needs_attention = request.args.get("needs_attention") in ("1", "true", "True")
    if needs_attention:
        query = query.filter(stuck_refund_filter())

    user_id = request.args.get("user_id", type=int)
    if user_id:
        query = query.filter(PaymentRefund.user_id == user_id)

    pagination = query.order_by(PaymentRefund.id.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=admin_page_size(),
        error_out=False,
    )
    payload = {
        "success": True,
        "refunds": [_payment_refund_payload(r) for r in pagination.items],
        "page": pagination.page,
        "pages": pagination.pages,
        "total": pagination.total,
    }
    if needs_attention:
        # Out-of-band (provider-dashboard) refunds have NO PaymentRefund row by
        # design - P0 correlates them onto the webhook event instead of inventing
        # one. They were visible only in `flask reconcile-refunds` output, so an
        # operator working from the API could not see them at all. Ids and the
        # correlated local source only: never the provider payload.
        out_of_band = unlinked_out_of_band_refund_events()
        payload["out_of_band_refunds"] = [
            {
                "webhook_event_id": event.id,
                "event_type": event.event_type,
                "payment_order_id": event.payment_order_id,
                "addon_purchase_id": event.addon_purchase_id,
                "state": "MANUAL_REVIEW_REQUIRED",
                "reason": "Provider-side refund correlated to a local purchase with no local refund record.",
            }
            for event in out_of_band
        ]
        payload["out_of_band_total"] = len(out_of_band)
    return jsonify(payload)

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
    """Legacy route, kept for backward compatibility (bookmarks/links).

    admin_users (/admin/users) is now the single canonical admin user list -
    it already covers filtering, search, and block/unblock. The one action
    this page had that admin_users lacked ("View User Dashboard") has been
    added to the user-detail page (admin_view_user) instead. Redirect
    directly rather than maintaining two separate user-list
    implementations/templates, forwarding the filters this page accepted so
    existing bookmarks/links keep working.
    """
    return redirect(url_for(
        "admin_users",
        status=request.args.get("status", "all"),
        plan_id=request.args.get("plan_id", type=int),
        search=request.args.get("search", "").strip() or None,
        page=request.args.get("page", type=int),
        per_page=request.args.get("per_page", type=int),
    ))

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

    # Coverage administration (Wave 5). Read-only inspection of the same
    # ProjectServiceCoverage rows the resolver reads: source, window, status
    # and who granted it, next to the resolved live/renewal state. Nothing here
    # mutates coverage - the only admin grant path stays the POST route.
    coverage_rows = (
        ProjectServiceCoverage.query
        .filter_by(project_id=project.id)
        .order_by(ProjectServiceCoverage.coverage_start.desc(), ProjectServiceCoverage.id.desc())
        .limit(25)
        .all()
    )
    # CURRENT owner drives commercial responsibility after a Wave 4 transfer;
    # the creator stays visible separately as history and is never overwritten.
    creator_id = project_created_by_user_id(project)

    return render_template("admin/view_project.html",
                         admin=admin,
                         project=project,
                         owner=owner,
                         coverage=project_coverage_summary(project),
                         coverage_rows=coverage_rows,
                         ownership=project_ownership_context(project, None),
                         creator=User.query.get(creator_id) if creator_id else None,
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
    
    blocked = project_deletion_block_reason(project)
    if blocked:
        flash(blocked, "error")
        return redirect(url_for("admin_view_project", project_id=project_id))

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
# Admin Routes - Content report moderation (Domain 2B). Explicit, audited, and
# never automatic: no route here deletes a project or its media, and none bans
# a creator. Reactivating a suspended project keeps using the existing
# admin_restore_project route rather than a new reversal path.
# --------------------------------------------------------------------------------------------
def _content_report_payload(report):
    return {
        "id": report.id,
        "project_id": report.project_id,
        "reason": report.reason,
        "details": report.details,
        "status": report.status,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "reviewed_at": report.reviewed_at.isoformat() if report.reviewed_at else None,
        "reviewed_by_admin_id": report.reviewed_by_admin_id,
        "resolution_action": report.resolution_action,
        "resolution_reason": report.resolution_reason,
        "reporter_user_id": report.reporter_user_id,
        "has_reporter_contact": bool(report.reporter_email),
        # Additive: the moderation queue needs something readable to show
        # next to the project link instead of a bare numeric id.
        "project_name": report.project.name if report.project else None,
    }


@app.route("/admin/moderation", methods=["GET"])
@require_admin_permission("admin.reports.view")
def admin_moderation_page():
    """Server-rendered shell for the existing JSON moderation API.

    /admin/reports and /admin/reports/<id>/review stay pure JSON (they already
    have contract tests); this only adds the page that drives them, so the
    queue is reachable from the admin nav instead of via curl.
    """
    return render_template(
        "admin/moderation.html",
        admin=current_admin(),
        report_statuses=["OPEN", "UNDER_REVIEW", "ACTION_TAKEN", "DISMISSED"],
        report_actions=["NONE", "PROJECT_SUSPENDED", "CREATOR_CONTACT_REQUIRED", "LEGAL_REVIEW_REQUIRED", "OTHER"],
        status_filter=(request.args.get("status") or "").strip().upper(),
    )


@app.route("/admin/reports", methods=["GET"])
@require_admin_permission("admin.reports.view")
def admin_content_reports():
    query = ContentReport.query
    status = (request.args.get("status") or "").strip().upper()
    if status in CONTENT_REPORT_STATUSES:
        query = query.filter(ContentReport.status == status)
    project_id = request.args.get("project_id", type=int)
    if project_id:
        query = query.filter(ContentReport.project_id == project_id)
    reports = (
        query.order_by(ContentReport.created_at.desc(), ContentReport.id.desc())
        .limit(admin_page_size())
        .all()
    )
    return jsonify({"success": True, "reports": [_content_report_payload(r) for r in reports]})


@app.route("/admin/reports/<int:report_id>", methods=["GET"])
@require_admin_permission("admin.reports.view")
def admin_content_report_detail(report_id):
    report = ContentReport.query.get_or_404(report_id)
    return jsonify({"success": True, "report": _content_report_payload(report)})


@app.route("/admin/reports/<int:report_id>/review", methods=["POST"])
@require_admin_permission("admin.reports.manage")
def admin_review_content_report(report_id):
    admin = current_admin()
    report = ContentReport.query.get_or_404(report_id)
    payload = request.get_json(silent=True) or request.form
    status = (payload.get("status") or "").strip().upper()
    if status not in CONTENT_REPORT_STATUSES or status == "OPEN":
        return jsonify({"success": False, "code": "INVALID_STATUS", "error": "Invalid moderation status."}), 400
    action = (payload.get("resolution_action") or "").strip().upper() or None
    if status == "ACTION_TAKEN" and action not in CONTENT_REPORT_ACTIONS:
        return jsonify({"success": False, "code": "INVALID_ACTION", "error": "Invalid moderation action."}), 400
    if action and action not in CONTENT_REPORT_ACTIONS:
        return jsonify({"success": False, "code": "INVALID_ACTION", "error": "Invalid moderation action."}), 400
    resolution_reason = (payload.get("resolution_reason") or "").strip() or None

    report.status = status
    report.resolution_action = action
    report.resolution_reason = resolution_reason
    report.reviewed_by_admin_id = admin.id
    if status != "UNDER_REVIEW":
        report.reviewed_at = get_utc_now()

    if status == "ACTION_TAKEN" and action == "PROJECT_SUSPENDED":
        project = Project.query.get(report.project_id)
        if project:
            # Suspension only. Project row, media and QR are untouched.
            project.is_active = False
    db.session.commit()

    log_admin_activity(
        admin.id,
        "content_report_review",
        f"Report {report.id} (project {report.project_id}) -> {status}"
        + (f" action={action}" if action else "")
        + (f": {resolution_reason[:150]}" if resolution_reason else ""),
    )
    return jsonify({"success": True, "report": _content_report_payload(report)})


# ===========================================================================
# V1.1 Wave 4: Admin ownership review.
#
# Manual adjudication only. Nothing here infers ownership from an email match,
# QR possession or a beneficiary field, and an approved claim still has to pass
# both capacity checks before ownership actually moves.
# ===========================================================================
def _admin_ownership_redirect(message=None, category="success"):
    if message:
        flash(message, category)
    return redirect(url_for("admin_ownership_page"))


def _ownership_party_label(user_id):
    user = User.query.get(user_id) if user_id else None
    return user.email if user else None


def _ownership_row_project(row):
    """Project for an ownership/claim row, or None once it has been detached.

    project_id is NULL on rows kept as history after the project was deleted
    (P0-2); Query.get(None) warns about a fully-NULL identity, so short-circuit.
    """
    return Project.query.get(row.project_id) if row.project_id else None


def _admin_transfer_row(transfer):
    project = _ownership_row_project(transfer)
    return {
        "transfer": transfer,
        "project": project,
        "created_by": _ownership_party_label(project.created_by_user_id if project else None),
        "current_owner": _ownership_party_label(project_current_owner_user_id(project) if project else None),
        "manager_vendor": _ownership_party_label(project.manager_vendor_user_id if project else None),
        "beneficiary": _ownership_party_label(project.beneficiary_user_id if project else None),
        "from_owner": _ownership_party_label(transfer.from_owner_user_id),
        "to_user": _ownership_party_label(transfer.to_user_id),
        "capacity_block": transfer_capacity_snapshot(transfer),
        "audit": ownership_audit_trail(transfer),
    }


def _admin_claim_row(claim):
    project = _ownership_row_project(claim)
    return {
        "claim": claim,
        "project": project,
        "claimant": _ownership_party_label(claim.claimant_user_id),
        "current_owner": _ownership_party_label(project_current_owner_user_id(project) if project else None),
        "manager_vendor": _ownership_party_label(project.manager_vendor_user_id if project else None),
        "audit": ownership_audit_trail(claim),
        # The SAME gate approve/reject enforce (P1-5), resolved once for the
        # template so the page can hide a premature adjudication control instead
        # of offering it and flashing a PermissionError afterwards. The condition
        # is not restated in Jinja - this is the backend's own answer.
        "admin_block_reason": claim_admin_review_block_reason(claim),
    }


@app.route("/admin/ownership", methods=["GET"])
@require_admin_permission("admin.ownership.view")
def admin_ownership_page():
    transfers = (
        ProjectOwnershipTransfer.query.order_by(ProjectOwnershipTransfer.id.desc())
        .limit(admin_page_size()).all()
    )
    claims = (
        ProjectOwnershipClaim.query.order_by(ProjectOwnershipClaim.id.desc())
        .limit(admin_page_size()).all()
    )
    return render_template(
        "admin/ownership.html",
        admin=current_admin(),
        transfer_rows=[_admin_transfer_row(t) for t in transfers],
        claim_rows=[_admin_claim_row(c) for c in claims],
    )


@app.route("/admin/ownership/claims/<int:claim_id>/approve", methods=["POST"])
@require_admin_permission("admin.ownership.manage")
def admin_approve_ownership_claim(claim_id):
    admin = current_admin()
    claim = ProjectOwnershipClaim.query.get_or_404(claim_id)
    reason = (request.form.get("decision_reason") or "").strip()[:500] or None
    try:
        claim, transfer = approve_project_ownership_claim_by_admin(claim, admin, reason)
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _admin_ownership_redirect(str(exc), "error")
    log_admin_activity(
        admin.id,
        "ownership_claim_review",
        f"Claim {claim.id} (project {claim.project_id}) -> APPROVED_BY_ADMIN"
        + (f", transfer {transfer.id} opened" if transfer else ", no live owner to transfer from")
        + (f": {reason}" if reason else ""),
    )
    return _admin_ownership_redirect(
        "Claim approved. Ownership still moves only once the recipient accepts and has capacity."
    )


@app.route("/admin/ownership/claims/<int:claim_id>/reject", methods=["POST"])
@require_admin_permission("admin.ownership.manage")
def admin_reject_ownership_claim(claim_id):
    admin = current_admin()
    claim = ProjectOwnershipClaim.query.get_or_404(claim_id)
    reason = (request.form.get("decision_reason") or "").strip()[:500] or None
    try:
        reject_project_ownership_claim_by_admin(claim, admin, reason)
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _admin_ownership_redirect(str(exc), "error")
    log_admin_activity(
        admin.id,
        "ownership_claim_review",
        f"Claim {claim.id} (project {claim.project_id}) -> REJECTED" + (f": {reason}" if reason else ""),
    )
    return _admin_ownership_redirect("Claim rejected. The current owner is unchanged.")


@app.route("/admin/ownership/transfers/<int:transfer_id>/<action>", methods=["POST"])
@require_admin_permission("admin.ownership.manage")
def admin_resolve_ownership_transfer(transfer_id, action):
    admin = current_admin()
    transfer = ProjectOwnershipTransfer.query.get_or_404(transfer_id)
    reason = (request.form.get("reason") or "").strip()[:500] or None
    previous = transfer.status
    handlers = {
        "dispute": lambda: mark_project_transfer_disputed(transfer, admin, reason),
        "release-dispute": lambda: release_project_transfer_dispute(transfer, admin, reason),
        "cancel": lambda: cancel_project_ownership_transfer(transfer, admin=admin, reason=reason),
        # Admin override of the ACCEPTANCE step only. The capacity gates are
        # still enforced - an admin cannot force an oversized project onto an
        # account that cannot hold it.
        "complete": lambda: accept_project_ownership_transfer(transfer, completed_by_admin=admin),
    }
    if action not in handlers:
        abort(404)
    try:
        handlers[action]()
        db.session.commit()
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        return _admin_ownership_redirect(str(exc), "error")
    log_admin_activity(
        admin.id,
        "ownership_transfer_review",
        f"Transfer {transfer.id} (project {transfer.project_id}) {previous} -> {transfer.status}"
        f" [{action}] from user {transfer.from_owner_user_id} to user {transfer.to_user_id}"
        + (f": {reason}" if reason else ""),
    )
    return _admin_ownership_redirect(f"Transfer {transfer.id} is now {transfer.status}.")


@app.route("/admin/projects/<int:project_id>/service-coverage/grant", methods=["POST"])
@require_admin_permission("superadmin.capacity.manage")
def admin_grant_project_coverage(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    payload = request.get_json(silent=True) or request.form
    try:
        days = int(payload.get("days"))
    except (TypeError, ValueError):
        days = 0
    try:
        coverage = admin_grant_project_service_coverage(project, admin, days, payload.get("reason"))
    except ValueError as exc:
        db.session.rollback()
        return jsonify({"success": False, "code": "INVALID_GRANT", "error": str(exc)}), 400
    db.session.commit()
    return jsonify({
        "success": True,
        "coverage_id": coverage.id,
        "coverage_start": coverage.coverage_start.isoformat(),
        "coverage_end": coverage.coverage_end.isoformat(),
    }), 201


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

    # templates/admin/user_scans.html renders scan_logs/status/search (status
    # tabs + search box) which this route never used to pass - Jinja's
    # lenient Undefined silently rendered them as an empty table instead of
    # crashing. status maps onto the real ScanLog.is_successful boolean;
    # "partial" has no backing concept on ScanLog (a plain boolean, no
    # partial state), so it correctly yields zero rows rather than
    # inventing one. search filters the one real searchable relationship,
    # the scan's project name.
    status = request.args.get("status", "all")
    search = request.args.get("search", "").strip()

    scan_logs_query = ScanLog.query.filter_by(user_id=user_id)
    if status == "success":
        scan_logs_query = scan_logs_query.filter(ScanLog.is_successful.is_(True))
    elif status == "failed":
        scan_logs_query = scan_logs_query.filter(ScanLog.is_successful.is_(False))
    elif status != "all":
        # e.g. "partial" - not a real ScanLog state, so no row can match it.
        scan_logs_query = scan_logs_query.filter(ScanLog.id.is_(None))

    if search:
        scan_logs_query = scan_logs_query.join(Project, ScanLog.project_id == Project.id).filter(
            Project.name.ilike(f"%{search}%")
        )

    scan_logs = scan_logs_query.order_by(ScanLog.created_at.desc()).all()

    # ponytail: the table/stat rows below also read scan.duration_minutes,
    # scan.scanned_pairs_count and scan.detections_count - fields ScanLog
    # never tracked (it logs one pass/fail boolean per session, not
    # per-pair progress or timing). Left undefined, the header stats block
    # does `total + scan.scanned_pairs_count` which raises (Undefined has
    # no __add__), and the per-row progress bar does
    # `scan.total_pairs_count > 0` which raises the same way - so once
    # scan_logs has real rows this crashes instead of rendering. Zeroed
    # here as honest placeholders (no fabricated numbers); total_pairs_count
    # is real data pulled from the project relationship. Add real
    # duration/progress columns to ScanLog if this ever needs to be genuine.
    for scan in scan_logs:
        scan.status = "success" if scan.is_successful else "failed"
        scan.duration_minutes = 0
        scan.scanned_pairs_count = 0
        scan.detections_count = 0
        scan.total_pairs_count = len(scan.project.pairs) if scan.project else 0

    return render_template("admin/user_scans.html",
                         admin=admin,
                         user=user,
                         scan_history=scan_history,
                         total_scans=total_scans,
                         successful_scans=successful_scans,
                         failed_scans=failed_scans,
                         recent_scans=recent_scans,
                         scan_logs=scan_logs,
                         status=status,
                         search=search)

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
    # The admin sets the BASE plan allowance. Purchased EXTRA_SCANS and prior
    # admin grants live in the entitlement ledger and are re-added on top -
    # writing new_scan_limit straight into the column used to silently delete
    # entitlement the user had actually paid for (same class of bug as P0-1).
    materialize_plan_entitlements(user, plan_scan_limit=new_scan_limit)

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

    # Admin grants are auditable ledger rows, not a bare += on the materialized
    # column. Without the ledger row the grant was erased by the next plan
    # activation (reconciled_scan_limit rebuilds the column from plan + ledger).
    # source_type keeps it distinguishable from purchased entitlement in the
    # resolver, so neither can silently overwrite the other. A NEGATIVE amount
    # is a governed revoke: it lowers the allowance and deletes nothing.
    try:
        grant_account_entitlement(
            admin, user, "EXTRA_SCANS", extra_scans,
            reason=(request.form.get("reason") or "").strip() or None,
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin_user_scans", user_id=user_id))
    if user.subscription_status == "limit_reached" and (
        user.subscribed_scan_limit in (None, 0) or user.remaining_scans > 0
    ):
        user.subscription_status = "active"
    db.session.commit()


    verb = "Granted" if extra_scans > 0 else "Revoked"
    flash(f"{verb} {abs(extra_scans)} extra scans for user.", "success")
    return redirect(url_for("admin_user_scans", user_id=user_id))


# Reusable account-level capacity an admin may grant or revoke directly.
# activity_type per entitlement so AdminActivity stays greppable per product.
ADMIN_GRANTABLE_ENTITLEMENTS = {
    "ACCOUNT_STORAGE": ("account_storage_grant", "storage bytes"),
    "PROJECT_CAPACITY": ("project_capacity_grant", "project slots"),
    "EXTRA_SCANS": ("extra_scans_grant", "scans"),
}


def grant_account_entitlement(admin, user, entitlement_type, delta, reason=None):
    """Governed admin entitlement adjustment. Positive grants, negative revokes.

    One ledger mechanism for every reusable account-level capacity, so the
    sources stay separately auditable: source_type='admin_grant' here,
    'addon_purchase'/'refund' for anything the customer paid for, and the plan's
    own base allowance. None can overwrite another because
    get_effective_entitlements() sums them independently.

    REVOCATION IS NEVER DESTRUCTIVE. A negative delta only lowers the allowance;
    no project, media object or QR code is touched. If the account ends up over
    capacity, existing content keeps working and only NEW consumption is blocked.
    """
    if entitlement_type not in ADMIN_GRANTABLE_ENTITLEMENTS:
        raise ValueError("Unsupported entitlement type for an admin grant.")
    delta = int(delta or 0)
    if delta == 0:
        raise ValueError("An entitlement adjustment must be a non-zero amount.")
    activity_type, unit = ADMIN_GRANTABLE_ENTITLEMENTS[entitlement_type]
    verb = "Granted" if delta > 0 else "Revoked"
    activity = log_admin_activity(
        admin.id, activity_type,
        f"{verb} {abs(delta)} {unit} for {user.email}" + (f": {reason}" if reason else ""),
    )
    tx, _replay = _apply_entitlement_transaction(
        user,
        entitlement_type,
        delta,
        source_type=_ent.ADMIN_GRANT_SOURCE_TYPE,
        source_id=activity.id,
        reason=reason or f"admin_grant_{entitlement_type.lower()}:admin={admin.id}",
    )
    return tx


def grant_account_storage(admin, user, delta_bytes, reason=None):
    """Kept as the storage-shaped entry point Wave 3 callers already use."""
    return grant_account_entitlement(admin, user, "ACCOUNT_STORAGE", delta_bytes, reason)


def _admin_entitlement_adjust_route(user_id, entitlement_type, amount_field, success_message):
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    delta = request.form.get(amount_field, type=int, default=0)
    try:
        grant_account_entitlement(
            admin, user, entitlement_type, delta,
            reason=(request.form.get("reason") or "").strip() or None,
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin_view_user", user_id=user_id))
    db.session.commit()
    flash(success_message, "success")
    return redirect(url_for("admin_view_user", user_id=user_id))


@app.route("/admin/users/<int:user_id>/grant-storage", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_grant_account_storage(user_id):
    return _admin_entitlement_adjust_route(
        user_id, "ACCOUNT_STORAGE", "storage_bytes", "Account storage entitlement updated."
    )


@app.route("/admin/users/<int:user_id>/grant-project-capacity", methods=["POST"])
@require_admin_permission("admin.users.manage")
def admin_grant_project_capacity(user_id):
    """Admin project-slot grant/revoke. Purchased PROJECT_CAPACITY add-ons and
    the plan's own base limit are separate ledger/plan sources and are never
    overwritten here; revoking only lowers the effective slot allowance and
    deletes no project."""
    return _admin_entitlement_adjust_route(
        user_id, "PROJECT_CAPACITY", "project_slots", "Project capacity entitlement updated."
    )


@app.route("/admin/users/<int:user_id>/account-type", methods=["POST"])
@require_admin_permission("admin.ownership.manage")
def admin_set_account_type(user_id):
    """Admin-only governed account-type conversion.

    Wave 4 shipped can_convert_to_individual() as a validation foundation with
    no caller; this is that caller. There is deliberately NO self-service
    conversion route - vendor capability governs who may hold and manage other
    people's projects, so it stays an admin decision.

    NOTHING IS DESTROYED IN EITHER DIRECTION. Only User.account_type moves.
    Projects, media, QR codes, purchases, the entitlement ledger, the storage
    ledger, ownership history and the subscription itself are all untouched -
    plan_family stays a separate axis from account type by design, so the
    account's plan is not reassigned here either.
    """
    admin = current_admin()
    user = User.query.get_or_404(user_id)
    target = (request.form.get("account_type") or "").strip().upper()
    if target not in USER_ACCOUNT_TYPES:
        flash("Unsupported account type.", "error")
        return redirect(url_for("admin_view_user", user_id=user_id))

    previous = (user.account_type or ACCOUNT_TYPE_INDIVIDUAL).upper()
    if previous == target:
        flash("Account is already set to that type.", "info")
        return redirect(url_for("admin_view_user", user_id=user_id))

    if target == ACCOUNT_TYPE_INDIVIDUAL:
        ok, blocked_reason = can_convert_to_individual(user)
        if not ok:
            # Relationships are never silently severed: a blocked downgrade
            # just leaves the account a vendor until the operator resolves the
            # managed projects, transfers or claims themselves.
            flash(f"Cannot convert to Individual: {blocked_reason}", "error")
            return redirect(url_for("admin_view_user", user_id=user_id))

    reason = (request.form.get("reason") or "").strip()[:500] or None
    user.account_type = target
    db.session.commit()
    log_admin_activity(
        admin.id,
        "account_type_change",
        f"User {user.id} ({user.email}) account_type {previous} -> {target}"
        + (f": {reason}" if reason else ""),
    )
    db.session.commit()
    flash(f"Account type changed to {ACCOUNT_TYPE_LABELS.get(target, target)}.", "success")
    return redirect(url_for("admin_view_user", user_id=user_id))

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
        # Update trial settings - these are the only settings.html fields actually
        # read back anywhere else in the app (see get_system_config("free_trial_*")
        # call sites). Every other field this route used to accept (site_name,
        # site_url, support_email, currency, razorpay_enabled, max_login_attempts,
        # session_timeout, maintenance_mode, allow_registration,
        # require_email_verification, login_notifications, payment_mode) was
        # confirmed to have zero runtime effect anywhere in the codebase - written
        # to SystemConfig but never read back. Rather than pretend those controls
        # do something, the settings.html form now marks them read-only/"not active
        # in V1" and this route no longer processes them, so a save can never
        # silently overwrite a previously-set value with a disabled input's default.
        free_trial_projects = request.form.get("free_trial_projects", type=int)
        free_trial_scans = request.form.get("free_trial_scans", type=int)
        free_trial_days = request.form.get("free_trial_days", type=int)

        set_system_config("free_trial_projects", free_trial_projects, "integer", "Free trial project limit")
        set_system_config("free_trial_scans", free_trial_scans, "integer", "Free trial scan limit")
        set_system_config("free_trial_days", free_trial_days, "integer", "Free trial duration in days")

        # Log activity
        log_admin_activity(admin.id, "settings_update", "Updated system settings")
        
        flash("Settings updated successfully.", "success")
        return redirect(url_for("admin_settings"))
    
    return render_template("admin/settings.html",
                         admin=admin,
                         get_system_config=get_system_config)

# --------------------------------------------------------------------------------------------
# Admin Routes - Capacity (V1 paid-account capacity gate). Read-and-safely-edit-two-fields
# UI only - reuses the exact same read query as the `capacity-status` CLI command
# (_capacity_state_snapshot()) and never touches consumed_count directly, which stays
# derived from PaymentReservation rows via the atomic reserve/release helpers above.
# --------------------------------------------------------------------------------------------
@app.route("/admin/capacity", methods=["GET", "POST"])
@require_admin_permission("superadmin.capacity.manage")
def admin_capacity():
    admin = current_admin()
    config = _get_or_create_capacity_config()

    if request.method == "POST":
        configured_limit_raw = (request.form.get("configured_limit") or "").strip()
        enabled = request.form.get("enabled") == "on"

        try:
            configured_limit = int(configured_limit_raw)
            if configured_limit < 1:
                raise ValueError()
        except ValueError:
            flash("Configured limit must be a positive integer.", "error")
            return redirect(url_for("admin_capacity"))

        old_limit, old_enabled = config.configured_limit, config.enabled
        config.configured_limit = configured_limit
        config.enabled = enabled
        db.session.commit()

        log_admin_activity(
            admin.id, "capacity_config_update",
            f"Updated capacity config: limit {old_limit} -> {configured_limit}, enabled {old_enabled} -> {enabled}"
        )
        flash("Capacity settings updated.", "success")
        return redirect(url_for("admin_capacity"))

    snapshot = _capacity_state_snapshot()
    return render_template("admin/capacity.html", admin=admin, snapshot=snapshot)


def _safe_basename(value):
    if not value:
        return "-"
    return os.path.basename(str(value))


def _smtp_diagnostics_payload():
    errors = []
    try:
        port = _smtp_port() if os.environ.get("SMTP_PORT") else None
    except Exception as exc:
        port = None
        errors.append(str(exc))
    try:
        security = _smtp_security_mode()
    except Exception as exc:
        security = None
        errors.append(str(exc))
    try:
        timeout = _smtp_timeout_seconds()
    except Exception as exc:
        timeout = None
        errors.append(str(exc))
    return {
        "configured": all(os.environ.get(key) for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM")),
        "host_configured": bool(os.environ.get("SMTP_HOST")),
        "port": port,
        "security": security,
        "timeout_seconds": timeout,
        "mail_from_configured": bool(os.environ.get("MAIL_FROM")),
        "errors": errors,
    }


def _rq_diagnostics_payload():
    payload = {
        "available": False,
        "mode": None,
        "redis_configured": bool(os.environ.get("REDIS_URL")),
        "queue_name": None,
        "timeout_seconds": None,
        "pending_count": None,
        "running_count": None,
        "failed_count": None,
        "error": None,
    }
    try:
        summary = queue_config_summary()
        payload.update(summary)
        payload["available"] = redis_ready_check()
        if summary["mode"] == "rq" and os.environ.get("REDIS_URL"):
            from redis import Redis
            from rq import Queue
            from rq.registry import FailedJobRegistry, StartedJobRegistry

            conn = Redis.from_url(os.environ["REDIS_URL"])
            queue = Queue(summary["queue_name"], connection=conn)
            payload["pending_count"] = queue.count
            payload["running_count"] = StartedJobRegistry(summary["queue_name"], connection=conn).count
            payload["failed_count"] = FailedJobRegistry(summary["queue_name"], connection=conn).count
        else:
            payload["pending_count"] = ProcessingJob.query.filter(ProcessingJob.status.in_(("queued", "retrying"))).count()
            payload["running_count"] = ProcessingJob.query.filter(ProcessingJob.status.in_(("processing", "running", "claimed"))).count()
            payload["failed_count"] = ProcessingJob.query.filter_by(status="failed").count()
    except Exception as exc:
        payload["error"] = safe_error_summary(exc)
    return payload


@app.route("/admin/operations", methods=["GET"])
@require_admin_permission("superadmin.operations.view")
def admin_operations():
    addon_purchases = (
        AddonPurchase.query
        .order_by(AddonPurchase.updated_at.desc(), AddonPurchase.id.desc())
        .limit(25)
        .all()
    )
    return render_template(
        "admin/operations.html",
        admin=current_admin(),
        upload_sessions=(
            UploadSession.query
            .order_by(UploadSession.updated_at.desc(), UploadSession.id.desc())
            .limit(25)
            .all()
        ),
        processing_jobs=(
            ProcessingJob.query
            .order_by(ProcessingJob.updated_at.desc(), ProcessingJob.id.desc())
            .limit(25)
            .all()
        ),
        addon_purchases=addon_purchases,
        # Same backend eligibility function the POST route enforces, resolved
        # per row. The template never decides eligibility for itself.
        addon_refund_eligibility={
            purchase.id: refund_eligibility_for_addon_purchase(purchase)
            for purchase in addon_purchases
        },
        addon_refunds={
            purchase.id: _existing_refund_for_source(addon_purchase=purchase)
            for purchase in addon_purchases
        },
        # The refund attention worklist (PAY-2). Deliberately the SAME predicate
        # `flask reconcile-refunds` and /admin/api/refunds?needs_attention=1 use
        # (stuck_refund_filter, P1-6), so the screen, the CLI and the API can
        # never disagree about what is outstanding. A settled refund
        # (REFUNDED + APPLIED) matches neither branch and is excluded by the
        # predicate itself, not by the template.
        attention_refunds=stuck_refund_query().limit(50).all(),
        # Provider-dashboard refunds with no local PaymentRefund row by design
        # (P1-7). Ids and the correlated local source only.
        out_of_band_refunds=unlinked_out_of_band_refund_events(),
        entitlement_transactions=(
            EntitlementTransaction.query
            .order_by(EntitlementTransaction.created_at.desc(), EntitlementTransaction.id.desc())
            .limit(25)
            .all()
        ),
        entitlement_users=(
            User.query
            .order_by(User.updated_at.desc(), User.id.desc())
            .limit(25)
            .all()
        ),
        rq_diagnostics=_rq_diagnostics_payload(),
        smtp_diagnostics=_smtp_diagnostics_payload(),
        safe_basename=_safe_basename,
    )

# --------------------------------------------------------------------------------------------
# Admin Routes - Razorpay Webhook Events (read-only). Never displays raw payload,
# signature, or secrets - only the metadata columns RazorpayWebhookEvent stores.
# --------------------------------------------------------------------------------------------
@app.route("/admin/webhook-events", methods=["GET"])
@require_admin_permission("admin.payments.view")
def admin_webhook_events():
    admin = current_admin()

    order_id = request.args.get("order_id", type=int)
    query = RazorpayWebhookEvent.query
    order = None
    if order_id:
        order = PaymentOrder.query.get_or_404(order_id)
        query = query.filter_by(payment_order_id=order.id)

    per_page = admin_page_size()
    pagination = query.order_by(RazorpayWebhookEvent.received_at.desc()).paginate(
        page=max(request.args.get("page", 1, type=int), 1),
        per_page=per_page,
        error_out=False,
    )

    return render_template(
        "admin/webhook_events.html",
        admin=admin,
        events=pagination.items,
        pagination=pagination,
        per_page=per_page,
        order=order,
    )

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
        direct_qr_supported=direct_qr_experience_supported(),
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
                    image_file, TMP_UPLOADS_DIR, _ent.MAX_IMAGE_SIZE, _ent.MAX_IMAGE_DIMENSION_PX, _ent.MAX_IMAGE_PIXELS
                )
            except UploadValidationError as exc:
                app.logger.warning(f"Admin upload rejected (image, pair {i}): {exc.detail}")
                raise
            try:
                vid_temp, vid_ext = validate_video(
                    video_file, TMP_UPLOADS_DIR, _ent.MAX_VIDEO_SIZE, _ent.MAX_VIDEO_DURATION_SECONDS
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
    db.session.flush()
    add_project_service_coverage(
        project,
        "ADMIN_GRANT",
        created_by_admin=admin,
        reason="Admin-created project public service coverage.",
    )
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
        db.session.flush()
        # Ledger row so deletion/reconciliation see this media, recorded
        # UNCOUNTED: an admin-owned project bills no subscriber account.
        record_pair_media_objects(
            project, pair,
            image_bytes=os.path.getsize(img_path),
            video_bytes=os.path.getsize(vid_path),
        )

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
        
        job = _schedule_project_pair_processing(project.id)
        if not job:
            for pair in ProjectPair.query.filter_by(project_id=project.id).all():
                pair.processing_status = "failed"
                pair.feature_extraction_status = "failed"
                pair.processing_error = "Processing queue unavailable"
            db.session.commit()
            return redirect(url_for("admin_success_page", project_id=project.id))
        
    except Exception as e:
        print(f"Admin background queue failed: {e}")
    
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
    if not pair.image_filename:
        abort(404)
    file_path = os.path.join(ADMIN_IMAGES_DIR, pair.image_filename)
    if not os.path.exists(file_path):
        print(f"❌ Admin image not found: {file_path}")
        abort(404)
    
    response = send_from_directory(ADMIN_IMAGES_DIR, pair.image_filename)
    return _apply_short_private_cache(response)
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
    return _apply_short_private_cache(response)

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
    return _apply_short_private_cache(response)

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

    blocked = project_deletion_block_reason(project)
    if blocked:
        flash(blocked, "error")
        return redirect(url_for("admin_projects"))

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
    # Run the app.
    # NOTE: this is the Werkzeug development server. Production deployments
    # must run behind a real WSGI server (gunicorn, waitress, etc.) - never
    # via `python app.py`. Apply schema first with `flask --app app db upgrade`;
    # this entry point never creates tables. debug/use_reloader only activate
    # when FLASK_DEBUG=1 is explicitly set (and are always off when
    # SCANSTORY_TESTING=1).
    app.run(host="0.0.0.0", port=5000, debug=FLASK_DEBUG_ENABLED, use_reloader=FLASK_DEBUG_ENABLED)
