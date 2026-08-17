"""V1.1 P0-1: refund recovery / reconciliation (money path)."""
from datetime import datetime, timedelta

import pytest
from werkzeug.security import generate_password_hash


# ---------------------------------------------------------------------------
# Fakes. The provider is the authority in production, so in tests it is the
# only thing we lie about - never the local state machine.
# ---------------------------------------------------------------------------
class FakePaymentApi:
    def __init__(self, status="processed", fail=False, existing_refunds=None, fetch_error=False):
        self.status = status
        self.fail = fail
        self.refund_calls = []
        self.fetch_calls = []
        self.existing_refunds = list(existing_refunds or [])
        self.fetch_error = fetch_error

    def refund(self, payment_id, data):
        self.refund_calls.append((payment_id, data))
        if self.fail:
            raise RuntimeError("provider down secret-noise-do-not-leak")
        entity = {
            "id": f"rfnd_fake_{len(self.refund_calls)}",
            "payment_id": payment_id,
            "amount": data["amount"],
            "currency": "INR",
            "status": self.status,
        }
        self.existing_refunds.append(entity)
        return entity

    def fetch_multiple_refund(self, payment_id):
        self.fetch_calls.append(payment_id)
        if self.fetch_error:
            raise RuntimeError("provider unreachable secret-noise-do-not-leak")
        return {"entity": "collection", "count": len(self.existing_refunds), "items": list(self.existing_refunds)}


class FakeRazorpayClient:
    def __init__(self, **kwargs):
        self.payment = FakePaymentApi(**kwargs)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------
def _user(app_module, db_session, email="refund-user@example.com", scan_limit=100):
    user = app_module.User(
        email=email,
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_status="active",
        subscription_expires_at=datetime.utcnow() + timedelta(days=30),
        subscribed_project_limit=5,
        subscribed_scan_limit=scan_limit,
        projects_used=0,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _catalog(app_module, db_session, code="EXTRA10", addon_type="EXTRA_SCANS", **deltas):
    item = app_module.AddonCatalog(
        code=code,
        name=code,
        addon_type=addon_type,
        unit_amount=99.0,
        currency="INR",
        scan_delta=deltas.get("scan_delta", 10 if addon_type == "EXTRA_SCANS" else None),
        validity_days_delta=deltas.get("validity_days_delta"),
        project_delta=deltas.get("project_delta"),
        storage_bytes_delta=deltas.get(
            "storage_bytes_delta", 5 * 1024 * 1024 * 1024 if addon_type == "ACCOUNT_STORAGE" else None
        ),
        is_active=True,
        is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _fulfilled_purchase(app_module, db_session, user, item, payment_id="pay_p0_1", suffix="1"):
    purchase = app_module.AddonPurchase(
        order_id=f"ADDON_P0_{item.code}_{suffix}",
        user_id=user.id,
        catalog_id=item.id,
        quantity=1,
        amount=item.unit_amount,
        total_amount=item.unit_amount,
        currency=item.currency,
        status="pending",
        razorpay_order_id=f"order_p0_{item.code}_{suffix}",
        razorpay_payment_id=payment_id,
    )
    db_session.add(purchase)
    db_session.commit()
    assert app_module.fulfill_addon_purchase(purchase)["success"] is True
    db_session.refresh(purchase)
    return purchase


def _refund_row(app_module, db_session, admin, user, purchase, *, status, reconciliation, provider_refund_id=None):
    refund = app_module.PaymentRefund(
        addon_purchase_id=purchase.id,
        user_id=user.id,
        provider="RAZORPAY",
        provider_payment_id=purchase.razorpay_payment_id,
        provider_refund_id=provider_refund_id,
        amount=purchase.total_amount,
        currency=purchase.currency,
        status=status,
        reconciliation_status=reconciliation,
        reason="operator test",
        requested_by_admin_id=admin.id,
        requested_at=datetime.utcnow(),
        idempotency_key=f"refund:addon_purchase:{purchase.id}",
    )
    db_session.add(refund)
    db_session.commit()
    return refund


@pytest.fixture()
def refund_fixture(app_module, db_session, admin):
    user = _user(app_module, db_session)
    item = _catalog(app_module, db_session)
    purchase = _fulfilled_purchase(app_module, db_session, user, item)
    return user, item, purchase, admin

# ===========================================================================
# P0-1  REFUND RECOVERY
# ===========================================================================
def test_stale_idempotency_replay_of_failed_refund_is_not_a_fake_success(
    app_module, db_session, refund_fixture, monkeypatch
):
    """The whole blocker: replaying a FAILED refund used to answer success."""
    user, item, purchase, admin = refund_fixture
    _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())

    result = app_module.initiate_admin_refund(admin, addon_purchase=purchase, reason="retry please")

    assert result["success"] is False
    assert result["code"] == "REFUND_PREVIOUSLY_FAILED"
    assert result["refund"]["status"] == "REFUND_FAILED"


def test_successful_idempotent_replay_still_reports_success(app_module, db_session, refund_fixture, monkeypatch):
    """No regression: a genuinely REFUNDED row still replays as success."""
    user, item, purchase, admin = refund_fixture
    _refund_row(
        app_module, db_session, admin, user, purchase,
        status="REFUNDED", reconciliation="APPLIED", provider_refund_id="rfnd_settled",
    )
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())

    result = app_module.initiate_admin_refund(admin, addon_purchase=purchase, reason="replay")

    assert result["success"] is True
    assert result["replay"] is True
    assert result.get("code") is None


