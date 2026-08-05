# models.py — ScanStory complete DB models with subscription system

import json
from datetime import datetime as dt
import os

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, inspect
from sqlalchemy.orm import validates
from sqlalchemy.sql import func
from sqlalchemy.orm import scoped_session, sessionmaker

db = SQLAlchemy()


def get_utc_now():
    return dt.utcnow()


# ---------F------------------------------------------------------------
# Subscription Plans
# ---------------------------------------------------------------------
class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    plan_name = db.Column(db.String(255), nullable=False)
    plan_description = db.Column(db.Text, nullable=True)
    max_pairs_per_project = db.Column(db.Integer, default=10)
    # Plan pricing
    plan_amount = db.Column(db.Float, nullable=False, default=0.0)
    offer_price = db.Column(db.Float, nullable=True)
    currency = db.Column(db.String(10), default="INR")  # Changed to INR for Razorpay

    # Duration type: 'time' (months/years) or 'count' (projects)
    duration_type = db.Column(db.String(20), default='time')  # 'time' or 'count'
    duration_value = db.Column(db.Integer, default=1)  # 6 months, 1 year, or project count
    
    # Trial settings
    trial_days = db.Column(db.Integer, default=0)
    
    # Limits (admin configurable)
    total_project_limit = db.Column(db.Integer, default=1)
    total_scan_limit = db.Column(db.Integer, default=100)
    
    # Additional plan metadata
    features_json = db.Column(db.Text, default="[]")
    is_active = db.Column(db.Boolean, default=True)
    is_popular = db.Column(db.Boolean, default=False)
    display_order = db.Column(db.Integer, default=0)
    is_trial_plan = db.Column(db.Boolean, default=False)  # Marks free trial plan
    
    # Razorpay integration
    razorpay_plan_id = db.Column(db.String(255), nullable=True)
    
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)

    # Relationships
    users = db.relationship("User", backref="subscription_plan", lazy=True)
    payment_orders = db.relationship("PaymentOrder", backref="plan", lazy=True)

    @property
    def features_list(self):
        try:
            return json.loads(self.features_json or "[]")
        except Exception:
            return []
        
    @property
    def display_original_price(self):
        """Format original price for display"""
        if not self.offer_price or self.offer_price >= self.plan_amount:
            return None
        if self.plan_amount.is_integer():
            return f"₹{int(self.plan_amount)}.0"
        else:
            return f"₹{self.plan_amount:.1f}"
    @property
    def duration_display(self):
        """Format duration for display based on your image"""
        if self.is_trial_plan:
            return f"{self.duration_value} Months"  # "7 Months" for trial
        elif self.duration_type == 'time':
            if self.duration_value == 12:
                return "1 Year"  # "1 Year" for Pro plan
            else:
                return f"{self.duration_value} Months"  # "6 Months" for Basic
        else:
            return f"{self.duration_value} Projects"
    @property
    def button_text(self):
        """Get appropriate button text"""
        if self.is_trial_plan:
            return "Start Free Trail"  # For Free Trial
        else:
            return "Choose Plan"
    @property
    def display_price(self):
        """Format price for display (no decimal if whole number)"""
        if self.effective_price.is_integer():
            return f"₹{int(self.effective_price)}.0"
        else:
            return f"₹{self.effective_price:.1f}"

    @features_list.setter
    def features_list(self, value):
        self.features_json = json.dumps(value or [])

    @property
    def effective_price(self):
        return self.offer_price if self.offer_price else self.plan_amount

    def __repr__(self):
        return f"<SubscriptionPlan {self.plan_name} ₹{self.effective_price}>"


