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


ACCOUNT_TYPE_INDIVIDUAL = "INDIVIDUAL"
ACCOUNT_TYPE_BUSINESS_VENDOR = "BUSINESS_VENDOR"
USER_ACCOUNT_TYPES = {ACCOUNT_TYPE_INDIVIDUAL, ACCOUNT_TYPE_BUSINESS_VENDOR}


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
    account_type = db.Column(
        db.String(30),
        nullable=False,
        default=ACCOUNT_TYPE_INDIVIDUAL,
        server_default=ACCOUNT_TYPE_INDIVIDUAL,
    )

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
    projects = db.relationship(
        "Project",
        backref="owner_user",
        lazy=True,
        cascade="all, delete-orphan",
        foreign_keys="Project.owner_user_id",
    )
    payment_orders = db.relationship("PaymentOrder", backref="user", lazy=True, cascade="all, delete-orphan")
    login_activities = db.relationship("UserLoginActivity", backref="user", lazy=True, cascade="all, delete-orphan")
    consent_evidence = db.relationship("UserConsentEvidence", backref="user", lazy=True, cascade="all, delete-orphan")

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

    @validates("account_type")
    def validate_account_type(self, key, value):
        return _validate_value((value or ACCOUNT_TYPE_INDIVIDUAL).strip().upper(), USER_ACCOUNT_TYPES, key)

    def __repr__(self):
        return f"<User {self.email} ({self.id})>"


# ---------------------------------------------------------------------
# User consent evidence
# ---------------------------------------------------------------------
class UserConsentEvidence(db.Model):
    __tablename__ = "user_consent_evidence"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "consent_type",
            "policy_version",
            "source_context",
            name="uq_user_consent_type_version_source",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    consent_type = db.Column(db.String(30), nullable=False, index=True)
    policy_version = db.Column(db.String(80), nullable=False)
    accepted_at = db.Column(db.DateTime, nullable=False, default=get_utc_now, index=True)
    source_context = db.Column(db.String(80), nullable=False, default="registration")
    evidence_metadata = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    @property
    def metadata_dict(self):
        try:
            return json.loads(self.evidence_metadata or "{}")
        except Exception:
            return {}

    @validates("consent_type")
    def validate_consent_type(self, key, consent_type):
        consent_type = (consent_type or "").strip().upper()
        if consent_type not in {"TERMS", "PRIVACY"}:
            raise ValueError("Invalid consent type.")
        return consent_type


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
    addon_purchase_id = db.Column(db.Integer, db.ForeignKey("addon_purchases.id"), nullable=True, index=True)
    payment_refund_id = db.Column(db.Integer, db.ForeignKey("payment_refunds.id"), nullable=True, index=True)
    failure_code = db.Column(db.String(50), nullable=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=1)

    received_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    processed_at = db.Column(db.DateTime, nullable=True)

    payment_order = db.relationship("PaymentOrder", lazy=True)
    addon_purchase = db.relationship("AddonPurchase", lazy=True)
    payment_refund = db.relationship("PaymentRefund", lazy=True)

    def __repr__(self):
        return f"<RazorpayWebhookEvent {self.event_type} status={self.processing_status}>"


# ---------------------------------------------------------------------
# Admin-initiated payment refunds
# ---------------------------------------------------------------------
REFUND_STATUSES = {"REFUND_REQUESTED", "REFUND_PROCESSING", "REFUNDED", "REFUND_FAILED"}
REFUND_RECONCILIATION_STATUSES = {
    "PENDING",
    "APPLIED",
    "MANUAL_REVIEW_REQUIRED",
    "FAILED",
}
REFUND_PROVIDERS = {"RAZORPAY"}


