# Authentication And Sessions

## User Registration

Route: `/register`, `app.py:2264`.

Flow:

1. GET renders `templates/user/register.html`.
2. POST reads email/name/phone/password.
3. Calls Google reCAPTCHA verifier with action `register`.
4. Validates password and duplicate email.
5. Creates `User` with trial subscription fields.
6. Creates `TrialDetails`.
7. Creates OTP with purpose `verify_email`.
8. Sends verification email.
9. Stores `pending_verify_email` in Flask session and redirects to `/verify-email/`.

## Email Verification

Route: `/verify-email/`, `app.py:2371`.

Valid OTP marks `User.is_verified=True`, sets `email_verified_at`, and clears `pending_verify_email`.

## Login

Route: `/login/`, `app.py:2415`.

- Passwords are stored using Werkzeug password hashes.
- Login checks failed attempts in `UserLoginActivity` and locks after 4 recent failures in 3 hours.
- Blocked users are denied.
- Trial status is synced/expired during login.
- On success, session stores `user_id`; helper `login_user` also supports `user_email`.

## Password Reset

Routes: `/forgot-password/`, `/reset-password/`, `app.py:2550` to `app.py:2607`.

OTP purpose is `reset_password`, stored in `pending_reset_email`.

## Admin Authentication

Routes: `/admin/login`, `/admin/forgot-password`, `/admin/reset-password`, `/admin/logout`.

Admin session keys: `admin_id`, `admin_email`, `admin_role`.

## CSRF And reCAPTCHA

- `WTF_CSRF_ENABLED` and `WTF_CSRF_CHECK_DEFAULT` are set false in `app.py:54` to `app.py:55`.
- reCAPTCHA v3 is used on registration and contact form paths.

## Security-Sensitive Behaviors To Preserve

- Password hashing.
- OTP deletion after verification.
- Failed login tracking and lockout.
- Blocked user handling.
- Admin/user session separation.
- Owner checks before editing/downloading/deleting projects.