# ---------------------------------------------------------------------
# Users with Subscription Limits
# ---------------------------------------------------------------------
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    phone = db.Column(db.String(20), nullable=True)
    company = db.Column(db.String(255), nullable=True)
    profile_image = db.Column(db.String(500), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)

    # Account status
    is_verified = db.Column(db.Boolean, default=False)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    is_blocked = db.Column(db.Boolean, default=False)
    blocked_reason = db.Column(db.Text, nullable=True)
    blocked_at = db.Column(db.DateTime, nullable=True)
    blocked_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)

    # Current subscription
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscription_plans.id"), nullable=True)
    subscription_taken_at = db.Column(db.DateTime, nullable=True)
    subscription_expires_at = db.Column(db.DateTime, nullable=True)
    subscription_status = db.Column(db.String(20), default="trial")  # trial/active/expired/limit_reached
    
    # Subscription limits at time of purchase
    subscribed_project_limit = db.Column(db.Integer, default=1)
    subscribed_scan_limit = db.Column(db.Integer, default=100)
    
    # Current usage counters
    projects_used = db.Column(db.Integer, default=0)
    scans_used = db.Column(db.Integer, default=0)
    
    # Razorpay integration
    razorpay_customer_id = db.Column(db.String(255), nullable=True)
    razorpay_subscription_id = db.Column(db.String(255), nullable=True)

    # Activity tracking
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_login_ip = db.Column(db.String(45), nullable=True)
    login_count = db.Column(db.Integer, default=0)

    # Preferences
    timezone = db.Column(db.String(50), default="UTC")
    language = db.Column(db.String(10), default="en")
    email_notifications = db.Column(db.Boolean, default=True)

    # Timestamps
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    # Relationships
    trial_details = db.relationship("TrialDetails", backref="user", uselist=False, lazy=True, cascade="all, delete-orphan")
    otp_codes = db.relationship("OTPCode", backref="user", lazy=True, cascade="all, delete-orphan")
    projects = db.relationship("Project", backref="owner_user", lazy=True, cascade="all, delete-orphan")
    payment_orders = db.relationship("PaymentOrder", backref="user", lazy=True, cascade="all, delete-orphan")
    login_activities = db.relationship("UserLoginActivity", backref="user", lazy=True, cascade="all, delete-orphan")

    @property
    def full_name(self):
        fn = (self.first_name or "").strip()
        ln = (self.last_name or "").strip()
        name = f"{fn} {ln}".strip()
        return name or (self.email.split("@")[0] if self.email else "")

    def has_active_subscription(self):
        # Paid subscription
        if self.subscription_status == "active":
            if not self.subscription_expires_at:
                return True
            return self.subscription_expires_at > get_utc_now()

        # Trial subscription
        if self.subscription_status == "trial":
            td = self.trial_details
            return bool(td and td.is_active)

        return False

    def refresh_limit_status(self):
        if self.subscription_status != "limit_reached":
            return

        # If user has quota again, unlock
        if self.remaining_projects > 0 and self.remaining_scans > 0:
            if self.trial_details and self.trial_details.is_active:
                self.subscription_status = "trial"
            elif self.subscription_expires_at and self.subscription_expires_at > get_utc_now():
                self.subscription_status = "active"
            else:
                self.subscription_status = "expired"

    @property
    def can_create_project(self):
        """Check if user can create a new project"""
        if not self.has_active_subscription():
            return False
        return self.remaining_projects > 0

    @property
    def can_scan(self):
        """Check if user can perform scans"""
        if not self.has_active_subscription():
            return False
        return self.remaining_scans > 0

    @property
    def remaining_projects(self):
        """Calculate remaining projects"""
        if self.subscribed_project_limit in (None, 0):
            return 999999999
        return max(0, self.subscribed_project_limit - self.projects_used)

    @property
    def remaining_scans(self):
        """Calculate remaining scans"""
        if self.subscribed_scan_limit in (None, 0):
            return 999999999
        return max(0, self.subscribed_scan_limit - self.scans_used)

    @property
    def current_plan_name(self):
        """Get current plan name"""
        if self.subscription_plan:
            return self.subscription_plan.plan_name
        return "Free Trial"

    @property
    def plan_duration(self):
        """Get plan duration in human readable format"""
        if self.subscription_plan:
            if self.subscription_plan.duration_type == 'time':
                if self.subscription_plan.duration_value == 6:
                    return "6 months"
                elif self.subscription_plan.duration_value == 12:
                    return "1 year"
                else:
                    return f"{self.subscription_plan.duration_value} months"
            else:
                return f"{self.subscription_plan.duration_value} projects"
        return "Trial"

    def increment_scans_used(self):
        """Increment scans used counter"""
        self.scans_used = (self.scans_used or 0) + 1
        if self.remaining_scans <= 0:
            self.subscription_status = "limit_reached"
        db.session.commit()  # ✅ ALWAYS COMMIT - OUTSIDE IF STATEMENT
        return self.scans_used

    @validates("email")
    def validate_email(self, key, email):
        return email.strip().lower() if email else email

    def __repr__(self):
        return f"<User {self.email} ({self.id})>"


# ---------------------------------------------------------------------
# Trial details
# ---------------------------------------------------------------------
class TrialDetails(db.Model):
    __tablename__ = "trial_details"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)

    trial_start = db.Column(db.DateTime, nullable=False)
    trial_end = db.Column(db.DateTime, nullable=False)

    # Trial limits
    trial_project_limit = db.Column(db.Integer, default=1)
    trial_scan_limit = db.Column(db.Integer, default=50)
    
    # Extension
    trial_extended = db.Column(db.Boolean, default=False)
    extended_days = db.Column(db.Integer, default=0)
    extended_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)
    extended_at = db.Column(db.DateTime, nullable=True)
    extended_reason = db.Column(db.Text, nullable=True)

    # Conversion to paid
    trial_converted = db.Column(db.Boolean, default=False)
    converted_at = db.Column(db.DateTime, nullable=True)
    converted_plan_id = db.Column(db.Integer, db.ForeignKey("subscription_plans.id"), nullable=True)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    @property
    def is_active(self):
        return self.trial_end > get_utc_now()

    @property
    def remaining_trial_days(self):
        """Calculate remaining trial days"""
        if not self.is_active:
            return 0
        remaining = self.trial_end - get_utc_now()
        return max(0, remaining.days)

    def __repr__(self):
        return f"<TrialDetails user_id={self.user_id}>"