class PaymentRefund(db.Model):
    __tablename__ = "payment_refunds"
    __table_args__ = (
        db.CheckConstraint(
            "(payment_order_id IS NOT NULL AND addon_purchase_id IS NULL) OR "
            "(payment_order_id IS NULL AND addon_purchase_id IS NOT NULL)",
            name="ck_payment_refunds_exactly_one_source",
        ),
        db.UniqueConstraint("payment_order_id", name="uq_payment_refunds_payment_order_id"),
        db.UniqueConstraint("addon_purchase_id", name="uq_payment_refunds_addon_purchase_id"),
        db.UniqueConstraint("provider_refund_id", name="uq_payment_refunds_provider_refund_id"),
        db.UniqueConstraint("idempotency_key", name="uq_payment_refunds_idempotency_key"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    payment_order_id = db.Column(db.Integer, db.ForeignKey("payment_orders.id"), nullable=True, index=True)
    addon_purchase_id = db.Column(db.Integer, db.ForeignKey("addon_purchases.id"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)
    provider = db.Column(db.String(30), nullable=False, default="RAZORPAY", server_default="RAZORPAY")
    provider_refund_id = db.Column(db.String(255), nullable=True, index=True)
    provider_payment_id = db.Column(db.String(255), nullable=False, index=True)
    provider_status = db.Column(db.String(40), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="INR")
    status = db.Column(db.String(30), nullable=False, default="REFUND_REQUESTED", server_default="REFUND_REQUESTED", index=True)
    reconciliation_status = db.Column(db.String(40), nullable=False, default="PENDING", server_default="PENDING", index=True)
    reconciliation_message_safe = db.Column(db.String(255), nullable=True)
    reason = db.Column(db.Text, nullable=False)
    requested_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=False, index=True)
    requested_at = db.Column(db.DateTime, nullable=False, default=get_utc_now)
    processing_started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    failed_at = db.Column(db.DateTime, nullable=True)
    failure_code = db.Column(db.String(80), nullable=True)
    failure_message_safe = db.Column(db.String(255), nullable=True)
    idempotency_key = db.Column(db.String(120), nullable=False)
    metadata_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    payment_order = db.relationship("PaymentOrder", lazy=True)
    addon_purchase = db.relationship("AddonPurchase", lazy=True)
    user = db.relationship("User", lazy=True)
    project = db.relationship("Project", lazy=True)
    requested_by_admin = db.relationship("Admin", lazy=True)

    @validates("provider")
    def validate_provider(self, key, value):
        return _validate_value((value or "RAZORPAY").strip().upper(), REFUND_PROVIDERS, key)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value((value or "REFUND_REQUESTED").strip().upper(), REFUND_STATUSES, key)

    @validates("reconciliation_status")
    def validate_reconciliation_status(self, key, value):
        return _validate_value(
            (value or "PENDING").strip().upper(),
            REFUND_RECONCILIATION_STATUSES,
            key,
        )


# ---------------------------------------------------------------------
# Self-service add-ons and entitlement ledger
# ---------------------------------------------------------------------
ADDON_TYPES = {"EXTRA_SCANS", "VALIDITY_EXTENSION", "PROJECT_CAPACITY", "PROJECT_SERVICE_COVERAGE"}
ADDON_PURCHASE_STATUSES = {"pending", "fulfilled", "failed", "cancelled", "refunded"}
ENTITLEMENT_TYPES = {"EXTRA_SCANS", "VALIDITY_EXTENSION", "PROJECT_CAPACITY", "PROJECT_SERVICE_COVERAGE"}


class AddonCatalog(db.Model):
    __tablename__ = "addon_catalog"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    code = db.Column(db.String(80), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=True)
    addon_type = db.Column(db.String(40), nullable=False, index=True)
    unit_amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default="INR", nullable=False)
    scan_delta = db.Column(db.Integer, nullable=True)
    validity_days_delta = db.Column(db.Integer, nullable=True)
    project_delta = db.Column(db.Integer, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_commercially_available = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    purchases = db.relationship("AddonPurchase", backref="catalog_item", lazy=True)

    @validates("addon_type")
    def validate_addon_type(self, key, value):
        return _validate_value(value, ADDON_TYPES, key)


class AddonPurchase(db.Model):
    __tablename__ = "addon_purchases"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    order_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    catalog_id = db.Column(db.Integer, db.ForeignKey("addon_catalog.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    amount = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default="INR", nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    razorpay_order_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    razorpay_payment_id = db.Column(db.String(255), unique=True, nullable=True, index=True)
    razorpay_signature = db.Column(db.String(512), nullable=True)
    failure_code = db.Column(db.String(80), nullable=True)
    paid_at = db.Column(db.DateTime, nullable=True)
    fulfilled_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    user = db.relationship("User", backref="addon_purchases", lazy=True)
    project = db.relationship("Project", lazy=True)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, ADDON_PURCHASE_STATUSES, key)


class EntitlementTransaction(db.Model):
    __tablename__ = "entitlement_transactions"
    __table_args__ = (
        db.UniqueConstraint("source_type", "source_id", "entitlement_type", name="uq_entitlement_source_type_id_type"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)
    entitlement_type = db.Column(db.String(40), nullable=False, index=True)
    delta_value = db.Column(db.Integer, nullable=False)
    source_type = db.Column(db.String(40), nullable=False, index=True)
    source_id = db.Column(db.Integer, nullable=False, index=True)
    reason = db.Column(db.String(200), nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)
    valid_from = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    user = db.relationship("User", backref="entitlement_transactions", lazy=True)
    project = db.relationship("Project", lazy=True)

    @property
    def metadata_dict(self):
        try:
            return json.loads(self.metadata_json or "{}")
        except Exception:
            return {}

    @validates("entitlement_type")
    def validate_entitlement_type(self, key, value):
        return _validate_value(value, ENTITLEMENT_TYPES, key)


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
PROJECT_EXPERIENCE_TYPES = {"image_video", "direct_qr"}
PROJECT_PLAYBACK_MODES = {"tracked_overlay", "detect_once", "direct"}
PROJECT_TRANSFER_STATUSES = {
    "PENDING_ACCEPTANCE",
    "PENDING_CAPACITY",
    "COMPLETED",
    "CANCELLED",
    "EXPIRED",
    "DISPUTED",
}
PROJECT_ACTIVE_TRANSFER_STATUSES = {"PENDING_ACCEPTANCE", "PENDING_CAPACITY", "DISPUTED"}
PROJECT_CLAIM_STATUSES = {
    "OPEN",
    "VENDOR_NOTIFIED",
    "APPROVED_BY_VENDOR",
    "PENDING_ADMIN_REVIEW",
    "APPROVED_BY_ADMIN",
    "REJECTED",
    "CANCELLED",
    "EXPIRED",
    "TRANSFER_COMPLETED",
}
PROJECT_ACTIVE_CLAIM_STATUSES = {"OPEN", "VENDOR_NOTIFIED", "PENDING_ADMIN_REVIEW", "APPROVED_BY_VENDOR"}
PROJECT_SERVICE_COVERAGE_SOURCE_TYPES = {
    "OWNER_SUBSCRIPTION",
    "STANDALONE_PROJECT_RENEWAL",
    "TRANSFER_CARRY_OVER",
    "ADMIN_GRANT",
    "LEGACY_COMPATIBILITY",
}
PROJECT_SERVICE_COVERAGE_STATUSES = {"ACTIVE", "REVOKED", "EXPIRED", "SUPERSEDED"}


class Project(db.Model):
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(255), nullable=False, default="Untitled Project")
    description = db.Column(db.Text, nullable=True)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    owner_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    current_owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    manager_vendor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    beneficiary_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    user_project_index = db.Column(db.Integer, nullable=True)  # Per-user project numbering
    experience_type = db.Column(db.String(30), nullable=False, default="image_video", server_default="image_video")
    playback_mode = db.Column(db.String(30), nullable=False, default="tracked_overlay", server_default="tracked_overlay")

    scanner_url = db.Column(db.Text, nullable=True)
    qr_code_path = db.Column(db.String(500), nullable=True)
    qr_code_filename = db.Column(db.String(255), nullable=True)

    is_active = db.Column(db.Boolean, default=True)

    # V1 Wave 6: project-level default fallback video. References one of this
    # project's OWN ProjectPair rows (reuses its existing video_filename -
    # no separate fallback-video upload flow exists). Deliberately no
    # db.relationship() here: Project already has a `pairs` relationship
    # keyed off ProjectPair.project_id, and every read site re-verifies
    # `ProjectPair.query.filter_by(id=fallback_pair_id, project_id=self.id)`
    # rather than trusting this FK alone - belt-and-suspenders against a
    # cross-project reference ever being served (see
    # resolve_project_fallback_video() in app.py).
    # use_alter=True + an explicit constraint name: projects <-> project_pairs
    # would otherwise be a mutually-dependent (cyclic) FK pair (ProjectPair
    # already has a NOT NULL FK back to projects.id) - without this,
    # SQLAlchemy's table-sort for a from-scratch db.create_all() (bootstrap
    # self-heal, test fixtures) cannot determine a safe creation order and
    # this constraint would be silently dropped from consideration (SQLite
    # tolerates that; a strict FK-enforcing backend would not). use_alter
    # defers this one FK to a separate ALTER TABLE step, breaking the cycle.
    fallback_pair_id = db.Column(
        db.Integer,
        db.ForeignKey("project_pairs.id", use_alter=True, name="fk_projects_fallback_pair_id"),
        nullable=True,
    )

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)

    pairs = db.relationship("ProjectPair", backref="project", lazy=True, cascade="all, delete-orphan", order_by="ProjectPair.pair_index", foreign_keys="ProjectPair.project_id")
    scan_logs = db.relationship("ScanLog", backref="project", lazy=True, cascade="all, delete-orphan")
    scan_events = db.relationship("ScanEvent", backref="project", lazy=True, cascade="all, delete-orphan")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], lazy=True)
    current_owner_user = db.relationship("User", foreign_keys=[current_owner_user_id], lazy=True)
    manager_vendor_user = db.relationship("User", foreign_keys=[manager_vendor_user_id], lazy=True)
    beneficiary_user = db.relationship("User", foreign_keys=[beneficiary_user_id], lazy=True)

    @validates("experience_type")
    def validate_experience_type(self, key, value):
        return _validate_value(value or "image_video", PROJECT_EXPERIENCE_TYPES, key)

    @validates("playback_mode")
    def validate_playback_mode(self, key, value):
        return _validate_value(value or "tracked_overlay", PROJECT_PLAYBACK_MODES, key)

    def __repr__(self):
        owner = f"user:{self.owner_user_id}" if self.owner_user_id else f"admin:{self.owner_admin_id}"
        return f"<Project '{self.name}' ({owner})>"


