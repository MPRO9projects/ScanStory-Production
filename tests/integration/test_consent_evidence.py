from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash


def _registration_payload(email="consent@example.com", include_terms=True):
    payload = {
        "email": email,
        "first_name": "Consent",
        "last_name": "User",
        "phone": "123",
        "password1": "password123",
        "password2": "password123",
    }
    if include_terms:
        payload["terms"] = "accepted"
    return payload


def test_registration_requires_terms_and_privacy_acceptance(client, app_module):
    response = client.post("/register", data=_registration_payload(include_terms=False))

    assert response.status_code == 200
    assert b"Please accept the Terms and Privacy Policy" in response.data
    assert app_module.User.query.filter_by(email="consent@example.com").first() is None
    assert app_module.UserConsentEvidence.query.count() == 0


def test_registration_creates_terms_and_privacy_evidence(client, app_module):
    response = client.post("/register", data=_registration_payload(), follow_redirects=False)

    assert response.status_code == 302
    user = app_module.User.query.filter_by(email="consent@example.com").first()
    assert user is not None
    records = app_module.UserConsentEvidence.query.filter_by(user_id=user.id).all()
    assert {record.consent_type for record in records} == {"TERMS", "PRIVACY"}
    assert {record.policy_version for record in records} == {"v1"}
    assert {record.source_context for record in records} == {"registration"}
    assert all(record.accepted_at for record in records)
    assert all(record.metadata_dict["form_field"] == "terms" for record in records)


def test_consent_evidence_duplicate_recording_is_idempotent(app_module, db_session, normal_user):
    accepted_at = datetime.utcnow()

    app_module._record_registration_consent_evidence(normal_user, accepted_at)
    app_module._record_registration_consent_evidence(normal_user, accepted_at + timedelta(seconds=1))
    db_session.commit()

    records = app_module.UserConsentEvidence.query.filter_by(user_id=normal_user.id).all()
    assert len(records) == 2
    assert {record.consent_type for record in records} == {"TERMS", "PRIVACY"}
    assert {record.policy_version for record in records} == {"v1"}


def test_new_policy_version_preserves_consent_history(app_module, db_session, normal_user, monkeypatch):
    app_module._record_registration_consent_evidence(normal_user, datetime.utcnow())
    db_session.commit()

    monkeypatch.setenv("SCANSTORY_TERMS_POLICY_VERSION", "v2")
    monkeypatch.setenv("SCANSTORY_PRIVACY_POLICY_VERSION", "v2")
    app_module._record_registration_consent_evidence(normal_user, datetime.utcnow())
    db_session.commit()

    records = app_module.UserConsentEvidence.query.filter_by(user_id=normal_user.id).all()
    assert len(records) == 4
    assert {record.policy_version for record in records} == {"v1", "v2"}


def test_consent_evidence_is_user_scoped(app_module, db_session, normal_user, plan):
    other = app_module.User(
        email="other-consent@example.com",
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="trial",
    )
    db_session.add(other)
    db_session.commit()

    app_module._record_registration_consent_evidence(normal_user, datetime.utcnow())
    app_module._record_registration_consent_evidence(other, datetime.utcnow())
    db_session.commit()

    assert app_module.UserConsentEvidence.query.filter_by(user_id=normal_user.id).count() == 2
    assert app_module.UserConsentEvidence.query.filter_by(user_id=other.id).count() == 2


def test_consent_evidence_table_has_expected_constraints(app_module):
    inspector = app_module.inspect(app_module.db.engine)
    columns = {column["name"] for column in inspector.get_columns("user_consent_evidence")}
    assert {
        "user_id",
        "consent_type",
        "policy_version",
        "accepted_at",
        "source_context",
        "evidence_metadata",
    }.issubset(columns)
    unique_names = {constraint["name"] for constraint in inspector.get_unique_constraints("user_consent_evidence")}
    assert "uq_user_consent_type_version_source" in unique_names