# ---------------------------------------------------------------------
# Payment Orders with Razorpay Integration
# ---------------------------------------------------------------------
class PaymentOrder(db.Model):
    __tablename__ = "payment_orders"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    order_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    # unique=True: enforced at the DB level (see migrations/versions/
    # *_razorpay_id_unique_constraints.py). NULL is not equal to NULL under
    # standard unique-index semantics in SQLite, MySQL, and Postgres alike,
    # so this still permits multiple NULL rows - no partial/conditional index
    # is needed to allow "unique only when set".
    razorpay_order_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    razorpay_payment_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    razorpay_signature = db.Column(db.String(512), nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("subscription_plans.id"), nullable=False)

    # Payment details
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default="INR")
    offer_amount = db.Column(db.Float, nullable=True)
    total_amount = db.Column(db.Float, nullable=False)
    
    # Payment method
    payment_method = db.Column(db.String(50), nullable=True)  # card/upi/netbanking/wallet
    bank_name = db.Column(db.String(100), nullable=True)
    
    # Status
    status = db.Column(db.String(20), default="pending")  # pending/success/failed/refunded
    
    # Plan limits at purchase time
    purchased_project_limit = db.Column(db.Integer, nullable=True)
    purchased_scan_limit = db.Column(db.Integer, nullable=True)
    
    # Subscription period
    subscription_start = db.Column(db.DateTime, nullable=True)
    subscription_end = db.Column(db.DateTime, nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    payment_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<PaymentOrder {self.order_id} - {self.status}>"


# ---------------------------------------------------------------------
# Paid-account capacity (V1 launch gate)
# ---------------------------------------------------------------------
class CapacityConfig(db.Model):
    """Durable global paid-account capacity config, single row (id=1).

    Invariant maintained by app.py's atomic reserve/release helpers:

        consumed_count == count(PaymentReservation rows with
                                 status in ('reserved', 'activated'))

    consumed_count only increases via the atomic conditional UPDATE that
    creates a new reservation (guarded by consumed_count < configured_limit),
    and only decreases when a *reserved* (not yet activated) reservation is
    released or expires. Once a reservation reaches 'activated' its slot is
    never freed by capacity logic - so lowering configured_limit later can
    never deactivate/evict an already-active customer.
    """
    __tablename__ = "capacity_config"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    configured_limit = db.Column(db.Integer, nullable=False, default=25)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    consumed_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class PaymentReservation(db.Model):
    """One row per attempted paid-account capacity slot.

    Lifecycle: reserved -> activated | released | expired
      - reserved:  slot held, checkout in progress (payment_order_id may
                   still be null for the brief window before the Razorpay
                   order row is created in the same request).
      - activated: payment verified, subscription is live; slot held for good.
      - released:  checkout abandoned/failed before activation (e.g. Razorpay
                   order creation itself failed); slot freed back.
      - expired:   reservation TTL (expires_at) passed before activation;
                   slot freed back.
    """
    __tablename__ = "payment_reservations"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    payment_order_id = db.Column(db.Integer, db.ForeignKey("payment_orders.id"), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default="reserved", index=True)
    reserved_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = db.relationship("User", backref="payment_reservations", lazy=True)
    payment_order = db.relationship("PaymentOrder", backref="reservation", uselist=False, lazy=True)

    def __repr__(self):
        return f"<PaymentReservation user={self.user_id} status={self.status}>"


class RazorpayWebhookEvent(db.Model):
    """One row per Razorpay webhook delivery, keyed by a deterministic
    idempotency key - the actual replay-safety gate for app.py's
    /webhooks/razorpay route (a DB unique-index rejection, not an in-app
    dict/set check - see the unique index in this table's migration).

    Razorpay's webhook envelope (entity/account_id/event/contains/payload/
    created_at) has no top-level unique event id, so `idempotency_key` is
    derived from stable fields instead of trusting a supplied id:
      - supported events (payment.captured): "{event_type}|{payment_id}|{order_id}"
        - stable across Razorpay's own retries of the same logical event,
          even if it re-sends with a different created_at/body byte layout.
      - any other validly-signed event type (no reconciliation is performed,
        see app.py SUPPORTED_WEBHOOK_EVENTS): "{event_type}|{payload_hash}"
        as a best-effort fallback, since those payload shapes aren't
        inspected/relied upon here.

    `payload_hash` is a sha256 hex digest of the raw request body - a
    non-sensitive fingerprint for observability/debugging, never the payload
    itself (the raw body is deliberately never persisted).
    """
    __tablename__ = "razorpay_webhook_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    idempotency_key = db.Column(db.String(255), unique=True, nullable=False, index=True)
    event_type = db.Column(db.String(100), nullable=False)
    razorpay_payment_id = db.Column(db.String(255), nullable=True, index=True)
    razorpay_order_id = db.Column(db.String(255), nullable=True, index=True)
    payload_hash = db.Column(db.String(64), nullable=False)

    # received -> processed | ignored | failed
    processing_status = db.Column(db.String(20), nullable=False, default="received")
    payment_order_id = db.Column(db.Integer, db.ForeignKey("payment_orders.id"), nullable=True, index=True)
    failure_code = db.Column(db.String(50), nullable=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=1)

    received_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    processed_at = db.Column(db.DateTime, nullable=True)

    payment_order = db.relationship("PaymentOrder", lazy=True)

    def __repr__(self):
        return f"<RazorpayWebhookEvent {self.event_type} status={self.processing_status}>"


# ---------------------------------------------------------------------
# OTP codes
# ---------------------------------------------------------------------
class OTPCode(db.Model):
    __tablename__ = "otp_codes"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    email = db.Column(db.String(255), nullable=False, index=True)
    code = db.Column(db.String(6), nullable=False)
    code_hash = db.Column(db.String(255), nullable=True)
    purpose = db.Column(db.String(50), nullable=False)
    challenge_id = db.Column(db.String(64), nullable=True, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)

    is_used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime, nullable=True)
    invalidated_at = db.Column(db.DateTime, nullable=True)
    locked_until = db.Column(db.DateTime, nullable=True)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=5, nullable=False)
    resend_count = db.Column(db.Integer, default=0, nullable=False)
    first_sent_at = db.Column(db.DateTime, nullable=True)
    last_sent_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        db.Index("ix_otp_email_purpose", "email", "purpose"),
        db.Index("ix_otp_expires_at", "expires_at"),
    )

    @property
    def is_expired(self):
        return get_utc_now() > self.expires_at

    def __repr__(self):
        return f"<OTPCode {self.email} {self.purpose}>"


