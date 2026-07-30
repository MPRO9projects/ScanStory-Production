import os
import sys
import time
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
from dotenv import load_dotenv
import cv2
import numpy as np
import qrcode
from qrcode.image.styledpil import StyledPilImage
from PIL import Image, ImageDraw, ImageFile, ImageFont
import ffmpeg
import secrets
import hashlib
import traceback
from concurrent.futures import ThreadPoolExecutor
import logging
import requests

from sqlalchemy import or_, desc, func, and_, case

# ✅ Import models
from models import (
    db, User, Admin, SubscriptionPlan, TrialDetails, OTPCode,
    Project, ProjectPair, PaymentOrder, ScanLog, SystemConfig,
    UserLoginActivity, AdminActivity
)

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

# ✅ ADD THESE 2 LINES TO DISABLE CSRF
app.config['WTF_CSRF_ENABLED'] = False
app.config['WTF_CSRF_CHECK_DEFAULT'] = False

# Secret key (only set once)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")

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
    DEBUG=False if SCANSTORY_TESTING else app.debug,
    SQLALCHEMY_DATABASE_URI=database_uri,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS=engine_options
)

# ✅ Initialize SQLAlchemy ONLY ONCE
db.init_app(app)
from experience_creator import experience_creator_bp

app.register_blueprint(experience_creator_bp)

# Ensure correct MIME type for wasm
mimetypes.add_type("application/wasm", ".wasm")
ImageFile.LOAD_TRUNCATED_IMAGES = True

RECAPTCHA_SITE_KEY = os.environ.get("RECAPTCHA_SITE_KEY", "")
RECAPTCHA_SECRET_KEY = os.environ.get("RECAPTCHA_SECRET_KEY", "")
RECAPTCHA_MIN_SCORE = float(os.environ.get("RECAPTCHA_MIN_SCORE", "0.5"))

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


def add_security_headers(response):
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    if request.path.startswith("/scanner"):
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    else:
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"

    return response    


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

for d in (DATA_DIR, IMAGES_DIR, VIDEOS_DIR, FEATURES_DIR, QR_DIR, STATIC_UPLOADS_DIR, STATIC_JS_DIR, LOGOS_DIR, ADMIN_UPLOADS_DIR):
    os.makedirs(d, exist_ok=True)

ADMIN_DATA_DIR = os.environ.get("SCANSTORY_ADMIN_DATA_DIR", os.path.join(BASE_DIR, "data_admin"))
ADMIN_IMAGES_DIR = os.path.join(ADMIN_DATA_DIR, "images")
ADMIN_VIDEOS_DIR = os.path.join(ADMIN_DATA_DIR, "videos")
ADMIN_FEATURES_DIR = os.path.join(ADMIN_DATA_DIR, "features")
ADMIN_QR_DIR = os.path.join(ADMIN_DATA_DIR, "qr_codes")
for d in [ADMIN_DATA_DIR, ADMIN_IMAGES_DIR, ADMIN_VIDEOS_DIR, ADMIN_FEATURES_DIR, ADMIN_QR_DIR]:
    os.makedirs(d, exist_ok=True)

# --------------------------------------------------------------------------------------------
# Bootstrap (tables + default plans + initial admin + system config)
# --------------------------------------------------------------------------------------------
with app.app_context():
    db.create_all()
    
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
    
    # Create initial admin
    if Admin.query.count() == 0:
        admin_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@scanstory.com")
        admin_pass = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "Admin@123")
        db.session.add(Admin(
            email=admin_email.strip().lower(),
            password_hash=generate_password_hash(admin_pass),
            name="Super Admin",
            role="superadmin"
        ))
    
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

def _generate_otp() -> str:
    return f"{secrets.randbelow(1000000):06d}"

def _create_otp(email: str, purpose: str, minutes: int = 2) -> str:
    OTPCode.query.filter_by(email=email, purpose=purpose).delete()
    db.session.commit()
    code = _generate_otp()
    otp = OTPCode(
        email=email,
        code=code,
        purpose=purpose,
        expires_at=dt.utcnow() + timedelta(minutes=minutes),
    )
    db.session.add(otp)
    db.session.commit()
    return code

def _verify_otp(email: str, purpose: str, code: str) -> bool:
    rec = OTPCode.query.filter_by(email=email, purpose=purpose, code=code).first()
    if not rec:
        return False
    if dt.utcnow() > rec.expires_at:
        return False
    db.session.delete(rec)
    db.session.commit()
    return True

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

def logout_user():
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
    session["admin_role"] = admin.role

def admin_logout():
    session.pop("admin_id", None)
    session.pop("admin_email", None)
    session.pop("admin_role", None)

def current_admin():
    aid = session.get("admin_id")
    if not aid:
        return None
    return Admin.query.get(aid)

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_admin():
            flash("Please login as admin to access this page.", "error")
            return redirect(url_for("admin_login_route"))
        return view(*args, **kwargs)
    return wrapped

def super_admin_required(view):
    @wraps(view)
    @admin_required
    def wrapped(*args, **kwargs):
        admin = current_admin()
        if admin.role != "superadmin":
            flash("Access denied. Super admin privileges required.", "error")
            return redirect(url_for("admin_dashboard"))
        return view(*args, **kwargs)
    return wrapped

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


