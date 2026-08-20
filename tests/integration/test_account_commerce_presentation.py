"""Presentation contracts for the auth / account / plan / payment surface.

These are the things that were WRONG on those pages and must not come back, so
each test names a specific defect rather than a generic style rule:

  * the remember-me checkbox had no explicitly-associated label;
  * verify_email.html displayed a hardcoded 10-minute expiry countdown while
    the server issues those codes with minutes=2;
  * email_verification.html read `now.year`, which send_email_verification_otp()
    never passes, so every verification email shipped a blank copyright year;
  * payment_success_email.html began with a stray literal "v" before <!DOCTYPE>;
  * the plans page called its panels "V1.1 Account Families" / "V1.1 Commercial
    Fit" and the account page called one "Effective Entitlement";
  * the plans FAQ promised cancellation, which no route implements;
  * subscribe.html read `trial`, a variable neither route passes;
  * payment_success.html dereferenced nullable datetime columns and compared a
    NULL plan limit with `> 0`, both of which 500 for a paying customer;
  * design-system.css was linked BEFORE tailwind.build.css on every one of these
    pages, which is the inversion _head_assets.html exists to prevent.

Every check runs against RENDERED output unless it is explicitly a source-level
guarantee (load order, template-literal contracts), so a source comment cannot
satisfy one.
"""
import re
from pathlib import Path

import pytest


TEMPLATES = Path("templates")

# The pages this lane owns that a browser renders directly.
BROWSER_PAGES = (
    "user/login.html",
    "user/register.html",
    "user/verify_email.html",
    "user/forgot_password.html",
    "user/reset_password.html",
    "user/profile.html",
    "user/subscribe.html",
    "user/payment_success.html",
)

# Vocabulary that belongs in the codebase, never on a customer's screen.
FORBIDDEN_TERMS = (
    "RQ",
    "Redis",
    "worker",
    "queue",
    "entitlement engine",
    "Effective Entitlement",
    "subscription_taken_at",
    "FX quote",
    "webhook",
    "reconciliation",
    "V1.1",
    "Wave 2",
    "Wave 3",
)


def read_template(name):
    return (TEMPLATES / name).read_text(encoding="utf-8", errors="ignore")