# ---------------------------------------------------------------------
# Admins
# ---------------------------------------------------------------------
class Admin(db.Model):
    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)

    name = db.Column(db.String(255), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(50), default="admin")

    phone = db.Column(db.String(20), nullable=True)
    profile_image = db.Column(db.String(500), nullable=True)

    permissions_json = db.Column(
        db.Text,
        default='{"manage_users": true, "manage_projects": true, "manage_plans": true, "manage_admins": true}',
    )

    is_active = db.Column(db.Boolean, default=True)
    last_login_at = db.Column(db.DateTime, nullable=True)
    last_login_ip = db.Column(db.String(45), nullable=True)
    login_count = db.Column(db.Integer, default=0)

    timezone = db.Column(db.String(50), default="UTC")
    language = db.Column(db.String(10), default="en")

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)

    projects = db.relationship("Project", backref="owner_admin", lazy=True, cascade="all, delete-orphan")

    blocked_users = db.relationship("User", foreign_keys="User.blocked_by", backref="blocked_by_admin", lazy=True)
    extended_trials = db.relationship("TrialDetails", foreign_keys="TrialDetails.extended_by", backref="extended_by_admin", lazy=True)
    created_plans = db.relationship("SubscriptionPlan", foreign_keys="SubscriptionPlan.created_by", backref="created_by_admin", lazy=True)

    admin_activities = db.relationship("AdminActivity", backref="admin", lazy=True, cascade="all, delete-orphan")

    @property
    def permissions(self):
        try:
            return json.loads(self.permissions_json or "{}")
        except Exception:
            return {}

    @validates("email")
    def validate_email(self, key, email):
        return email.strip().lower() if email else email

    def __repr__(self):
        return f"<Admin {self.email} ({self.role})>"