def test_failed_refund_retry_reuses_the_same_row_and_creates_no_second_record(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    refund_id = refund.id
    client = FakeRazorpayClient(status="processed")
    monkeypatch.setattr(app_module, "razorpay_client", client)

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "retried"
    # Provider was READ before being written to.
    assert client.payment.fetch_calls == [purchase.razorpay_payment_id]
    assert len(client.payment.refund_calls) == 1
    assert app_module.PaymentRefund.query.count() == 1
    assert app_module.PaymentRefund.query.get(refund_id).status == "REFUNDED"


def test_provider_failure_during_recovery_never_reverses_entitlements(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    before = int(user.subscribed_scan_limit or 0)
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(fail=True))

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "retry_failed"
    assert refund.status == "REFUND_FAILED"
    assert refund.reconciliation_status == "PENDING"
    db_session.refresh(user)
    assert int(user.subscribed_scan_limit or 0) == before
    assert app_module.EntitlementTransaction.query.filter_by(source_type="refund").count() == 0
    # No provider exception text escapes into the stored/returned message.
    assert "secret-noise" not in (refund.failure_message_safe or "")
    assert "secret-noise" not in result["message"]


def test_confirmed_provider_refund_with_failed_reconciliation_retries_local_only(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(
        app_module, db_session, admin, user, purchase,
        status="REFUNDED", reconciliation="FAILED", provider_refund_id="rfnd_already_done",
    )
    client = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", client)

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "reconciled"
    assert refund.reconciliation_status == "APPLIED"
    # The provider was never touched: money already moved.
    assert client.payment.refund_calls == []
    assert client.payment.fetch_calls == []


def test_recovery_adopts_an_existing_provider_refund_instead_of_issuing_a_second(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    already_there = {
        "id": "rfnd_out_of_band",
        "payment_id": purchase.razorpay_payment_id,
        "amount": app_module._refund_amount_paise(purchase.total_amount),
        "currency": "INR",
        "status": "processed",
    }
    client = FakeRazorpayClient(existing_refunds=[already_there])
    monkeypatch.setattr(app_module, "razorpay_client", client)

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "adopted_provider_state"
    assert client.payment.refund_calls == []  # NO duplicate provider refund
    assert refund.provider_refund_id == "rfnd_out_of_band"
    assert refund.status == "REFUNDED"


def test_processing_refund_with_no_provider_record_is_left_for_manual_review(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_PROCESSING", reconciliation="PENDING")
    client = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", client)

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "unresolved"
    assert client.payment.refund_calls == []
    assert refund.status == "REFUND_PROCESSING"


def test_unreadable_provider_state_never_issues_a_refund(app_module, db_session, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    client = FakeRazorpayClient(fetch_error=True)
    monkeypatch.setattr(app_module, "razorpay_client", client)

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "unresolved"
    assert client.payment.refund_calls == []
    assert "secret-noise" not in result["message"]


def test_manual_review_is_never_auto_resolved(app_module, db_session, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(
        app_module, db_session, admin, user, purchase,
        status="REFUNDED", reconciliation="MANUAL_REVIEW_REQUIRED", provider_refund_id="rfnd_manual",
    )
    client = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", client)

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "manual_review"
    assert result["changed"] is False
    assert refund.reconciliation_status == "MANUAL_REVIEW_REQUIRED"
    assert client.payment.refund_calls == []


def test_recovery_is_idempotent_on_a_settled_refund(app_module, db_session, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(
        app_module, db_session, admin, user, purchase,
        status="REFUNDED", reconciliation="APPLIED", provider_refund_id="rfnd_done",
    )
    client = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", client)

    first = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)
    second = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert first["outcome"] == second["outcome"] == "already_settled"
    assert first["changed"] is False and second["changed"] is False
    assert client.payment.refund_calls == []


def test_apply_twice_after_a_real_retry_is_idempotent(app_module, db_session, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    client = FakeRazorpayClient(status="processed")
    monkeypatch.setattr(app_module, "razorpay_client", client)

    app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)
    reversals_after_first = app_module.EntitlementTransaction.query.filter_by(source_type="refund").count()
    app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert len(client.payment.refund_calls) == 1
    assert app_module.EntitlementTransaction.query.filter_by(source_type="refund").count() == reversals_after_first
    assert app_module.PaymentRefund.query.count() == 1


def test_dry_run_recovery_writes_nothing(app_module, db_session, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    client = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", client)
    activities_before = app_module.AdminActivity.query.count()

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=False)

    assert result["outcome"] == "would_retry_provider"
    assert result["changed"] is False
    assert client.payment.refund_calls == []
    db_session.refresh(refund)
    assert refund.status == "REFUND_FAILED"
    assert refund.reconciliation_status == "PENDING"
    assert app_module.AdminActivity.query.count() == activities_before


def test_refund_recovery_never_deletes_media(app_module, db_session, refund_fixture, monkeypatch, tmp_path):
    """Full-refund reconciliation must not touch stored media (locked rule)."""
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))
    media_root = tmp_path / "static_uploads"
    before = sorted(str(p) for p in media_root.rglob("*")) if media_root.exists() else []

    app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    after = sorted(str(p) for p in media_root.rglob("*")) if media_root.exists() else []
    assert after == before


def test_account_storage_refund_leaves_overage_allowed(app_module, db_session, admin, monkeypatch):
    """Post-refund storage overage stays allowed; only the ledger moves."""
    user = _user(app_module, db_session, email="storage-refund@example.com")
    item = _catalog(app_module, db_session, code="STORE5", addon_type="ACCOUNT_STORAGE")
    purchase = _fulfilled_purchase(app_module, db_session, user, item, payment_id="pay_store_1", suffix="store")
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUNDED", reconciliation="FAILED",
                         provider_refund_id="rfnd_store")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient())

    result = app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert result["outcome"] == "reconciled"
    # The reversal is a negative ledger row, not a deletion or a hard counter.
    reversal = app_module.EntitlementTransaction.query.filter_by(source_type="refund", source_id=refund.id).first()
    assert reversal is not None
    assert reversal.delta_value < 0
    assert app_module.MediaObject.query.filter_by(status="DELETED").count() == 0


def test_recovery_is_audited_when_admin_triggered(app_module, db_session, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    monkeypatch.setattr(app_module, "razorpay_client", FakeRazorpayClient(status="processed"))

    app_module.recover_payment_refund(refund, admin=admin, apply_changes=True)

    assert app_module.AdminActivity.query.filter_by(activity_type="refund_recovery").count() == 1


def test_reconcile_refunds_cli_dry_run_writes_nothing_and_flags_unresolved(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    client = FakeRazorpayClient()
    monkeypatch.setattr(app_module, "razorpay_client", client)

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["reconcile-refunds"])

    assert "Mode: dry-run" in result.output
    assert "would_retry_provider" in result.output
    assert "Dry run: nothing was written." in result.output
    assert client.payment.refund_calls == []
    db_session.refresh(refund)
    assert refund.status == "REFUND_FAILED"


def test_reconcile_refunds_cli_apply_is_idempotent_and_exits_clean(
    app_module, db_session, refund_fixture, monkeypatch
):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    client = FakeRazorpayClient(status="processed")
    monkeypatch.setattr(app_module, "razorpay_client", client)
    runner = app_module.app.test_cli_runner()

    first = runner.invoke(args=["reconcile-refunds", "--apply"])
    second = runner.invoke(args=["reconcile-refunds", "--apply"])

    assert "Mode: apply" in first.output
    assert "Recovered: 1" in first.output
    assert len(client.payment.refund_calls) == 1
    # Second pass finds nothing left to do and exits 0.
    assert "Refunds needing attention: 0" in second.output
    assert second.exit_code == 0
    assert app_module.PaymentRefund.query.count() == 1


def test_reconcile_refunds_cli_rejects_conflicting_modes(app_module, refund_fixture):
    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["reconcile-refunds", "--apply", "--dry-run"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_out_of_band_provider_refund_is_correlated_not_dropped(app_module, db_session, refund_fixture):
    """A dashboard refund with no local record must stay visible and auditable."""
    user, item, purchase, admin = refund_fixture
    event = app_module.RazorpayWebhookEvent(
        idempotency_key="refund.processed|rfnd_dashboard",
        event_type="refund.processed",
        payload_hash="0" * 64,
        processing_status="received",
    )
    db_session.add(event)
    db_session.commit()

    handled = app_module._process_refund_webhook_event(
        event,
        {
            "id": "rfnd_dashboard",
            "payment_id": purchase.razorpay_payment_id,
            "amount": app_module._refund_amount_paise(purchase.total_amount),
            "currency": "INR",
            "status": "processed",
        },
        None,
    )

    assert handled is True
    db_session.refresh(event)
    assert event.failure_code == app_module.OUT_OF_BAND_REFUND_FAILURE_CODE
    assert event.addon_purchase_id == purchase.id
    # No PaymentRefund is fabricated, so no admin identity is invented.
    assert app_module.PaymentRefund.query.count() == 0
    assert [e.id for e in app_module.unlinked_out_of_band_refund_events()] == [event.id]


def test_uncorrelatable_refund_webhook_still_reports_unknown(app_module, db_session, refund_fixture):
    user, item, purchase, admin = refund_fixture
    event = app_module.RazorpayWebhookEvent(
        idempotency_key="refund.processed|rfnd_nowhere",
        event_type="refund.processed",
        payload_hash="1" * 64,
        processing_status="received",
    )
    db_session.add(event)
    db_session.commit()

    app_module._process_refund_webhook_event(
        event,
        {"id": "rfnd_nowhere", "payment_id": "pay_not_ours", "amount": 100, "currency": "INR", "status": "processed"},
        None,
    )

    db_session.refresh(event)
    assert event.failure_code == "unknown_refund"


def test_admin_recover_refund_api_requires_apply_to_mutate(app_module, db_session, client, refund_fixture, monkeypatch):
    user, item, purchase, admin = refund_fixture
    refund = _refund_row(app_module, db_session, admin, user, purchase, status="REFUND_FAILED", reconciliation="PENDING")
    fake = FakeRazorpayClient(status="processed")
    monkeypatch.setattr(app_module, "razorpay_client", fake)
    with client.session_transaction() as sess:
        sess["admin_id"] = admin.id

    preview = client.post(f"/admin/api/refunds/{refund.id}/recover", json={})
    assert preview.status_code == 200
    assert preview.get_json()["recovery"]["outcome"] == "would_retry_provider"
    assert fake.payment.refund_calls == []

    applied = client.post(f"/admin/api/refunds/{refund.id}/recover", json={"apply": True})
    assert applied.status_code == 200
    assert applied.get_json()["recovery"]["outcome"] == "retried"
    assert len(fake.payment.refund_calls) == 1
