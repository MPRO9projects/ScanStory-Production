# models.py — ScanStory complete DB models with subscription system

import json
from datetime import datetime as dt
import os

from flask_sqlalchemy import SQLAlchemy
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
        return max(0, self.subscribed_project_limit - self.projects_used)

    @property
    def remaining_scans(self):
        """Calculate remaining scans"""
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
    razorpay_order_id = db.Column(db.String(255), nullable=True, index=True)
    razorpay_payment_id = db.Column(db.String(255), nullable=True, index=True)
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
# OTP codes
# ---------------------------------------------------------------------
class OTPCode(db.Model):
    __tablename__ = "otp_codes"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    email = db.Column(db.String(255), nullable=False, index=True)
    code = db.Column(db.String(6), nullable=False)
    purpose = db.Column(db.String(50), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)

    is_used = db.Column(db.Boolean, default=False)
    used_at = db.Column(db.DateTime, nullable=True)
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