# ---------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------
class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(255), nullable=False, default="Untitled Project")
    description = db.Column(db.Text, nullable=True)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    owner_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)

    user_project_index = db.Column(db.Integer, nullable=True)  # Per-user project numbering

    scanner_url = db.Column(db.Text, nullable=True)
    qr_code_path = db.Column(db.String(500), nullable=True)
    qr_code_filename = db.Column(db.String(255), nullable=True)

    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    pairs = db.relationship("ProjectPair", backref="project", lazy=True, cascade="all, delete-orphan", order_by="ProjectPair.pair_index")
    scan_logs = db.relationship("ScanLog", backref="project", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        owner = f"user:{self.owner_user_id}" if self.owner_user_id else f"admin:{self.owner_admin_id}"
        return f"<Project '{self.name}' ({owner})>"


class ProjectPair(db.Model):
    __tablename__ = "project_pairs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    pair_index = db.Column(db.Integer, nullable=False)

    image_filename = db.Column(db.String(255), nullable=False)
    video_filename = db.Column(db.String(255), nullable=False)
    image_path = db.Column(db.String(500), nullable=True)
    # image_hash = db.Column(db.String(64), nullable=True, index=True)
    original_image_name = db.Column(db.String(255), nullable=True)
    original_video_name = db.Column(db.String(255), nullable=True)
    image_size = db.Column(db.Integer, nullable=True)
    video_size = db.Column(db.Integer, nullable=True)

    # Recognition marker metadata. Legacy rows without explicit values behave as full-image markers.
    marker_mode = db.Column(db.String(20), nullable=True, default="full_image")
    marker_crop_x = db.Column(db.Float, nullable=True, default=0.0)
    marker_crop_y = db.Column(db.Float, nullable=True, default=0.0)
    marker_crop_width = db.Column(db.Float, nullable=True, default=1.0)
    marker_crop_height = db.Column(db.Float, nullable=True, default=1.0)
    marker_rotation = db.Column(db.Integer, nullable=True, default=0)
    marker_original_width = db.Column(db.Integer, nullable=True)
    marker_original_height = db.Column(db.Integer, nullable=True)
    marker_processed_width = db.Column(db.Integer, nullable=True)
    marker_processed_height = db.Column(db.Integer, nullable=True)
    marker_source_size_bytes = db.Column(db.Integer, nullable=True)
    marker_processed_size_bytes = db.Column(db.Integer, nullable=True)
    marker_display_orientation = db.Column(db.String(20), nullable=True)

    # ✅ CRITICAL ADDITIONS FOR FAST PROCESSING:
    is_processed = db.Column(db.Boolean, default=False)
    processing_status = db.Column(db.String(20), default='uploaded')  # 'uploaded', 'processing', 'completed', 'failed'
    video_processing_status = db.Column(db.String(20), default='pending')  # 'pending', 'compressing', 'compressed', 'failed'
    feature_extraction_status = db.Column(db.String(20), default='pending')  # 'pending', 'extracting', 'extracted', 'failed'
    
    processing_error = db.Column(db.Text, nullable=True)
    
    # Performance tracking
    feature_extraction_time = db.Column(db.Float, nullable=True)  # Time in seconds
    video_compression_time = db.Column(db.Float, nullable=True)   # Time in seconds
    total_processing_time = db.Column(db.Float, nullable=True)    # Total time in seconds

    match_count = db.Column(db.Integer, default=0)
    last_matched_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    scan_logs = db.relationship("ScanLog", backref="pair", lazy=True, cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("project_id", "pair_index", name="uq_project_pair_index"),
        db.Index("ix_project_pairs_processed", "project_id", "is_processed"),
        db.Index("ix_project_pairs_status", "project_id", "processing_status"),  # ✅ ADD THIS
        db.Index("ix_project_pairs_video_status", "video_processing_status"),  # ✅ ADD THIS
    )

    def __repr__(self):
        return f"<ProjectPair project_id={self.project_id} index={self.pair_index} status={self.processing_status}>"
    @staticmethod
    def get_threadsafe_session():
        """Get a thread-safe database session"""
        from app import db  # Import here to avoid circular imports
        Session = scoped_session(sessionmaker(bind=db.engine))
        return Session()
    # ✅ ENHANCED HELPER METHODS:
    @property
    def is_ready_for_detection(self):
        """Check if this pair is ready for scanning - EVEN IF VIDEO NOT COMPRESSED"""
        # Allow scanning if features are extracted OR if we have fallback
        return (self.feature_extraction_status == 'extracted' or 
                self.is_processed) and not self.processing_error
    
    @property
    def can_serve_video(self):
        """Check if video can be served (even if not compressed)"""
        return os.path.exists(self.video_file_path)
    
    @property
    def image_file_path(self):
        """Get full path to image file"""
        from app import IMAGES_DIR
        return os.path.join(IMAGES_DIR, self.image_filename)
    
    @property
    def video_file_path(self):
        """Get full path to video file"""
        from app import VIDEOS_DIR
        return os.path.join(VIDEOS_DIR, self.video_filename)
    
    @property
    def compressed_video_filename(self):
        """Get compressed video filename if exists"""
        if self.video_filename.endswith('.mp4'):
            return self.video_filename.replace('.mp4', '_fast.mp4')
        return self.video_filename + '_fast.mp4'
    
    @property
    def compressed_video_path(self):
        """Get path to compressed video if exists"""
        from app import VIDEOS_DIR
        compressed_name = self.compressed_video_filename
        compressed_path = os.path.join(VIDEOS_DIR, compressed_name)
        return compressed_path if os.path.exists(compressed_path) else self.video_file_path
    
    @property
    def npz_file_path(self):
        """Get path to feature file"""
        from app import FEATURES_DIR
        return os.path.join(FEATURES_DIR, f"{self.project_id}_{self.pair_index}.npz")
    
    @property
    def has_features(self):
        """Check if feature file exists"""
        return os.path.exists(self.npz_file_path)
    
    def mark_feature_extraction_complete(self, extraction_time=None):
        """Mark feature extraction as complete"""
        self.feature_extraction_status = 'extracted'
        self.is_processed = True  # Mark as processed for immediate use
        if extraction_time:
            self.feature_extraction_time = extraction_time
        db.session.commit()
    
    def mark_video_compression_complete(self, compression_time=None):
        """Mark video compression as complete"""
        self.video_processing_status = 'compressed'
        if compression_time:
            self.video_compression_time = compression_time
        
        # Update total processing time
        total_time = 0
        if self.feature_extraction_time:
            total_time += self.feature_extraction_time
        if compression_time:
            total_time += compression_time
        self.total_processing_time = total_time
        
        db.session.commit()
    
    def mark_as_failed(self, error_message, stage='processing'):
        """Mark processing as failed"""
        self.processing_status = 'failed'
        self.processing_error = error_message
        
        if stage == 'video':
            self.video_processing_status = 'failed'
        elif stage == 'features':
            self.feature_extraction_status = 'failed'
        
        db.session.commit()
    
    def increment_match_count(self):
        """Increment match counter"""
        self.match_count += 1
        self.last_matched_at = dt.utcnow()
        db.session.commit()
    
    def get_video_url(self):
        """Get video URL (prefers compressed, falls back to original)"""
        from app import url_for
        # Try compressed first
        compressed_path = self.compressed_video_path
        if compressed_path != self.video_file_path:
            compressed_name = os.path.basename(compressed_path)
            return url_for("serve_video", project_id=self.project_id, image_id=self.pair_index, filename=compressed_name)
        
        # Fall back to original
        return url_for("serve_video", project_id=self.project_id, image_id=self.pair_index)

# ---------------------------------------------------------------------
# Scan logs with subscription enforcement
# ---------------------------------------------------------------------
class ScanLog(db.Model):
    __tablename__ = "scan_logs"
    __table_args__ = (
        db.UniqueConstraint("user_id", "scan_session_id", name="uq_scan_logs_user_session"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    pair_id = db.Column(db.Integer, db.ForeignKey("project_pairs.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    scan_session_id = db.Column(db.String(100), nullable=False, index=True)
    is_successful = db.Column(db.Boolean, default=False)
    scan_type = db.Column(db.String(50), default="public")  # public/user
    counted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return f"<ScanLog user={self.user_id} project={self.project_id}>"


# ---------------------------------------------------------------------
# User Login Activity
# ---------------------------------------------------------------------
class UserLoginActivity(db.Model):
    __tablename__ = "user_login_activities"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    ip_address = db.Column(db.String(45), nullable=False)
    user_agent = db.Column(db.Text, nullable=True)

    is_successful = db.Column(db.Boolean, default=True)
    login_at = db.Column(db.DateTime, nullable=False, default=get_utc_now)


# ---------------------------------------------------------------------
# Admin Activity
# ---------------------------------------------------------------------
class AdminActivity(db.Model):
    __tablename__ = "admin_activities"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=False, index=True)

    activity_type = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    activity_at = db.Column(db.DateTime, nullable=False, default=get_utc_now)


# ---------------------------------------------------------------------
# System Configuration (Admin configurable settings)
# ---------------------------------------------------------------------
class SystemConfig(db.Model):
    __tablename__ = "system_configs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    config_key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    config_value = db.Column(db.Text, nullable=True)
    config_type = db.Column(db.String(50), default="string")  # string/integer/boolean/json
    description = db.Column(db.Text, nullable=True)
    
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True)

    def __repr__(self):
        return f"<SystemConfig {self.config_key}={self.config_value}>"


# ---------------------------------------------------------------------
# Gate C additive compatibility model foundation
# ---------------------------------------------------------------------

WORKSPACE_STATUSES = {"active", "suspended", "archived"}
WORKSPACE_TYPES = {"personal", "team", "agency", "education", "enterprise", "managed_service"}
WORKSPACE_MEMBER_ROLES = {"owner", "admin", "creator", "reviewer", "publisher", "analyst", "billing_admin"}
WORKSPACE_MEMBER_STATUSES = {"active", "invited", "suspended", "removed"}

EXPERIENCE_STATUSES = {
    "draft",
    "processing",
    "needs_attention",
    "ready_to_test",
    "ready_to_publish",
    "published",
    "paused",
    "archived",
}
EXPERIENCE_VERSION_STATUSES = {
    "draft",
    "processing",
    "needs_attention",
    "ready_to_publish",
    "publishing",
    "published",
    "superseded",
    "failed_publish",
    "archived",
}

TRIGGER_TYPES = {"image_marker"}
TRIGGER_STATUSES = {
    "draft",
    "uploading",
    "validating",
    "optimizing",
    "extracting",
    "robustness_testing",
    "ready",
    "failed",
    "retry_scheduled",
    "retrying",
    "excluded",
}

ASSET_TYPES = {"image", "video", "poster", "fallback", "recognition_artifact"}
TRIGGER_ASSET_ROLES = {"reference_image", "video", "poster", "fallback"}
RECOGNITION_ARTIFACT_TYPES = {"feature_npz"}
PROCESSING_JOB_STATUSES = {
    "pending",
    "queued",
    "ready",
    "claimed",
    "processing",
    "running",
    "completed",
    "succeeded",
    "failed",
    "failed_retryable",
    "retrying",
    "retry_scheduled",
    "failed_terminal",
    "cancelled",
    "superseded",
}
MIGRATION_CHECKPOINT_STATUSES = {"pending", "dry_run", "completed", "failed", "skipped"}


def _validate_value(value, allowed, field_name):
    if value not in allowed:
        raise ValueError(f"{field_name} must be one of: {', '.join(sorted(allowed))}")
    return value


def _prevent_public_key_update(mapper, connection, target):
    state = inspect(target)
    history = state.attrs.public_key.history
    if history.has_changes() and history.deleted:
        raise ValueError("public_key is immutable")


class Organization(db.Model):
    __tablename__ = "organizations"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspaces = db.relationship("Workspace", back_populates="organization", lazy=True)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, WORKSPACE_STATUSES, key)


class Workspace(db.Model):
    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    organization_id = db.Column(db.Integer, db.ForeignKey("organizations.id"), nullable=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    workspace_type = db.Column(db.String(40), default="personal", nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    organization = db.relationship("Organization", back_populates="workspaces", lazy=True)
    members = db.relationship("WorkspaceMember", back_populates="workspace", lazy=True, cascade="all, delete-orphan")
    experiences = db.relationship("Experience", back_populates="workspace", lazy=True)
    assets = db.relationship("Asset", back_populates="workspace", lazy=True)
    processing_jobs = db.relationship("ProcessingJob", back_populates="workspace", lazy=True)

    @validates("workspace_type")
    def validate_workspace_type(self, key, value):
        return _validate_value(value, WORKSPACE_TYPES, key)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, WORKSPACE_STATUSES, key)


class WorkspaceMember(db.Model):
    __tablename__ = "workspace_members"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    role = db.Column(db.String(40), default="owner", nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)
    joined_at = db.Column(db.DateTime, nullable=False, default=get_utc_now)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = db.relationship("Workspace", back_populates="members", lazy=True)
    user = db.relationship("User", backref=db.backref("workspace_memberships", lazy=True), lazy=True)

    __table_args__ = (
        db.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member_user"),
    )

    @validates("role")
    def validate_role(self, key, value):
        return _validate_value(value, WORKSPACE_MEMBER_ROLES, key)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, WORKSPACE_MEMBER_STATUSES, key)


class Experience(db.Model):
    __tablename__ = "experiences"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True)
    legacy_project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), unique=True, nullable=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), default="draft", nullable=False)
    current_published_version_id = db.Column(
        db.Integer,
        db.ForeignKey("experience_versions.id", use_alter=True, name="fk_experiences_current_published_version"),
        nullable=True,
    )
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    workspace = db.relationship("Workspace", back_populates="experiences", lazy=True)
    legacy_project = db.relationship("Project", backref=db.backref("mapped_experience", uselist=False), lazy=True)
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], lazy=True)
    versions = db.relationship(
        "ExperienceVersion",
        back_populates="experience",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="ExperienceVersion.experience_id",
    )
    current_published_version = db.relationship(
        "ExperienceVersion",
        foreign_keys=[current_published_version_id],
        post_update=True,
        lazy=True,
    )
    triggers = db.relationship("Trigger", back_populates="experience", lazy=True)
    processing_jobs = db.relationship("ProcessingJob", back_populates="experience", lazy=True)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, EXPERIENCE_STATUSES, key)