def get_plan_pairs_limit(user):
    """Return the configured max pairs per project for the user's current plan."""
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


def check_user_limits(user):
    """
    Single source of truth enforcement:
    - None / NULL limit means unlimited
    - Numeric limit is enforced
    """
    if user.is_blocked:
        return False, url_for("login"), "Account is blocked"

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

# --------------------------------------------------------------------------------------------
# CV/QR functions (same as before)
# --------------------------------------------------------------------------------------------
MAX_IMAGE_SIZE = 50 * 1024 * 1024
MAX_VIDEO_SIZE = 1 * 1024 * 1024 * 1024
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
    
    # Create initial super admin
    if Admin.query.count() == 0:
        admin_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@scanstory.com")
        admin_pass = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "Admin@123")
        super_admin = Admin(
            email=admin_email.strip().lower(),
            password_hash=generate_password_hash(admin_pass),
            name="Super Admin",
            role="superadmin",
            is_active=True
        )
        db.session.add(super_admin)
    
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

def match_best_variant(test_desc, feats, ratio=0.75):
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
            good = []
            for m_n in knn:
                if len(m_n) != 2:
                    continue
                m, n = m_n
                if m.distance < ratio_try * n.distance:
                    good.append(m)

            if len(good) > len(best[1]):
                best = (tag, good, skp if skp is not None else stored_kp)

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

def make_feature_working_jpeg(src_path: str, out_path: str, max_dim: int = ORB_MAX_DIM, jpeg_quality: int = 92) -> str:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
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
    response.headers["Cache-Control"] = "no-store"
    return response



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

        trial = None
        changed = False

        # Handle TRIAL and LIMIT_REACHED users
        if user.subscription_status in ("trial", "limit_reached"):
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
            admin_view=admin_view  # Pass this to template if needed
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
            if _too_big(new_image, MAX_IMAGE_SIZE):
                flash(f"Image for pair {pair.pair_index + 1} is too large.", "error")
                return redirect(url_for("user_edit_project_page", project_id=project_id))
            img_path = os.path.join(IMAGES_DIR, pair.image_filename)
            new_image.save(img_path)
            standardize_uploaded_image(img_path, target_size=1200)
            pair.is_processed = False
            pair.processing_status = "uploaded"
            pair.feature_extraction_status = "pending"
            pair.processing_error = None
            updated += 1

        if new_video and new_video.filename:
            if _too_big(new_video, MAX_VIDEO_SIZE):
                flash(f"Video for pair {pair.pair_index + 1} is too large.", "error")
                return redirect(url_for("user_edit_project_page", project_id=project_id))
            vid_path = os.path.join(VIDEOS_DIR, pair.video_filename)
            new_video.save(vid_path)
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
        code = _create_otp(email, "verify_email", minutes=2)
        try:
            send_email_verification_otp(email, code, minutes=2)
            flash("OTP sent to your email. Please verify to continue.", "success")
        except Exception as e:
            flash(f"OTP created but email sending failed: {str(e)}", "error")

        session["pending_verify_email"] = email
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
    if not _verify_otp(email, "verify_email", otp):
        flash("Invalid or expired OTP. Please try again.", "error")
        return render_template("user/verify_email.html", email=email)
    
    user = User.query.filter_by(email=email).first()
    if not user:
        flash("Account not found. Please register again.", "error")
        return redirect(url_for("register"))
    
    user.is_verified = True
    user.email_verified_at = dt.utcnow()
    db.session.commit()
    
    session.pop("pending_verify_email", None)
    flash("Email verified successfully. You can now login.", "success")
    return redirect(url_for("login"))

@app.route("/resend-otp/", methods=["GET"])
def resend_otp():
    email = session.get("pending_verify_email")
    if not email:
        flash("No verification session found.", "error")
        return redirect(url_for("register"))
    
    code = _create_otp(email, "verify_email", minutes=2)
    try:
        send_email_verification_otp(email, code, minutes=2)
        flash("A new OTP has been sent to your email.", "success")
    except Exception as e:
        flash(f"Email sending failed: {str(e)}", "error")
    
    return redirect(url_for("verify_email"))