def strip_jinja_comments(text):
    """Drop {# ... #} blocks. Engineering rationale lives in those, and it is
    never rendered - only the surviving text can reach a customer."""
    return re.sub(r"\{#.*?#\}", "", text, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Shared design foundation and load order
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("template_name", BROWSER_PAGES)
def test_pages_pull_the_design_system_through_the_shared_head_partial(template_name):
    html = strip_jinja_comments(read_template(template_name))
    assert '{% include "user/_head_assets.html" %}' in html, (
        "the page must take design-system.css/font/icons from the shared partial"
    )
    # No page may keep its own competing copy of what the partial provides.
    assert "css/design-system.css" not in html
    assert "fonts.googleapis.com/css2" not in html
    assert "font-awesome" not in html


@pytest.mark.parametrize("template_name", BROWSER_PAGES)
def test_design_system_is_included_after_tailwind_not_before(template_name):
    """design-system.css documents that it must load AFTER Tailwind. Every one of
    these pages linked it first, so Tailwind's utilities silently overrode the
    .ss-* tokens the file exists to provide."""
    # Comments stripped first: the rationale comments themselves mention
    # "<style>", and matching one of those would measure the wrong thing.
    html = strip_jinja_comments(read_template(template_name))
    include_at = html.index('{% include "user/_head_assets.html" %}')
    if "css/tailwind.build.css" in html:
        assert html.index("css/tailwind.build.css") < include_at
    # And always before the page's own <style>, which overrides in turn.
    assert include_at < html.index("<style>")


# ---------------------------------------------------------------------------
# Login: the remember-me label is the explicitly-called-out a11y contract
# ---------------------------------------------------------------------------
def test_remember_me_checkbox_has_an_explicitly_associated_label(client):
    html = client.get("/login/").get_data(as_text=True)

    match = re.search(r'<input[^>]*name="remember"[^>]*>', html)
    assert match, "the remember-me checkbox must still be present"
    checkbox = match.group(0)

    id_match = re.search(r'id="([^"]+)"', checkbox)
    assert id_match, "remember-me needs an id so a label can point at it"
    assert f'for="{id_match.group(1)}"' in html, (
        "remember-me needs a <label for=...>, not just a wrapping element"
    )


def test_login_keeps_its_form_contract(client):
    html = client.get("/login/").get_data(as_text=True)
    assert 'name="email"' in html
    assert 'name="password"' in html
    assert 'name="remember"' in html
    # One-line hidden CSRF input: tests/security/test_csrf_and_headers.py
    # extracts it with exactly this attribute order.
    assert re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert 'autocomplete="current-password"' in html
    assert "/forgot-password/" in html
    assert "/register" in html


def test_login_password_reveal_is_a_button_with_a_pressed_state(client):
    html = client.get("/login/").get_data(as_text=True)
    assert 'id="togglePassword"' in html
    assert 'aria-pressed="false"' in html
    assert 'aria-controls="passwordInput"' in html


def test_login_errors_are_announced_and_not_silently_auto_dismissed(client):
    """A failed sign-in must reach a screen reader as an alert, and the script
    must only self-dismiss role=status messages - an error the user has to act
    on used to disappear after five seconds."""
    response = client.post("/login/", data={"email": "nobody@example.com", "password": "wrong"})
    html = response.get_data(as_text=True)
    assert 'role="alert"' in html
    assert "Invalid email or password." in html
    assert "getAttribute('role') === 'status'" in read_template("user/login.html")


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
def test_register_keeps_every_field_and_hook_the_backend_reads(client):
    html = client.get("/register").get_data(as_text=True)
    for field in ("email", "first_name", "last_name", "phone", "password1", "password2", "terms"):
        assert f'name="{field}"' in html, field
    assert 'name="g-recaptcha-response"' in html, "the reCAPTCHA token input must stay in the DOM"
    assert re.search(r'name="csrf_token" value="([^"]+)"', html)


def test_register_states_the_real_password_rule_and_makes_no_strength_claim(client):
    html = client.get("/register").get_data(as_text=True)
    # app.py enforces len(password1) >= 6 and nothing else.
    assert "At least 6 characters." in html
    assert 'minlength="6"' in html
    body = strip_jinja_comments(html).lower()
    for fake in ("strong password", "password strength", "very secure", "weak password"):
        assert fake not in body


def test_register_explains_the_verification_step_that_follows(client):
    html = client.get("/register").get_data(as_text=True)
    assert "code to confirm this address" in html


def test_register_surfaces_the_plan_the_visitor_arrived_from(client, plan):
    """selected_plan is passed by the register route and used to be dropped on
    the floor, so anyone arriving from a plan card lost that context."""
    html = client.get(f"/register?plan_id={plan.id}").get_data(as_text=True)
    assert plan.plan_name in html


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------
def test_verify_email_states_no_expiry_it_cannot_know(client, app_module, db_session, normal_user):
    """The page used to render a hardcoded 10:00 countdown. _create_otp is called
    with minutes=2 and the route passes no expiry into the template, so any
    concrete duration here is a number the page invented."""
    with client.session_transaction() as session:
        session["pending_verify_email"] = normal_user.email
        session["pending_verify_challenge_id"] = "x"

    html = client.get("/verify-email/").get_data(as_text=True)
    assert "10:00" not in html
    assert "Code Expires in" not in html
    assert "Codes expire a few minutes after they are sent" in html


def test_verify_email_resend_stays_a_post_form_at_the_exact_route(client, normal_user):
    with client.session_transaction() as session:
        session["pending_verify_email"] = normal_user.email

    html = client.get("/verify-email/").get_data(as_text=True)
    # GET /resend-otp/ is a 405; this must never become a link.
    assert 'method="POST" action="/resend-otp/"' in html
    assert 'name="otp"' in html


def test_verify_email_without_a_device_challenge_says_what_to_do(client, normal_user):
    with client.session_transaction() as session:
        session["pending_verify_email"] = normal_user.email

    html = client.get("/verify-email/").get_data(as_text=True)
    assert "Request a new verification code on this device" in html


# ---------------------------------------------------------------------------
# Forgot / reset password
# ---------------------------------------------------------------------------
def test_forgot_password_copy_never_confirms_an_account_exists(client):
    """The route flashes the same neutral line for a known address, an unknown
    address, a send failure and a rate-limited request, so the page must not
    promise more than 'if there is an account'."""
    html = client.get("/forgot-password/").get_data(as_text=True)
    assert "If there is a ScanStory account for it" in html
    body = strip_jinja_comments(html).lower()
    assert "we found your account" not in body
    assert "no account with that email" not in body
    assert 'name="email"' in html


def test_reset_password_shows_the_address_and_the_real_password_rule(client, normal_user):
    with client.session_transaction() as session:
        session["pending_reset_email"] = normal_user.email

    html = client.get("/reset-password/").get_data(as_text=True)
    # `email` is passed by the route and was previously never rendered.
    assert normal_user.email in html
    assert "At least 6 characters." in html
    for field in ("otp", "new_password", "confirm_password"):
        assert f'name="{field}"' in html, field


def test_reset_password_validation_errors_are_announced(client, normal_user):
    with client.session_transaction() as session:
        session["pending_reset_email"] = normal_user.email

    response = client.post(
        "/reset-password/",
        data={"otp": "000000", "new_password": "abcdef", "confirm_password": "different"},
    )
    html = response.get_data(as_text=True)
    assert 'role="alert"' in html
    assert "Passwords do not match." in html


# ---------------------------------------------------------------------------
# Product language
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/pricing", "/profile", "/subscribe"])
def test_account_and_plan_pages_expose_no_internal_vocabulary(client, login_user, path):
    body = client.get(path).get_data(as_text=True)
    for term in FORBIDDEN_TERMS:
        assert term not in body, f"{path} leaks internal term {term!r}"


@pytest.mark.parametrize("template_name", BROWSER_PAGES)
def test_no_internal_vocabulary_survives_in_renderable_template_text(template_name):
    """Belt and braces for the branches the fixtures above do not reach: strip
    the Jinja comments (which legitimately discuss the internals) and check what
    is left."""
    renderable = strip_jinja_comments(read_template(template_name))
    for term in ("Effective Entitlement", "V1.1", "entitlement engine", "subscription_taken_at"):
        assert term not in renderable, f"{template_name} would render {term!r}"


# ---------------------------------------------------------------------------
# Subscription state
# ---------------------------------------------------------------------------
def test_plan_status_is_a_label_never_the_raw_column(client, login_user, db_session):
    """subscription_status only ever holds trial/active/expired/limit_reached.
    'limit_reached' used to reach the screen as "Limit_Reached"."""
    login_user.subscription_status = "limit_reached"
    db_session.commit()

    for path in ("/subscribe", "/profile"):
        body = client.get(path).get_data(as_text=True)
        assert "Plan limit reached" in body, path
        assert "Limit_Reached" not in body, path
        assert "limit_reached" not in body, path


def test_trial_end_date_actually_renders_on_the_plans_page(client, login_user, db_session):
    """subscribe.html guarded this on `trial`, which neither pricing_page() nor
    subscribe_page() passes - so the branch was dead and no visitor ever saw it.
    It reads user.trial_details now."""
    login_user.subscription_status = "trial"
    db_session.commit()
    assert login_user.trial_details is not None, "fixture should provide trial details"

    body = client.get("/subscribe").get_data(as_text=True)
    assert "Trial ends" in body
    assert login_user.trial_details.trial_end.strftime("%d %b %Y") in body


def test_active_plan_shows_its_end_date_and_denies_auto_renewal(client, login_user, db_session):
    """Nothing in this codebase charges a renewal: there is no auto_renew field,
    no recurring Razorpay subscription and no cancellation route. The page must
    not imply otherwise."""
    from datetime import datetime, timedelta

    login_user.subscription_status = "active"
    login_user.subscription_expires_at = datetime.utcnow() + timedelta(days=45)
    db_session.commit()

    body = client.get("/profile").get_data(as_text=True)
    assert "Active until" in body
    assert "do not auto-renew" in body


def test_plans_faq_does_not_promise_a_cancellation_that_does_not_exist(client):
    body = client.get("/pricing").get_data(as_text=True)
    assert "cancel your subscription" not in body
    assert "Nothing renews on its own" in body


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
def test_usd_is_reference_only_and_inr_is_the_charged_currency(client):
    body = client.get("/pricing").get_data(as_text=True)
    assert "Approx. USD reference only" in body
    assert "charged in INR" in body
    # The rate behind the toggle is a configured fallback, never a live feed.
    assert "not a live market rate" in body
    lowered = body.lower()
    assert "pay in usd" not in lowered
    assert "live exchange rate" not in lowered


def test_checkout_review_step_states_an_inr_amount_before_razorpay_opens(client, login_user):
    body = client.get("/subscribe").get_data(as_text=True)
    # Razorpay checkout.js and the JS CSRF literal that
    # tests/security/test_csrf_and_headers.py extracts must both survive.
    assert "checkout.razorpay.com/v1/checkout.js" in body
    assert re.search(r"'X-CSRFToken':\s*'([^']+)'", body)
    # The review step exists, is priced, and names the currency.
    assert "Review your order" in body
    assert "Pay ₹" in body
    assert "Amount due" in body
    assert "Charged in INR" in body


def test_plan_buttons_carry_their_own_data_and_a_double_submit_latch(client, login_user):
    body = client.get("/subscribe").get_data(as_text=True)
    assert "plan-choose-btn" in body
    assert "data-plan-price=" in body
    assert "data-plan-term=" in body
    # In-flight latch around the existing create-razorpay-order call.
    assert "paymentInFlight" in body
    assert "/create-razorpay-order" in body


def test_payment_failure_states_are_honest_about_money(client, login_user):
    """Only the branch where no order ever reached the provider may claim nothing
    was taken. The unconfirmed branch must NOT, and must not invite a retry that
    could double-charge."""
    body = client.get("/subscribe").get_data(as_text=True)
    assert "Payment could not be completed" in body
    assert "No payment was taken" in body
    assert "not-charged" in body
    assert "unconfirmed" in body
    assert "do not pay again" in body
    assert "Try this payment again" in body
    assert "Back to plans" in body


def test_payment_failed_route_lands_on_an_announced_recoverable_screen(client, login_user):
    """/payment-failed has no template of its own - it flashes and redirects to
    the plans page, so that flash block IS the failure screen. It used to render
    with no live region and no next step."""
    response = client.get("/payment-failed", follow_redirects=True)
    body = response.get_data(as_text=True)
    assert 'role="alert"' in body
    assert "Payment failed. Please try again." in body
    assert "Nothing was added to your account" in body
    assert "do not pay twice" in body
    assert "/contact" in body


def test_quota_errors_do_not_borrow_payment_recovery_copy(client, login_user, db_session):
    """The same flash category carries quota messages. Payment reassurance under
    "Project limit reached" would be nonsense."""
    login_user.subscribed_project_limit = 1
    login_user.projects_used = 1
    db_session.commit()

    response = client.get("/create-project", follow_redirects=True)
    body = response.get_data(as_text=True)
    if "Project limit reached" in body:
        assert "Nothing was added to your account" not in body


# ---------------------------------------------------------------------------
# Payment success
# ---------------------------------------------------------------------------
@pytest.fixture()
def paid_order(app_module, db_session, normal_user, plan):
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    order = app_module.PaymentOrder(
        order_id="ORDER_PRESENTATION_1",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.plan_amount,
        currency="INR",
        status="success",
        razorpay_payment_id="pay_presentation_1",
        payment_at=now,
        subscription_start=now,
        subscription_end=now + timedelta(days=180),
    )
    db_session.add(order)
    db_session.commit()
    return order


def test_payment_success_shows_a_formatted_amount_and_the_key_actions(client, login_user, paid_order):
    body = client.get(f"/payment-success?order_id={paid_order.order_id}").get_data(as_text=True)
    assert "Payment received" in body
    # Float column: this used to render as "₹1999.0".
    assert f"₹{paid_order.total_amount:.2f}" in body
    assert ".0<" not in body.split("Amount paid")[1][:200]
    assert paid_order.order_id in body
    assert "/dashboard" in body
    assert "/create-project" in body
    # The provider's payment id is provider-internal; it was printed twice.
    assert body.count(paid_order.razorpay_payment_id) == 0


def test_payment_success_survives_null_dates_and_an_unlimited_plan(
    client, login_user, db_session, paid_order, plan
):
    """Every one of these columns is nullable and every one of them was
    dereferenced unguarded, and `NULL > 0` is a TypeError in Jinja - so the
    biggest plan and any order missing a timestamp both 500'd this page."""
    paid_order.payment_at = None
    paid_order.subscription_start = None
    paid_order.subscription_end = None
    plan.total_project_limit = None
    plan.total_scan_limit = 999999
    db_session.commit()

    response = client.get(f"/payment-success?order_id={paid_order.order_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "999999" not in body
    assert "Unlimited" in body


# ---------------------------------------------------------------------------
# Email templates
# ---------------------------------------------------------------------------
def test_otp_email_renders_a_copyright_year_from_the_variable_callers_pass(app_module):
    """The template read `now.year`; send_email_verification_otp() passes `year`
    and no `now`, so the footer year was blank on every verification email."""
    with app_module.app.test_request_context():
        html = app_module.render_template(
            "user/email_verification.html", code="123456", minutes=2, year=2031
        )
    assert "123456" in html
    assert "2031" in html
    assert "expires in 2 minutes" in html


def test_otp_email_copy_serves_verification_and_password_reset_alike(app_module):
    """One template backs both send_email_verification_otp() and
    send_reset_password_otp(), so it must not say "verify your email"."""
    with app_module.app.test_request_context():
        html = app_module.render_template(
            "user/email_verification.html", code="654321", minutes=2, year=2031
        )
    assert "one-time ScanStory code" in html
    assert "Never share the code" in html


def test_payment_receipt_email_starts_at_the_doctype(app_module, db_session, normal_user, plan):
    """The file used to begin with a stray literal 'v', which rendered as the
    first visible character of every receipt."""
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    order = app_module.PaymentOrder(
        order_id="ORDER_EMAIL_1",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.plan_amount,
        currency="INR",
        status="success",
        payment_at=now,
        subscription_start=now,
        subscription_end=now + timedelta(days=180),
    )
    db_session.add(order)
    db_session.commit()

    with app_module.app.test_request_context():
        html = app_module.render_template(
            "user/payment_success_email.html", user=normal_user, plan=plan, order=order, year=2031
        )

    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "Payment received" in html
    assert f"₹{order.total_amount:.2f}" in html
    assert order.order_id in html
    assert "2031" in html
    # display:grid is ignored by Outlook's rendering engine; tables are not.
    assert "display: grid" not in html
    assert "display:grid" not in html


def test_payment_receipt_email_survives_null_timestamps(app_module, db_session, normal_user, plan):
    """Both call sites swallow exceptions from this send, so an unguarded
    .strftime() on a NULL silently cost the customer their receipt."""
    order = app_module.PaymentOrder(
        order_id="ORDER_EMAIL_2",
        user_id=normal_user.id,
        plan_id=plan.id,
        amount=plan.plan_amount,
        total_amount=plan.plan_amount,
        currency="INR",
        status="success",
    )
    db_session.add(order)
    db_session.commit()

    with app_module.app.test_request_context():
        html = app_module.render_template(
            "user/payment_success_email.html", user=normal_user, plan=plan, order=order, year=2031
        )
    assert "Payment received" in html
    assert order.order_id in html