class ExperienceVersion(db.Model):
    __tablename__ = "experience_versions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    experience_id = db.Column(db.Integer, db.ForeignKey("experiences.id"), nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), default="draft", nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    published_at = db.Column(db.DateTime, nullable=True)
    superseded_at = db.Column(db.DateTime, nullable=True)
    source_version_id = db.Column(db.Integer, db.ForeignKey("experience_versions.id"), nullable=True)
    published_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    publication_checksum = db.Column(db.String(128), nullable=True)
    publication_idempotency_key = db.Column(db.String(128), nullable=True, index=True)
    processing_snapshot_json = db.Column(db.Text, nullable=True)
    publication_notes = db.Column(db.Text, nullable=True)
    rollback_source_version_id = db.Column(db.Integer, db.ForeignKey("experience_versions.id"), nullable=True)
    is_immutable = db.Column(db.Boolean, default=False, nullable=False)
    public_destination = db.Column(db.String(500), nullable=True)

    experience = db.relationship("Experience", back_populates="versions", foreign_keys=[experience_id], lazy=True)
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], lazy=True)
    published_by_user = db.relationship("User", foreign_keys=[published_by_user_id], lazy=True)
    source_version = db.relationship("ExperienceVersion", remote_side=[id], foreign_keys=[source_version_id], lazy=True)
    rollback_source_version = db.relationship("ExperienceVersion", remote_side=[id], foreign_keys=[rollback_source_version_id], lazy=True)
    triggers = db.relationship("Trigger", back_populates="experience_version", lazy=True)
    trigger_snapshots = db.relationship("ExperienceVersionTrigger", back_populates="version", lazy=True, cascade="all, delete-orphan")

    __table_args__ = (
        db.UniqueConstraint("experience_id", "version_number", name="uq_experience_version_number"),
    )

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, EXPERIENCE_VERSION_STATUSES, key)