@app.route("/login/", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("user/login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

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
    if user.subscription_status == "trial":
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
    
    email = (request.form.get("email") or "").strip().lower()
    user = User.query.filter_by(email=email).first()
    
    if user:
        try:
            code = _create_otp(email, "reset_password", minutes=2)
            send_reset_password_otp(email, code, minutes=2)
            flash("Password reset OTP has been sent to your email.", "success")
        except Exception as e:
            print(f"❌ Forgot password email error: {e}")
            flash("Could not send email. Please try again later or contact support.", "error")
            return redirect(url_for("forgot_password"))
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
    
    if not _verify_otp(email, "reset_password", otp):
        flash("Invalid or expired OTP.", "error")
        return render_template("user/reset_password.html", email=email)
    
    user = User.query.filter_by(email=email).first()
    if user:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
    
    session.pop("pending_reset_email", None)
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

    # enforce_subscription already checked.
    # This is just an extra safety check (optional)
    if not user.can_create_project:
        flash("Project limit reached. Please upgrade your plan.", "error")
        return redirect(url_for("subscribe_page"))

    max_pairs_per_project = get_plan_pairs_limit(user)
    if max_pairs_per_project is None:
        flash("Pairs allowed per project is not configured for your current plan. Please contact admin.", "error")
        return redirect(url_for("subscribe_page"))

    return render_template("user/user_create_project.html", user=user, max_pairs_per_project=max_pairs_per_project)



@app.route("/upload", methods=["POST"])
@login_required
@enforce_subscription
def handle_upload():
    """Optimized project creation with background processing for MULTIPLE PAIRS"""
    user = current_user()

    if not user.can_create_project:
        flash("Project limit reached. Please upgrade your plan.", "error")
        return redirect(url_for("user_create_project_page"))

    t0 = time.time()

    # Get project name and uploaded files
    name = request.form.get("name", "Untitled Project")
    images = request.files.getlist("images")
    videos = request.files.getlist("videos")

    # Validation
    if not images or not videos or len(images) != len(videos):
        flash("Error: Please upload equal number of images and videos", "error")
        return redirect(url_for("user_create_project_page"))

    # Get max pairs based on subscription plan only
    max_pairs = get_plan_pairs_limit(user)
    if max_pairs is None:
        flash("Pairs allowed per project is not configured for your current plan. Please contact admin.", "error")
        return redirect(url_for("user_create_project_page"))

    if len(images) > max_pairs:
        flash(f"Your current plan allows maximum {max_pairs} pairs per project.", "error")
        return redirect(url_for("user_create_project_page"))

    # Quick file size check (FAST - doesn't read entire file)
    for image_file in images:
        if image_file.content_length and image_file.content_length > MAX_IMAGE_SIZE:
            flash("Image file exceeds allowed size limit.", "error")
            return redirect(url_for("user_create_project_page"))

    for video_file in videos:
        if video_file.content_length and video_file.content_length > MAX_VIDEO_SIZE:
            flash("Video file exceeds allowed size limit.", "error")
            return redirect(url_for("user_create_project_page"))

    # ✅ STEP 1: Create project record (FAST)
    # Assign a per-user project index so each user sees projects numbered 1,2,3...
    try:
        # Use the maximum existing per-user index to avoid issues with deleted rows
        max_index = db.session.query(func.max(Project.user_project_index)).filter(
            Project.owner_user_id == user.id
        ).scalar()
        user_project_index = (int(max_index) if max_index and int(max_index) > 0 else 0) + 1
    except Exception:
        # Fallback to simple count if something goes wrong
        try:
            existing_count = Project.query.filter_by(owner_user_id=user.id).count()
        except Exception:
            existing_count = 0
        user_project_index = int(existing_count or 0) + 1

    project = Project(name=name, owner_user_id=user.id, user_project_index=user_project_index)
    db.session.add(project)
    db.session.commit()

    # ✅ STEP 2: Save ALL files quickly with standardization
    pairs_data = []
    for i, (image_file, video_file) in enumerate(zip(images, videos)):
        # Generate filenames
        img_filename = f"{project.id}_{i}.jpg"
        vid_ext = os.path.splitext(video_file.filename or "")[1].lower() or ".mp4"
        vid_filename = f"{project.id}_{i}{vid_ext}"
        
        # Save files (FAST)
        img_path = os.path.join(IMAGES_DIR, img_filename)
        image_file.save(img_path)
        
        # ✅ FIX: Standardize image to 1200px (match ORB_MAX_DIM)
        standardize_uploaded_image(img_path, target_size=1200)
        
        vid_path = os.path.join(VIDEOS_DIR, vid_filename)
        video_file.save(vid_path)
        
        # Create pair record (NOT processed)
        pair = ProjectPair(
            project_id=project.id,
            pair_index=i,
            image_filename=img_filename,
            video_filename=vid_filename,
            image_path=f"/image/{project.id}/{i}",
            is_processed=False,
            processing_status="uploaded",
            feature_extraction_status="pending",
            processing_error=None
        )
        db.session.add(pair)
        
        # Store data for background processing
        pairs_data.append({
            "pair_index": i,
            "image_filename": img_filename,
            "video_filename": vid_filename
        })

    # ✅ STEP 3: Update user count
    user.projects_used = int(user.projects_used or 0) + 1
    db.session.commit()

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
        
        def process_single_pair_bg(project_id, pair_index, img_filename):
            """Process ONE pair in background - YOUR EXACT LOGIC"""
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
                
                # Process this single pair
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
                
                return True
                
            except Exception as e:
                print(f"[BG ERROR] Failed pair {pair_index}: {e}")
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
        
        def background_processing_all_pairs(project_id, all_pairs_data):
            """Process ALL pairs in parallel"""
            with app.app_context():
                try:
                    proj = Project.query.get(project_id)
                    display_pid = proj.user_project_index if proj and proj.user_project_index else project_id
                    print(f"[BG START] Processing {len(all_pairs_data)} pairs for project {display_pid} (global {project_id})")
                    
                    # Process pairs in parallel for speed
                    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                        futures = []
                        for pair_data in all_pairs_data:
                            future = executor.submit(
                                process_single_pair_bg,
                                project_id,
                                pair_data["pair_index"],
                                pair_data["image_filename"]
                            )
                            futures.append(future)
                        
                        # Wait for all to complete
                        results = [f.result() for f in futures]
                        successful = sum(results)
                        
                        proj = Project.query.get(project_id)
                        display_pid = proj.user_project_index if proj and proj.user_project_index else project_id
                        print(f"[BG DONE] Project {display_pid} (global {project_id}): {successful}/{len(all_pairs_data)} pairs processed")
                    
                    # Clear feature cache
                    load_features.cache_clear()
                    
                except Exception as e:
                    print(f"[BG FATAL ERROR] {e}")
                    import traceback
                    traceback.print_exc()
        
        # Start background processing
        thread = threading.Thread(
            target=background_processing_all_pairs,
            args=(project.id, pairs_data),
            daemon=True
        )
        thread.start()
        
        print(f"[UPLOAD] Started background processing for {len(pairs_data)} pairs")
        
    except Exception as e:
        print(f"Failed to start background processing: {e}")

    display_pid = project.user_project_index if project and project.user_project_index else project.id
    print(f"[UPLOAD COMPLETE] Project {display_pid} (global {project.id}) created in {time.time() - t0:.2f}s with {len(pairs_data)} pairs")

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
    return render_template("user/subscribe.html", plans=plans, user=user, get_system_config=get_system_config)


@app.route("/subscribe", methods=["GET"])
@login_required
def subscribe_page():
    """Show subscription plans"""
    user = current_user()
    plans = SubscriptionPlan.query.filter_by(is_active=True).order_by(SubscriptionPlan.display_order.asc()).all()
    
    return render_template("user/subscribe.html", 
                         plans=plans, 
                         user=user,
                         get_system_config=get_system_config)

@app.route("/create-razorpay-order", methods=["POST"])
@login_required
def create_razorpay_order():
    """Create Razorpay order for subscription"""
    user = current_user()
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
    
    # Calculate amount in paise (Razorpay expects amount in smallest currency unit)
    try:
        amount_paise = int(plan.effective_price * 100)
        if amount_paise < 100:  # Minimum amount for Razorpay is 100 paise (₹1)
            amount_paise = 100
    except Exception as e:
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
        
        print(f"📋 Creating Razorpay order: {order_data}")
        
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
        print(f"❌ Razorpay Bad Request: {e}")
        return jsonify({"success": False, "error": f"Invalid request to payment gateway: {str(e)}"})
    except razorpay.errors.AuthenticationError as e:
        print(f"❌ Razorpay Authentication Error: {e}")
        return jsonify({"success": False, "error": "Payment gateway authentication failed. Please check API keys."})
    except Exception as e:
        print(f"❌ Razorpay order creation failed: {e}")
        return jsonify({"success": False, "error": f"Payment gateway error: {str(e)}"})

@app.route("/verify-payment", methods=["POST"])
@login_required
def verify_payment():
    """Verify Razorpay payment and activate subscription"""
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
        # Verify payment signature
        razorpay_client.utility.verify_payment_signature(params_dict)
        
        # Get payment order from database
        payment_order = PaymentOrder.query.filter_by(razorpay_order_id=razorpay_order_id).first()
        if not payment_order or payment_order.user_id != user.id:
            return jsonify({"success": False, "error": "Invalid payment order"})
        
        # Get plan details
        plan = SubscriptionPlan.query.get(payment_order.plan_id)
        if not plan:
            return jsonify({"success": False, "error": "Plan not found"})
        
        # Update payment order
        payment_order.razorpay_payment_id = razorpay_payment_id
        payment_order.razorpay_signature = razorpay_signature
        payment_order.status = "success"
        payment_order.payment_at = dt.utcnow()
        
        # Set subscription period
        payment_order.subscription_start = dt.utcnow()
        if plan.duration_type == "time":
            payment_order.subscription_end = dt.utcnow() + timedelta(days=plan.duration_value * 30)
        else:
            # For count-based plans, set far future date
            payment_order.subscription_end = dt.utcnow() + timedelta(days=365 * 10)  # 10 years
        
        # Update user subscription
        user.subscription_id = plan.id
        user.subscription_taken_at = dt.utcnow()
        user.subscription_expires_at = payment_order.subscription_end
        user.subscription_status = "active"
        user.subscribed_project_limit = plan.total_project_limit
        user.subscribed_scan_limit = plan.total_scan_limit
        user.projects_used = 0  # Reset for new subscription
        user.scans_used = 0
        
        # Update trial details if exists
        trial = TrialDetails.query.filter_by(user_id=user.id).first()
        if trial:
            trial.trial_converted = True
            trial.converted_at = dt.utcnow()
            trial.converted_plan_id = plan.id
        
        db.session.commit()
        
        # Send success email
        try:
            send_payment_success_email(user, plan, payment_order)
        except Exception as e:
            print(f"Failed to send payment success email: {e}")
        
        return jsonify({
            "success": True,
            "message": "Payment verified successfully",
            "order_id": payment_order.order_id,
            "plan_name": plan.plan_name
        })
        
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"success": False, "error": "Invalid payment signature"})
    except Exception as e:
        print(f"Payment verification failed: {e}")
        return jsonify({"success": False, "error": str(e)})

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
        projects_url=url_for("projects_page")
    )