class ProjectOwnershipTransfer(db.Model):
    __tablename__ = "project_ownership_transfers"
    __table_args__ = (
        db.Index("ix_project_ownership_transfers_project_status", "project_id", "status"),
        db.Index("ix_project_ownership_transfers_to_status", "to_user_id", "status"),
        db.Index("ix_project_ownership_transfers_from_status", "from_owner_user_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    initiated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    from_owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    to_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    retain_vendor_management = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    status = db.Column(db.String(30), nullable=False, default="PENDING_ACCEPTANCE", server_default="PENDING_ACCEPTANCE")
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    accepted_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    cancelled_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    completed_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    reason = db.Column(db.Text, nullable=True)
    note = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", backref=db.backref("ownership_transfers", lazy=True))
    initiated_by_user = db.relationship("User", foreign_keys=[initiated_by_user_id], lazy=True)
    from_owner_user = db.relationship("User", foreign_keys=[from_owner_user_id], lazy=True)
    to_user = db.relationship("User", foreign_keys=[to_user_id], lazy=True)
    completed_by_admin = db.relationship("Admin", foreign_keys=[completed_by_admin_id], lazy=True)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value((value or "PENDING_ACCEPTANCE").strip().upper(), PROJECT_TRANSFER_STATUSES, key)


class ProjectOwnershipClaim(db.Model):
    __tablename__ = "project_ownership_claims"
    __table_args__ = (
        db.Index("ix_project_ownership_claims_project_status", "project_id", "status"),
        db.Index("ix_project_ownership_claims_claimant_status", "claimant_user_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    claimant_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    current_owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default="OPEN", server_default="OPEN")
    evidence_summary = db.Column(db.Text, nullable=True)
    evidence_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    vendor_notified_at = db.Column(db.DateTime, nullable=True)
    response_deadline_at = db.Column(db.DateTime, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    decision_reason = db.Column(db.Text, nullable=True)
    transfer_id = db.Column(db.Integer, db.ForeignKey("project_ownership_transfers.id"), nullable=True, index=True)

    project = db.relationship("Project", backref=db.backref("ownership_claims", lazy=True))
    claimant_user = db.relationship("User", foreign_keys=[claimant_user_id], lazy=True)
    current_owner_user = db.relationship("User", foreign_keys=[current_owner_user_id], lazy=True)
    reviewed_by_admin = db.relationship("Admin", foreign_keys=[reviewed_by_admin_id], lazy=True)
    transfer = db.relationship("ProjectOwnershipTransfer", foreign_keys=[transfer_id], lazy=True)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value((value or "OPEN").strip().upper(), PROJECT_CLAIM_STATUSES, key)


class ProjectServiceCoverage(db.Model):
    __tablename__ = "project_service_coverages"
    __table_args__ = (
        db.Index("ix_project_service_coverages_project_status_end", "project_id", "status", "coverage_end"),
        db.Index("ix_project_service_coverages_source", "source_type", "source_id", "source_reference"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    source_type = db.Column(db.String(40), nullable=False)
    source_id = db.Column(db.Integer, nullable=True)
    source_reference = db.Column(db.String(120), nullable=True)
    coverage_start = db.Column(db.DateTime, nullable=False, default=get_utc_now)
    coverage_end = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="ACTIVE", server_default="ACTIVE")
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    created_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    revoked_by_refund_id = db.Column(db.Integer, db.ForeignKey("payment_refunds.id"), nullable=True, index=True)
    reason = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", backref=db.backref("service_coverages", lazy=True, cascade="all, delete-orphan"))
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id], lazy=True)
    created_by_admin = db.relationship("Admin", foreign_keys=[created_by_admin_id], lazy=True)
    revoked_by_refund = db.relationship("PaymentRefund", lazy=True)

    @validates("source_type")
    def validate_source_type(self, key, value):
        return _validate_value((value or "").strip().upper(), PROJECT_SERVICE_COVERAGE_SOURCE_TYPES, key)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value((value or "ACTIVE").strip().upper(), PROJECT_SERVICE_COVERAGE_STATUSES, key)


class ProjectPair(db.Model):
    __tablename__ = "project_pairs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    pair_index = db.Column(db.Integer, nullable=False)

    image_filename = db.Column(db.String(255), nullable=True)
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
    scan_events = db.relationship("ScanEvent", backref="pair", lazy=True, cascade="all, delete-orphan")

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
# Scanner fallback analytics (V1 Wave 6)
# ---------------------------------------------------------------------
# "matched_scan" is a documented vocabulary anchor, not an insertable value
# here: a real successful detection+overlay is recorded exclusively via
# ScanLog.is_successful=True (untouched by this wave - see ScanEvent
# docstring below), and the client-facing fallback-event route in app.py
# deliberately never accepts it - a match can only ever be recorded by the
# server-side detection path, never claimed by a client POST.
SCAN_EVENT_TYPES = {
    "pair_fallback_view",
    "project_fallback_view",
    "recognition_timeout",
    "camera_unavailable",
}


class ScanEvent(db.Model):
    """Fallback/analytics event log (V1 Wave 6) - a table separate from
    ScanLog by design, not an oversight.

    ScanLog enforces UniqueConstraint(user_id, scan_session_id): at most ONE
    row per scan session. That is structurally wrong for fallback events,
    where a single session can produce more than one (a recognition-timeout
    prompt, then later a pair fallback view, etc). ScanLog is also already
    read by unfiltered aggregates that do not (and must not have to) know
    about fallback semantics - e.g. admin_dashboard's
    `ScanLog.query.filter_by(project_id=p.id).count()` and
    `ScanLog.query.count()` "total_scans" stats have no is_successful filter
    at all. Extending ScanLog with new event-type rows would silently
    inflate those existing counters with fallback/timeout events unless
    every call site were individually audited and patched. A new additive
    table keeps that risk at zero: ScanLog and every one of its existing
    call sites are completely untouched by this feature, so every
    pre-migration ScanLog row keeps meaning exactly what it always meant -
    is_successful=True is still "matched scan", unmoved and unredefined.

    client_event_id is a client-generated UUID and is the sole idempotency
    key: a flaky-network retry of the exact same logical event resends the
    exact same client_event_id and gets a safe idempotent no-op on the
    unique-constraint collision (see the fallback-event route in app.py) -
    enforced at the DB level via a UNIQUE constraint, not just an in-app
    duplicate check. A client must generate a fresh UUID per real event and
    never reuse one across a genuinely different event/project/pair.
    """
    __tablename__ = "scan_events"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    # Contextual pair hint: the specific pair the scanner was tracking
    # toward (if any) when the fallback/timeout/camera event occurred.
    # Nullable - camera_unavailable in particular can occur before any pair
    # was ever identified.
    pair_id = db.Column(db.Integer, db.ForeignKey("project_pairs.id"), nullable=True, index=True)

    event_type = db.Column(db.String(30), nullable=False, index=True)
    scan_session_id = db.Column(db.String(100), nullable=True, index=True)
    client_event_id = db.Column(db.String(36), nullable=False, unique=True, index=True)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    @validates("event_type")
    def validate_event_type(self, key, value):
        return _validate_value(value, SCAN_EVENT_TYPES, key)

    def __repr__(self):
        return f"<ScanEvent project={self.project_id} type={self.event_type}>"


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
# Human-reviewed content reports
# ---------------------------------------------------------------------
CONTENT_REPORT_REASONS = {
    "EXPLICIT_OR_INAPPROPRIATE",
    "VIOLENCE_OR_DANGER",
    "HATE_OR_HARASSMENT",
    "SCAM_OR_MISLEADING",
    "COPYRIGHT_OR_IP",
    "PRIVACY",
    "SPAM",
    "OTHER",
}
CONTENT_REPORT_STATUSES = {"OPEN", "UNDER_REVIEW", "ACTION_TAKEN", "DISMISSED"}
CONTENT_REPORT_ACTIONS = {
    "NONE",
    "PROJECT_SUSPENDED",
    "CREATOR_CONTACT_REQUIRED",
    "LEGAL_REVIEW_REQUIRED",
    "OTHER",
}


class ContentReport(db.Model):
    __tablename__ = "content_reports"
    __table_args__ = (
        db.Index("ix_content_reports_project_status", "project_id", "status"),
        db.Index("ix_content_reports_created_at", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False, index=True)
    reporter_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    reporter_email = db.Column(db.String(255), nullable=True)
    reporter_session_hash = db.Column(db.String(64), nullable=True, index=True)
    reporter_ip_hash = db.Column(db.String(64), nullable=True, index=True)
    reason = db.Column(db.String(40), nullable=False, index=True)
    details = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="OPEN", server_default="OPEN", index=True)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)
    resolution_action = db.Column(db.String(40), nullable=True)
    resolution_reason = db.Column(db.Text, nullable=True)
    metadata_json = db.Column(db.Text, nullable=True)

    project = db.relationship("Project", backref=db.backref("content_reports", lazy=True, cascade="all, delete-orphan"))
    reporter_user = db.relationship("User", foreign_keys=[reporter_user_id], lazy=True)
    reviewed_by_admin = db.relationship("Admin", foreign_keys=[reviewed_by_admin_id], lazy=True)

    @validates("reason")
    def validate_reason(self, key, value):
        return _validate_value((value or "").strip().upper(), CONTENT_REPORT_REASONS, key)

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value((value or "OPEN").strip().upper(), CONTENT_REPORT_STATUSES, key)

    @validates("resolution_action")
    def validate_resolution_action(self, key, value):
        if value in (None, ""):
            return None
        return _validate_value((value or "").strip().upper(), CONTENT_REPORT_ACTIONS, key)


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


# ---------------------------------------------------------------------
# Resumable uploads (V1 Wave 5)
# ---------------------------------------------------------------------
UPLOAD_SESSION_PURPOSES = {"project_pair"}
UPLOAD_SESSION_STATUSES = {
    "active",       # accepting chunks
    "finalizing",   # atomic transition target while finalize is assembling/validating
    "assembled",    # file(s) validated, Project+ProjectPair created, quota consumed,
                    # but the RQ enqueue call itself failed/threw - see enqueue_project_pair_processing
                    # in app.py. Recoverable: calling finalize again on a session in this
                    # state retries ONLY the enqueue step (no re-validation, no second
                    # quota consumption, no duplicate Project/Pair).
    "completed",    # fully done: validated, persisted, queued for processing
    "cancelled",
    "expired",
    "failed",
}


class UploadSession(db.Model):
    """One row per resumable upload attempt (V1 Wave 5).

    Scope decision (documented, not an oversight): a single session covers
    exactly ONE new single-pair Project - one image + one video, uploaded as
    a single sequential byte stream (image bytes first, then video bytes;
    the split point is `image_size`). This keeps the chunk/offset contract
    dead simple (one monotonic offset over one server-side temp file) while
    still letting `finalize` create a real Project+ProjectPair exactly like
    the non-resumable /upload route does, and consume exactly the same one
    project-quota unit at exactly the same point (_reserve_project_quota_atomic,
    called once per project regardless of pair count - see app.py). Multi-pair
    resumable projects and "attach a resumable pair to an existing project"
    are out of scope for this wave.

    owner_user_id / owner_admin_id mirror Project's mutually-exclusive
    nullable ownership pair. `storage_token` is a server-generated UUID4
    (models.py never trusts client input for anything that becomes part of
    a filesystem path) used by app.py to build the isolated temp file path
    under TMP_UPLOADS_DIR - never derived from original_image_name/
    original_video_name, which are stored for display only.

    Status lifecycle: active -> finalizing -> (assembled -> completed) |
    (failed) | active -> cancelled | active -> expired. `finalizing` is only
    ever observed mid-request (the atomic conditional UPDATE gate against
    double finalization - see app.py finalize route); it is never a resting
    state a client should see.

    client_checksum_sha256 is an OPTIONAL client-declared sha256 of the full
    assembled byte stream (image+video concatenated). If provided at session
    creation, finalize recomputes the real digest of the assembled file and
    rejects on mismatch before any validation/quota/Project work begins. If
    omitted, no checksum comparison is performed - resumability's own
    sequential-offset contract (server tracks current_offset, rejects
    non-matching offsets) is the primary integrity guarantee; the checksum
    is an optional extra guard against a corrupted-but-same-length transfer.
    """
    __tablename__ = "upload_sessions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    owner_admin_id = db.Column(db.Integer, db.ForeignKey("admins.id"), nullable=True, index=True)

    purpose = db.Column(db.String(30), nullable=False, default="project_pair")
    project_name = db.Column(db.String(255), nullable=True)

    original_image_name = db.Column(db.String(255), nullable=True)
    original_video_name = db.Column(db.String(255), nullable=True)
    image_content_type = db.Column(db.String(100), nullable=True)
    video_content_type = db.Column(db.String(100), nullable=True)
    experience_type = db.Column(db.String(30), nullable=False, default="image_video", server_default="image_video")
    playback_mode = db.Column(db.String(30), nullable=False, default="tracked_overlay", server_default="tracked_overlay")

    image_size = db.Column(db.Integer, nullable=False)   # declared/expected image byte count
    video_size = db.Column(db.Integer, nullable=False)   # declared/expected video byte count
    expected_total_size = db.Column(db.Integer, nullable=False)  # image_size + video_size
    current_offset = db.Column(db.Integer, nullable=False, default=0)

    status = db.Column(db.String(20), nullable=False, default="active", index=True)

    storage_token = db.Column(db.String(36), unique=True, nullable=False, index=True)

    client_checksum_sha256 = db.Column(db.String(64), nullable=True)
    computed_checksum_sha256 = db.Column(db.String(64), nullable=True)

    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=True, index=True)
    pair_id = db.Column(db.Integer, db.ForeignKey("project_pairs.id"), nullable=True, index=True)

    failure_code = db.Column(db.String(50), nullable=True)

    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("Project", lazy=True)
    pair = db.relationship("ProjectPair", lazy=True)

    __table_args__ = (
        db.CheckConstraint("current_offset >= 0", name="ck_upload_session_offset_non_negative"),
        db.CheckConstraint("current_offset <= expected_total_size", name="ck_upload_session_offset_le_total"),
        db.CheckConstraint("image_size >= 0 AND video_size >= 0", name="ck_upload_session_sizes_non_negative"),
        db.CheckConstraint("experience_type IN ('image_video', 'direct_qr')", name="ck_upload_sessions_experience_type"),
        db.CheckConstraint("playback_mode IN ('tracked_overlay', 'detect_once', 'direct')", name="ck_upload_sessions_playback_mode"),
        db.Index("ix_upload_sessions_owner_user_status", "owner_user_id", "status"),
        db.Index("ix_upload_sessions_owner_admin_status", "owner_admin_id", "status"),
        db.Index("ix_upload_sessions_status_expires", "status", "expires_at"),
    )

    @validates("status")
    def validate_status(self, key, value):
        return _validate_value(value, UPLOAD_SESSION_STATUSES, key)

    @validates("purpose")
    def validate_purpose(self, key, value):
        return _validate_value(value, UPLOAD_SESSION_PURPOSES, key)

    @validates("experience_type")
    def validate_experience_type(self, key, value):
        return _validate_value(value or "image_video", PROJECT_EXPERIENCE_TYPES, key)

    @validates("playback_mode")
    def validate_playback_mode(self, key, value):
        return _validate_value(value or "tracked_overlay", PROJECT_PLAYBACK_MODES, key)

    def __repr__(self):
        owner = f"user:{self.owner_user_id}" if self.owner_user_id else f"admin:{self.owner_admin_id}"
        return f"<UploadSession id={self.id} ({owner}) status={self.status}>"


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