class ExperienceVersionTrigger(db.Model):
    __tablename__ = "experience_version_triggers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    experience_version_id = db.Column(db.Integer, db.ForeignKey("experience_versions.id"), nullable=False, index=True)
    trigger_id = db.Column(db.Integer, db.ForeignKey("triggers.id"), nullable=False, index=True)
    inclusion_order = db.Column(db.Integer, default=0, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_excluded = db.Column(db.Boolean, default=False, nullable=False)
    reference_image_asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=True)
    video_asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=True)
    recognition_artifact_id = db.Column(db.Integer, db.ForeignKey("recognition_artifacts.id"), nullable=True)
    fallback_asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=True)
    creator_label = db.Column(db.String(255), nullable=True)
    processing_snapshot_json = db.Column(db.Text, nullable=True)
    source_revision_hash = db.Column(db.String(128), nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    version = db.relationship("ExperienceVersion", back_populates="trigger_snapshots", lazy=True)
    trigger = db.relationship("Trigger", lazy=True)
    reference_image_asset = db.relationship("Asset", foreign_keys=[reference_image_asset_id], lazy=True)
    video_asset = db.relationship("Asset", foreign_keys=[video_asset_id], lazy=True)
    recognition_artifact = db.relationship("RecognitionArtifact", lazy=True)
    fallback_asset = db.relationship("Asset", foreign_keys=[fallback_asset_id], lazy=True)

    __table_args__ = (
        db.UniqueConstraint("experience_version_id", "trigger_id", name="uq_experience_version_trigger"),
    )


class Trigger(db.Model):
    __tablename__ = "triggers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    experience_id = db.Column(db.Integer, db.ForeignKey("experiences.id"), nullable=False, index=True)
    experience_version_id = db.Column(db.Integer, db.ForeignKey("experience_versions.id"), nullable=True, index=True)
    legacy_project_pair_id = db.Column(db.Integer, db.ForeignKey("project_pairs.id"), unique=True, nullable=True, index=True)
    name = db.Column(db.String(255), nullable=False)
    trigger_type = db.Column(db.String(40), default="image_marker", nullable=False)
    status = db.Column(db.String(40), default="draft", nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_excluded = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    experience = db.relationship("Experience", back_populates="triggers", lazy=True)
    experience_version = db.relationship("ExperienceVersion", back_populates="triggers", lazy=True)
    legacy_project_pair = db.relationship("ProjectPair", backref=db.backref("mapped_trigger", uselist=False), lazy=True)
    trigger_assets = db.relationship("TriggerAsset", back_populates="trigger", lazy=True, cascade="all, delete-orphan")
    recognition_artifacts = db.relationship("RecognitionArtifact", back_populates="trigger", lazy=True, cascade="all, delete-orphan")
    processing_jobs = db.relationship("ProcessingJob", back_populates="trigger", lazy=True)

    @validates("trigger_type")
    def validate_trigger_type(self, key, value):
        return _validate_value(value, TRIGGER_TYPES, key)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, TRIGGER_STATUSES, key)


class Asset(db.Model):
    __tablename__ = "assets"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=False, index=True)
    asset_type = db.Column(db.String(40), nullable=False)
    storage_provider = db.Column(db.String(40), default="local_legacy", nullable=False)
    storage_key = db.Column(db.String(500), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True)
    mime_type = db.Column(db.String(255), nullable=True)
    size_bytes = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(30), default="available", nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = db.relationship("Workspace", back_populates="assets", lazy=True)
    trigger_assets = db.relationship("TriggerAsset", back_populates="asset", lazy=True, cascade="all, delete-orphan")

    @validates("asset_type")
    def validate_asset_type(self, key, value):
        return _validate_value(value, ASSET_TYPES, key)