# --------------------------------------------------------------------------------------------
# Scanner Routes (Public)
# --------------------------------------------------------------------------------------------

@app.route("/video/<int:project_id>/<int:image_id>")
def serve_video(project_id, image_id):
    project = Project.query.get(project_id)
    if not project:
        return "Project not found"
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        return "Pair not found"
    
    return send_from_directory(VIDEOS_DIR, pair.video_filename)

@app.route("/image/<int:project_id>/<int:image_id>")
def serve_image(project_id, image_id):
    project = Project.query.get(project_id)
    if not project:
        return "Project not found"
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        return "Pair not found"
    
    return send_from_directory(IMAGES_DIR, pair.image_filename)

@app.route("/qr/<filename>")
def serve_qr(filename):
    return send_from_directory(QR_DIR, filename)

@app.route("/scanner/<int:project_id>")
def scanner(project_id):
    """Public scanner - handles both user and admin projects"""
    user_id = request.args.get("user_id", type=int)
    admin_id = request.args.get("admin_id", type=int)
    user_name = request.args.get("user_name")
    admin_name = request.args.get("admin_name")
    
    # ✅ FIX: If user_id is in URL, set it in session
    # if user_id and not session.get("user_id"):
    #     session["user_id"] = user_id
    #     print(f"✅ Auto-logged in user {user_id} from QR code")

    # ✅ FIX: ALWAYS set user_id from URL into session
    if user_id:
        session["user_id"] = user_id
        session.permanent = True
        print(f"✅ FORCE set user_id {user_id} in session from QR code")
    else:
        print(f"❌ No user_id in URL - scans will not count")
    
    
    project = Project.query.get(project_id)
    
    if not project:
        return "Project not found"
    
    # Determine creator info
    if project.owner_user_id:
        creator_type = "user"
        creator_id = project.owner_user_id
        creator_name = project.owner_user.full_name if project.owner_user else "User"
    else:
        creator_type = "admin"
        creator_id = project.owner_admin_id
        creator_name = project.owner_admin.name if project.owner_admin else "Admin"
    
    return render_template(
        "user/scanner.html",
        project_id=project_id,
        project_name=project.name,
        qr_code_url=project.qr_code_path,
        user_id=user_id,
        admin_id=admin_id,
        user_name=user_name,
        admin_name=admin_name,
        creator_type=creator_type,
        creator_name=creator_name
    )