class TriggerAsset(db.Model):
    __tablename__ = "trigger_assets"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    trigger_id = db.Column(db.Integer, db.ForeignKey("triggers.id"), nullable=False, index=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("assets.id"), nullable=False, index=True)
    role = db.Column(db.String(40), nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    trigger = db.relationship("Trigger", back_populates="trigger_assets", lazy=True)
    asset = db.relationship("Asset", back_populates="trigger_assets", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("trigger_id", "asset_id", "role", name="uq_trigger_asset_role"),
    )

    @validates("role")
    def validate_role(self, key, value):
        return _validate_value(value, TRIGGER_ASSET_ROLES, key)


class RecognitionArtifact(db.Model):
    __tablename__ = "recognition_artifacts"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    trigger_id = db.Column(db.Integer, db.ForeignKey("triggers.id"), nullable=False, index=True)
    artifact_type = db.Column(db.String(40), nullable=False)
    algorithm = db.Column(db.String(80), default="orb", nullable=False)
    algorithm_version = db.Column(db.String(80), nullable=True)
    storage_provider = db.Column(db.String(40), default="local_legacy", nullable=False)
    storage_key = db.Column(db.String(500), nullable=False)
    status = db.Column(db.String(30), default="available", nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    trigger = db.relationship("Trigger", back_populates="recognition_artifacts", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("trigger_id", "artifact_type", "storage_key", name="uq_trigger_artifact_storage"),
    )

    @validates("artifact_type")
    def validate_artifact_type(self, key, value):
        return _validate_value(value, RECOGNITION_ARTIFACT_TYPES, key)


class ProcessingJob(db.Model):
    __tablename__ = "processing_jobs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=True, index=True)
    experience_id = db.Column(db.Integer, db.ForeignKey("experiences.id"), nullable=True, index=True)
    trigger_id = db.Column(db.Integer, db.ForeignKey("triggers.id"), nullable=True, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)
    pair_id = db.Column(db.Integer, db.ForeignKey("project_pairs.id"), nullable=True, index=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    owner_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    job_type = db.Column(db.String(80), nullable=False)
    status = db.Column(db.String(30), default="pending", nullable=False)
    queue_job_id = db.Column(db.String(191), nullable=True, index=True)
    progress = db.Column(db.Integer, default=0, nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    max_attempts = db.Column(db.Integer, default=3, nullable=False)
    priority = db.Column(db.Integer, default=100, nullable=False)
    idempotency_key = db.Column(db.String(128), nullable=False, index=True)
    queued_at = db.Column(db.DateTime, nullable=True)
    available_at = db.Column(db.DateTime, nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    claimed_by = db.Column(db.String(128), nullable=True)
    lease_expires_at = db.Column(db.DateTime, nullable=True)
    error_code = db.Column(db.String(80), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    safe_error_code = db.Column(db.String(80), nullable=True)
    safe_error_summary = db.Column(db.String(500), nullable=True)
    internal_diagnostics = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    failed_at = db.Column(db.DateTime, nullable=True)
    last_heartbeat_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = db.relationship("Workspace", back_populates="processing_jobs", lazy=True)
    experience = db.relationship("Experience", back_populates="processing_jobs", lazy=True)
    trigger = db.relationship("Trigger", back_populates="processing_jobs", lazy=True)
    project = db.relationship("Project", lazy=True)
    pair = db.relationship("ProjectPair", lazy=True)
    owner_user = db.relationship("User", lazy=True)
    owner_admin = db.relationship("Admin", lazy=True)

    __table_args__ = (
        db.UniqueConstraint("workspace_id", "idempotency_key", name="uq_processing_job_workspace_idempotency"),
        db.UniqueConstraint("project_id", "idempotency_key", name="uq_processing_job_project_idempotency"),
        db.Index("ix_processing_jobs_project_status", "project_id", "status"),
        db.Index("ix_processing_jobs_pair_status", "pair_id", "status"),
        db.Index("ix_processing_jobs_type_status", "job_type", "status"),
        db.Index("ix_processing_jobs_owner_user_status", "owner_user_id", "status"),
        db.Index("ix_processing_jobs_owner_admin_status", "owner_admin_id", "status"),
    )

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, PROCESSING_JOB_STATUSES, key)

    @validates("progress")
    def validate_progress(self, key, value):
        if value < 0 or value > 100:
            raise ValueError("progress must be between 0 and 100")
        return value


class ProcessingEvent(db.Model):
    __tablename__ = "processing_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    workspace_id = db.Column(db.Integer, db.ForeignKey("workspaces.id"), nullable=True, index=True)
    experience_id = db.Column(db.Integer, db.ForeignKey("experiences.id"), nullable=True, index=True)
    trigger_id = db.Column(db.Integer, db.ForeignKey("triggers.id"), nullable=True, index=True)
    job_id = db.Column(db.Integer, db.ForeignKey("processing_jobs.id"), nullable=True, index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    actor = db.Column(db.String(120), nullable=True)
    creator_message = db.Column(db.Text, nullable=True)
    diagnostic_code = db.Column(db.String(80), nullable=True)
    diagnostic_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    workspace = db.relationship("Workspace", lazy=True)
    experience = db.relationship("Experience", lazy=True)
    trigger = db.relationship("Trigger", lazy=True)
    job = db.relationship("ProcessingJob", lazy=True)


class MigrationCheckpoint(db.Model):
    __tablename__ = "migration_checkpoints"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    migration_name = db.Column(db.String(120), nullable=False, index=True)
    entity_type = db.Column(db.String(80), nullable=False, index=True)
    legacy_id = db.Column(db.Integer, nullable=True, index=True)
    target_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(30), default="pending", nullable=False)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint("migration_name", "entity_type", "legacy_id", name="uq_migration_checkpoint_legacy"),
    )

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, MIGRATION_CHECKPOINT_STATUSES, key)


for _public_model in (Organization, Workspace, Experience, Trigger, Asset, ProcessingJob):
    event.listen(_public_model, "before_update", _prevent_public_key_update)


def _prevent_published_snapshot_update(mapper, connection, target):
    version = getattr(target, "version", None)
    if version and version.is_immutable:
        raise ValueError("published version snapshot is immutable")


def _prevent_published_snapshot_delete(mapper, connection, target):
    version = getattr(target, "version", None)
    if version and version.is_immutable:
        raise ValueError("published version snapshot cannot be deleted")


event.listen(ExperienceVersionTrigger, "before_update", _prevent_published_snapshot_update)
event.listen(ExperienceVersionTrigger, "before_delete", _prevent_published_snapshot_delete)