@app.route("/detect_init", methods=["POST"])
def detect_init():
    """Public detection with multi-pair support"""
    try:
        print("\n" + "="*50)
        print("🔍 DETECT_INIT CALLED")
        print("="*50)
        import sys; sys.stdout.flush()
        t_start = time.time()
        
        project_id = request.form.get("project_id", type=int)
        test_file = request.files.get("test_image")
        
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
                "ready_pairs": 0
            }), 200
        
        # Get scan session info
        user_id = session.get("user_id")
        scan_session_id = request.form.get("scan_session_id")
        
        print(f"👤 user_id: {user_id}")
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
        if user_id:
            user = User.query.get(user_id)
            print(f"👤 User found: {user is not None}")
            
            if user:
                # Check if a log already exists for this session
                existing_log = ScanLog.query.filter_by(
                    user_id=user_id,
                    scan_session_id=scan_session_id
                ).first()
                
                print(f"📝 Existing log for this session: {existing_log is not None}")
                
                if not existing_log:
                    scan_log = ScanLog(
                        project_id=project_id,
                        user_id=user_id,
                        scan_session_id=scan_session_id,
                        is_successful=False,
                        scan_type="admin" if is_admin_project else "user"
                    )
                    db.session.add(scan_log)
                    db.session.commit()
                    print(f"✅ Created NEW scan log for session {scan_session_id}")
                else:
                    scan_log = existing_log
                    print(f"✅ Using EXISTING scan log for session {scan_session_id}")
                
                # ✅ ONLY check scan limits for USER projects (not admin projects)
                if not is_admin_project:
                    if not user.can_scan:
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
            return jsonify({"detected": False, "reason": "Invalid image"}), 400
        
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
                "frame_height": frame_h
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
        
        for pid in top_ids:
            feats = load_features(project_id, pid)
            if feats is None:
                continue
            
            best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.75)
            
            if not good_matches or len(good_matches) < MIN_GOOD_MATCHES:
                best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.80)
            
            if not good_matches or len(good_matches) < MIN_GOOD_MATCHES:
                best_tag, good_matches, stored_kp = match_best_variant(test_desc, feats, ratio=0.90)
            
            if good_matches and len(good_matches) > best_good:
                best_good = len(good_matches)
                best_match = (best_tag, good_matches, stored_kp, feats)
                best_match_id = pid
                print(f"  - Pair {pid}: {len(good_matches)} good matches")
        t_after_match = time.time()
        print(f"⏱ match_time={(t_after_match - t_after_quick):.3f}s; best_good={best_good}")
        
        if not best_match or best_good < MIN_GOOD_MATCHES:
            print(f"❌ Detection failed: best_good={best_good}")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({
                "detected": False, 
                "reason": f"Mobile detection failed: Found {best_good} matches",
                "frame_width": frame_w, 
                "frame_height": frame_h
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
        
        H, mask = cv2.findHomography(src_arr, dst_arr, cv2.RANSAC, RANSAC_REPROJ)
        if H is None or mask is None:
            print(f"❌ Homography failed")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({"detected": False, "reason": "Homography failed"}), 200
        
        inliers = int(np.sum(mask))
        min_inliers_needed = max(MIN_INLIERS_ABS, int(MIN_INLIERS_RATIO * len(src_arr)))
        print(f"📐 Inliers: {inliers}/{len(src_arr)} (need >={min_inliers_needed})")

        if inliers < min_inliers_needed:
            print(f"❌ Weak homography: {inliers} inliers < required {min_inliers_needed}")
            if scan_log and not is_admin_project:
                db.session.commit()
            return jsonify({"detected": False, "reason": "Weak homography"}), 200
        
        tw, th = feats["w"], feats["h"]
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
        
        if project.owner_admin_id:
            matched_video_url = url_for("serve_admin_video", project_id=project_id, image_id=best_match_id, _external=True,_scheme="https")
        else:
            matched_video_url = url_for("serve_video", project_id=project_id, image_id=best_match_id, _external=True,_scheme="https")
        
        print(f"✅ Detection successful! Returning response")
        print("="*50 + "\n")
        
        return jsonify({
            "detected": True,
            "matched_pair_id": best_match_id,
            "video_url": matched_video_url,
            "corners": corners_out,
            "init_points": points_out,
            "frame_width": frame_w,
            "frame_height": frame_h,
            "variant": best_tag,
            "inliers": inliers,
            "top_checked": top_ids,
            "scan_session_id": scan_session_id if user_id else None,
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
def scanner_session_end():
    """End scanner session - COUNT ONLY ONCE here"""
    try:
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
        user_id = session.get("user_id")

        project = Project.query.get(int(project_id)) if project_id else None

        if project and project.owner_admin_id:
            print("✅ Admin project session end - not counting scan")
            return jsonify({
                "ok": True,
                "counted": False,
                "reason": "Admin project - unlimited scans"
            })
        
        print(f"📌 project_id: {project_id}")
        print(f"📌 session_id: {session_id}")
        print(f"📌 user_id from session: {user_id}")
        
        if not project_id or not session_id:
            return jsonify({"ok": False, "error": "Missing required fields"}), 400
        
        # Only count for logged-in users
        if not user_id:
            print("❌ Guest user - not counting")
            return jsonify({"ok": True, "counted": False, "reason": "Guest user"})
        
        user = User.query.get(user_id)
        if not user:
            print(f"❌ User {user_id} not found")
            return jsonify({"ok": False, "error": "User not found"}), 404
        
        print(f"👤 User found: {user.email}")
        print(f"📊 Current scans_used: {user.scans_used}")
        
        # Check if this session had ANY successful scan
        successful_scan = ScanLog.query.filter_by(
            user_id=user_id,
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
                user_id=user_id,
                scan_session_id=session_id
            ).first()
            if any_log:
                print(f"📝 Found log but is_successful={any_log.is_successful}")
            else:
                print("📝 No logs found for this session")
            
            return jsonify({"ok": True, "counted": False, "reason": "No successful detection"})
        
        # Check if already counted
        if hasattr(successful_scan, 'counted') and successful_scan.counted:
            print("⏭️ Session already counted, skipping")
            return jsonify({"ok": True, "counted": False, "reason": "Already counted"})
        
        # COUNT THE SCAN
        old_count = user.scans_used
        user.scans_used = (user.scans_used or 0) + 1
        
        # Mark as counted
        successful_scan.counted = True
        
        # Update status if limit reached
        if _limit_reached(user.subscribed_scan_limit, user.scans_used):
            user.subscription_status = "limit_reached"
        
        db.session.commit()
        
        print(f"✅ COUNTED: {old_count} → {user.scans_used}")
        print("="*50 + "\n")
        
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
def detect_track():
    """Tracking endpoint - with scan counting"""
    try:
        t_start = time.time()
        project_id = request.form.get("project_id", type=int)
        pair_id = request.form.get("pair_id", type=int)
        test_file = request.files.get("test_image")
        scan_session_id = request.form.get("scan_session_id", "")
        
        if project_id is None or pair_id is None or test_file is None:
            return jsonify({"ok": False, "reason": "Missing project_id/pair_id/image"}), 400
        
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
        rect = np.array([[0, 0], [tw, 0], [tw, th], [0, th]], dtype=np.float32).reshape(-1, 1, 2)
        pts = cv2.perspectiveTransform(rect, H).reshape(4, 2)
        corners = [(float(p[0] / scale), float(p[1] / scale)) for p in pts]
        
        if not valid_corners(corners, frame_w, frame_h):
            return jsonify({"ok": False, "reason": "Bad corners", "frame_width": frame_w, "frame_height": frame_h}), 200
        
        corners_out = [{"x": c[0], "y": c[1]} for c in corners]
        
        return jsonify({
            "ok": True,
            "corners": corners_out,
            "frame_width": frame_w,
            "frame_height": frame_h,
            "variant": best_tag,
            "inliers": inliers
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
    
    if not admin or not check_password_hash(admin.password_hash, password):
        flash("Invalid email or password.", "error")
        return render_template("admin/login.html")
    
    if not admin.is_active:
        flash("Your account is deactivated. Please contact super admin.", "error")
        return render_template("admin/login.html")
    
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
        try:
            send_admin_password_reset_email(email, code, minutes=10)
        except Exception as e:
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
    
    if not _verify_otp(email, "admin_reset_password", otp):
        flash("Invalid or expired OTP.", "error")
        return render_template("admin/reset_password.html", email=email)
    
    admin = Admin.query.filter_by(email=email).first()
    if admin:
        admin.password_hash = generate_password_hash(new_password)
        db.session.commit()
        
        # Log activity
        log_admin_activity(admin.id, "password_reset", "Admin reset password via OTP")
    
    session.pop("pending_admin_reset_email", None)
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
@admin_required
def admin_manage_admins():
    admin = current_admin()
    if admin.role != "superadmin":
        flash("Access denied. Super admin privileges required.", "error")
        return redirect(url_for("admin_dashboard"))
    
    admins = Admin.query.order_by(Admin.created_at.desc()).all()
    return render_template("admin/manage_admins.html", admin=admin, admins=admins)

@app.route("/admin/admins/add", methods=["GET", "POST"])
@super_admin_required
def admin_add_admin():
    admin = current_admin()
    
    if request.method == "GET":
        return render_template("admin/add_admin.html", admin=admin)
    
    # Get form data
    email = (request.form.get("email") or "").strip().lower()
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    role = request.form.get("role", "admin")
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
@super_admin_required
def admin_edit_admin(admin_id):
    admin = current_admin()
    target_admin = Admin.query.get_or_404(admin_id)
    
    if request.method == "GET":
        return render_template("admin/edit_admin.html", admin=admin, target_admin=target_admin)
    
    # Get form data
    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    role = request.form.get("role", "admin")
    is_active = request.form.get("is_active") == "on"
    
    # Validation
    if not name:
        flash("Name is required.", "error")
        return render_template("admin/edit_admin.html", admin=admin, target_admin=target_admin)
    
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
    
    flash("Admin updated successfully.", "success")
    return redirect(url_for("admin_manage_admins"))

@app.route("/admin/admins/<int:admin_id>/delete", methods=["POST"])
@super_admin_required
def admin_delete_admin(admin_id):
    """Delete an admin account"""
    admin = current_admin()
    target_admin = Admin.query.get_or_404(admin_id)
    
    # Prevent self-deletion
    if target_admin.id == admin.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_manage_admins"))
    
    # Prevent deleting the only super admin
    if target_admin.role == "superadmin":
        superadmin_count = Admin.query.filter_by(role="superadmin", is_active=True).count()
        if superadmin_count <= 1:
            flash("Cannot delete the only active super admin.", "error")
            return redirect(url_for("admin_manage_admins"))
    
    # Log activity before deletion
    log_admin_activity(admin.id, "admin_delete", f"Deleted admin: {target_admin.email}")
    
    db.session.delete(target_admin)
    db.session.commit()
    
    flash("Admin deleted successfully.", "success")
    return redirect(url_for("admin_manage_admins"))

@app.route("/admin/admins/<int:admin_id>/toggle-status", methods=["POST"])
@super_admin_required
def admin_toggle_admin_status(admin_id):
    admin = current_admin()
    target_admin = Admin.query.get_or_404(admin_id)
    
    # Prevent self-deactivation
    if target_admin.id == admin.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin_manage_admins"))
    
    # Prevent deactivating the only super admin
    if target_admin.role == "superadmin" and target_admin.is_active:
        superadmin_count = Admin.query.filter_by(role="superadmin", is_active=True).count()
        if superadmin_count <= 1:
            flash("Cannot deactivate the only active super admin.", "error")
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
@admin_required
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
    """Show ALL the logged-in admin's own projects"""
    admin = current_admin()
    
    # Get ALL projects - no limit
    projects = Project.query.filter_by(
        owner_admin_id=admin.id
    ).order_by(Project.created_at.asc()).all()
    
    # Get pairs count and display number for each project
    for idx, p in enumerate(projects, 1):
        p.pairs_count = ProjectPair.query.filter_by(project_id=p.id).count()
        p.scan_count = ScanLog.query.filter_by(project_id=p.id).count()
        p.display_number = idx  # Add sequential number
    
    return render_template("admin/my_projects.html",
                         admin=admin,
                         projects=projects)
@app.route("/admin/users", methods=["GET"])
@admin_required
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
    
    users = query.order_by(User.created_at.desc()).all()
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    
    return render_template("admin/users.html", 
                         admin=admin, 
                         users=users, 
                         plans=plans,
                         status=status,
                         selected_plan_id=plan_id,
                         search=search)

@app.route("/admin/users/<int:user_id>", methods=["GET"])
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
def admin_subscriptions():
    admin = current_admin()
    
    # Get filter parameters
    status = request.args.get("status", "all")
    plan_id = request.args.get("plan_id", type=int)
    search = request.args.get("search", "").strip()
    
    # Build query
    query = PaymentOrder.query.filter_by(status="success")
    
    if status == "active":
        query = query.filter(PaymentOrder.subscription_end > dt.utcnow)
    elif status == "expired":
        query = query.filter(PaymentOrder.subscription_end <= dt.utcnow)
    
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
    
    subscriptions = query.order_by(PaymentOrder.created_at.desc()).all()
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
                         search=search) 

@app.route("/admin/subscriptions/<int:order_id>/extend", methods=["POST"])
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
    
    payments = query.order_by(PaymentOrder.created_at.desc()).all()
    
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
                         total_amount=total_amount,
                         success_count=success_count)

@app.route("/admin/payments/<int:payment_id>", methods=["GET"])
@admin_required
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

# --------------------------------------------------------------------------------------------
# Admin Routes - Module 8: Project Monitoring
# --------------------------------------------------------------------------------------------
@app.route("/admin/projects", methods=["GET"])
@admin_required
def admin_projects():
    """Display all user profiles"""
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
    
    users = query.order_by(User.created_at.desc()).all()
    
    # Get all plans for filter dropdown
    plans = SubscriptionPlan.query.filter_by(is_active=True).all()
    
    return render_template(
        "admin/projects.html",
        admin=admin,
        users=users,
        plans=plans,
        status=status,
        search=search,
        selected_plan_id=plan_id
    )

@app.route("/admin/projects/<int:project_id>", methods=["GET"])
@admin_required
def admin_view_project(project_id):
    admin = current_admin()
    project = Project.query.get_or_404(project_id)
    
    # Get project owner
    owner = User.query.get(project.owner_user_id) if project.owner_user_id else None
    
    # Get project pairs
    pairs = ProjectPair.query.filter_by(project_id=project_id).order_by(ProjectPair.pair_index).all()
    
    # Get scan history for this project
    scan_history = ScanLog.query.filter_by(project_id=project_id).order_by(ScanLog.created_at.desc()).limit(50).all()
    
    return render_template("admin/view_project.html",
                         admin=admin,
                         project=project,
                         owner=owner,
                         pairs=pairs,
                         scan_history=scan_history)

@app.route("/admin/projects/<int:project_id>/toggle-status", methods=["POST"])
@admin_required
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

@app.route("/admin/projects/<int:project_id>/delete", methods=["POST"])
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
@admin_required
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
    
    # Get all activities (remove pagination)
    activities = query.order_by(AdminActivity.activity_at.desc()).all()
    
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
                         end_date=end_date)

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
        get_system_config=get_system_config
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
    
    
    
    # Quick file size check
    for image_file in images:
        if image_file.content_length and image_file.content_length > MAX_IMAGE_SIZE:
            flash("Image file exceeds allowed size limit.", "error")
            return redirect(url_for("admin_create_project_page"))
    
    for video_file in videos:
        if video_file.content_length and video_file.content_length > MAX_VIDEO_SIZE:
            flash("Video file exceeds allowed size limit.", "error")
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
    
    # Save ALL files quickly
    pairs_data = []
    for i, (image_file, video_file) in enumerate(zip(images, videos)):
        # Generate filenames
        img_filename = f"{project.id}_{i}.jpg"
        vid_ext = os.path.splitext(video_file.filename or "")[1].lower() or ".mp4"
        vid_filename = f"{project.id}_{i}{vid_ext}"
        
        # ✅ CHANGE 1: Save to ADMIN folders
        img_path = os.path.join(ADMIN_IMAGES_DIR, img_filename)  # ← CHANGED
        image_file.save(img_path)
        
        vid_path = os.path.join(ADMIN_VIDEOS_DIR, vid_filename)  # ← CHANGED
        video_file.save(vid_path)
        
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
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        abort(404)
    
    # ✅ ADD THIS CHECK
    file_path = os.path.join(ADMIN_IMAGES_DIR, pair.image_filename)
    if not os.path.exists(file_path):
        print(f"❌ Admin image not found: {file_path}")
        abort(404)
    
    return send_from_directory(ADMIN_IMAGES_DIR, pair.image_filename)
@app.route("/admin/video/<int:project_id>/<int:image_id>")
def serve_admin_video(project_id, image_id):
    """Serve videos for ADMIN projects only"""
    project = Project.query.get(project_id)
    if not project or not project.owner_admin_id:
        abort(404)
    
    pair = ProjectPair.query.filter_by(project_id=project_id, pair_index=image_id).first()
    if not pair:
        abort(404)
    
    return send_from_directory(ADMIN_VIDEOS_DIR, pair.video_filename)

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
    
    return send_from_directory(ADMIN_QR_DIR, filename)

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
        projects_url=url_for("admin_my_projects")
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
    """Ensure all errors return JSON for API endpoints"""
    # Check if the request is for an API/detection endpoint
    if request.path.startswith('/detect') or request.path.startswith('/api'):
        error_code = 500
        if hasattr(error, 'code'):
            error_code = error.code
        
        print(f"❌ API Error at {request.path}: {str(error)}")
        
        return jsonify({
            "detected": False,
            "reason": f"Server error: {str(error)[:100]}",
            "error": True,
            "path": request.path,
            "method": request.method
        }), error_code
    
    # For regular routes, return an HTTP response instead of the exception object
    error_code = getattr(error, 'code', 500) or 500
    app.logger.exception(error)
    return f"<h1>Error {error_code}</h1><p>{str(error)}</p>", error_code
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
        
        # Then populate with default data
        bootstrap_database()
    
    # Run the app
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True)
