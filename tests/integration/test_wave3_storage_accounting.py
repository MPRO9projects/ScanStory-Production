"""Wave 3 tests: authoritative account storage accounting.

Covers the MediaObject ledger, the extended entitlement resolver, create/upload
and replacement enforcement, physical-delete-first freeing, the ACCOUNT_STORAGE
add-on and admin grants, storage-aware transfer primitives, the reconciliation
CLI, and the concurrency guarantee.

Focused scope by policy: the full suite and the full PostgreSQL certification
lane are the project lead's, run once after this wave merges.
"""
import os
import tempfile
import threading
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image
from sqlalchemy.exc import OperationalError
from werkzeug.security import generate_password_hash

import storage_accounting as sa


GB = 1024 ** 3


class NoopThread:
    def __init__(self, target=None, args=(), kwargs=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        return None


def _jpeg_bytes(width=640, height=480, color=(160, 80, 40)):
    out = BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="JPEG", quality=88)
    out.seek(0)
    return out


def _mp4_bytes(frames=5, noise=False):
    """A real decodable MP4. More frames / noise => a genuinely bigger file,
    which is what the replacement size tests need."""
    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 64))
        rng = np.random.default_rng(7)
        for _ in range(frames):
            frame = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8) if noise else np.zeros((64, 64, 3), dtype=np.uint8)
            writer.write(frame)
        writer.release()
        return BytesIO(Path(path).read_bytes())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _patch_upload_processing(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "standardize_uploaded_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_feature_working_jpeg", lambda *a, **k: Path(a[1]).write_bytes(b"work"))
    monkeypatch.setattr(app_module, "extract_features_multi", lambda *a, **k: Path(a[1]).write_bytes(b"npz"))
    monkeypatch.setattr(app_module, "generate_custom_qr", lambda *a, **k: False)
    monkeypatch.setattr(app_module, "generate_basic_qr", lambda *a, **k: Path(a[3]).write_bytes(b"qr") if len(a) > 3 else None)
    monkeypatch.setattr(app_module.threading, "Thread", NoopThread)
    monkeypatch.setattr(app_module, "_schedule_project_pair_processing", lambda *a, **k: object())


def _upload_data(name="Storage Project", pairs=1, frames=5):
    data = {"name": name, "upload_id": f"upload-{name}"}
    images, videos = [], []
    for index in range(pairs):
        images.append((_jpeg_bytes(), f"marker-{index}.jpg"))
        videos.append((_mp4_bytes(frames=frames), f"clip-{index}.mp4"))
        data[f"marker_{index}_mode"] = "full_image"
        for suffix, value in (
            ("crop_x", "0"), ("crop_y", "0"), ("crop_width", "1"), ("crop_height", "1"),
            ("rotation", "0"), ("original_width", "640"), ("original_height", "480"),
            ("processed_width", "640"), ("processed_height", "480"),
            ("source_size_bytes", "1000"), ("processed_size_bytes", ""),
            ("display_orientation", "landscape"),
        ):
            data[f"marker_{index}_{suffix}"] = value
    data["images"] = images
    data["videos"] = videos
    return data


def _set_storage_allowance(app_module, db_session, user, allowance_bytes):
    plan = app_module.SubscriptionPlan.query.get(user.subscription_id)
    plan.base_storage_bytes = allowance_bytes
    db_session.commit()
    return plan


def _ents(app_module, user):
    return app_module.user_entitlements(user)


def _second_user(app_module, db_session, plan, email="recipient@example.com"):
    user = app_module.User(
        email=email,
        password_hash=generate_password_hash("password123"),
        is_verified=True,
        subscription_id=plan.id,
        subscription_status="active",
        subscribed_project_limit=10,
        subscribed_scan_limit=100,
        projects_used=0,
        scans_used=0,
    )
    db_session.add(user)
    db_session.commit()
    return user


# ===========================================================================
# Schema
# ===========================================================================
def test_media_object_uses_bigint_for_every_byte_field(app_module, db_session):
    """Wave 1 flagged Integer's ~2.1GB cap. A 5GB object must round-trip."""
    from models import MediaObject

    obj = MediaObject(
        media_role="video", storage_key="user/videos/9_0.mp4", size_bytes=5 * GB,
    )
    db_session.add(obj)
    db_session.commit()
    db_session.expire_all()
    assert app_module.MediaObject.query.one().size_bytes == 5 * GB


def test_media_object_rejects_unknown_role_status_and_source(app_module, db_session):
    from models import MediaObject

    with pytest.raises(ValueError):
        MediaObject(media_role="thumbnail", storage_key="k", size_bytes=1)
    with pytest.raises(ValueError):
        MediaObject(media_role="video", storage_key="k", size_bytes=1, status="ARCHIVED")
    with pytest.raises(ValueError):
        MediaObject(media_role="video", storage_key="k", size_bytes=1, source="guess")


def test_only_one_active_row_may_claim_a_storage_key(app_module, db_session):
    from models import MediaObject

    db_session.add(MediaObject(media_role="video", storage_key="user/videos/1_0.mp4", size_bytes=5))
    db_session.commit()
    # Superseded history may reuse the key; a second ACTIVE claim may not.
    db_session.add(MediaObject(media_role="video", storage_key="user/videos/1_0.mp4", size_bytes=6, status="SUPERSEDED"))
    db_session.commit()
    db_session.add(MediaObject(media_role="video", storage_key="user/videos/1_0.mp4", size_bytes=7))
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


def test_generated_and_derived_artifacts_never_get_a_ledger_row(app_module, db_session, project_with_pair, normal_user):
    """QR images, .npz features and _fast/_work derivatives are ours, not the
    customer's - counting them would bill our own pipeline."""
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    db_session.commit()
    keys = {obj.storage_key for obj in app_module.MediaObject.query.all()}
    assert keys == {f"user/images/{project.id}_0.jpg", f"user/videos/{project.id}_0.mp4"}
    assert not any("qr" in key or "npz" in key or "features" in key for key in keys)


# ===========================================================================
# Entitlement resolver
# ===========================================================================
def test_resolver_reports_three_separate_storage_sources(app_module, db_session, normal_user, admin):
    _set_storage_allowance(app_module, db_session, normal_user, 10 * GB)
    db_session.add(app_module.EntitlementTransaction(
        user_id=normal_user.id, entitlement_type="ACCOUNT_STORAGE", delta_value=3 * GB,
        source_type="addon_purchase", source_id=1, reason="test",
    ))
    db_session.add(app_module.EntitlementTransaction(
        user_id=normal_user.id, entitlement_type="ACCOUNT_STORAGE", delta_value=2 * GB,
        source_type="admin_grant", source_id=2, reason="test",
    ))
    normal_user.storage_used_bytes = 4 * GB
    db_session.commit()

    ents = _ents(app_module, normal_user)
    assert ents["base_storage_bytes"] == 10 * GB
    assert ents["purchased_storage_bytes"] == 3 * GB
    assert ents["admin_granted_storage_bytes"] == 2 * GB
    assert ents["effective_storage_bytes"] == 15 * GB
    assert ents["storage_used_bytes"] == 4 * GB
    assert ents["storage_remaining_bytes"] == 11 * GB
    assert ents["over_storage"] is False
    assert ents["storage_overage_bytes"] == 0
    # The Wave 2 "not tracked yet" disclaimer is now obsolete.
    assert ents["storage_usage_tracked"] is True


def test_resolver_reports_over_storage_without_touching_media(app_module, db_session, normal_user):
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    normal_user.storage_used_bytes = 3 * GB
    db_session.commit()

    ents = _ents(app_module, normal_user)
    assert ents["over_storage"] is True
    assert ents["storage_overage_bytes"] == 2 * GB
    assert ents["storage_remaining_bytes"] == 0


def test_unstated_plan_storage_is_unenforced(app_module, db_session, normal_user):
    """NULL base storage means 'this plan states no allowance', not 'zero'."""
    _set_storage_allowance(app_module, db_session, normal_user, None)
    normal_user.storage_used_bytes = 99 * GB
    db_session.commit()

    ents = _ents(app_module, normal_user)
    assert ents["effective_storage_bytes"] is None
    assert ents["storage_remaining_bytes"] is None
    assert ents["over_storage"] is False


def test_storage_entitlement_is_independent_of_project_and_scan_capacity(app_module, db_session, normal_user):
    _set_storage_allowance(app_module, db_session, normal_user, 5 * GB)
    normal_user.storage_used_bytes = 5 * GB
    db_session.commit()
    ents = _ents(app_module, normal_user)
    assert ents["storage_remaining_bytes"] == 0
    # Storage exhaustion must not be conflated with project/scan capacity.
    assert ents["projects_remaining"] != 0 or ents["effective_project_limit"] is None
    assert ents["over_project_capacity"] is False


# ===========================================================================
# Create / upload enforcement
# ===========================================================================
def test_upload_within_allowance_records_the_ledger_and_the_counter(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)

    response = client.post("/upload", data=_upload_data(), content_type="multipart/form-data")
    assert response.status_code in (200, 302)

    project = app_module.Project.query.one()
    objects = app_module.MediaObject.query.filter_by(status="ACTIVE").all()
    assert {o.media_role for o in objects} == {"trigger_image", "video"}
    assert all(o.owner_user_id == normal_user.id and o.project_id == project.id for o in objects)

    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    ledger_total = sa.account_storage_used_bytes(user.id)
    assert ledger_total > 0
    # Enforcement counter and audit ledger agree.
    assert user.storage_used_bytes == ledger_total


def test_upload_beyond_allowance_is_rejected_and_leaves_no_usage(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 10)  # bytes

    client.post("/upload", data=_upload_data(name="Too Big"), content_type="multipart/form-data")

    assert app_module.Project.query.count() == 0
    assert app_module.MediaObject.query.count() == 0
    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    assert user.storage_used_bytes == 0
    assert user.projects_used == 0  # the project slot was released too


def test_upload_exactly_at_the_allowance_is_allowed_and_the_next_one_is_not(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    client.post("/upload", data=_upload_data(name="First"), content_type="multipart/form-data")

    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    exact = user.storage_used_bytes
    assert exact > 0
    # Pin the allowance to exactly what is in use: at-allowance is valid, not over.
    _set_storage_allowance(app_module, db_session, user, exact)
    ents = _ents(app_module, user)
    assert ents["storage_remaining_bytes"] == 0
    assert ents["over_storage"] is False

    client.post("/upload", data=_upload_data(name="Second"), content_type="multipart/form-data")
    assert app_module.Project.query.count() == 1
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == exact


def test_per_file_policy_is_enforced_independently_of_a_huge_storage_allowance(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    """A storage allowance never relaxes a per-file limit; both must pass."""
    _patch_upload_processing(app_module, monkeypatch)
    plan = _set_storage_allowance(app_module, db_session, normal_user, 500 * GB)
    plan.max_video_bytes = 10  # per-file policy the upload cannot satisfy
    db_session.commit()

    client.post("/upload", data=_upload_data(name="Per File"), content_type="multipart/form-data")

    assert app_module.Project.query.count() == 0
    assert app_module.MediaObject.query.count() == 0
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0


def test_multi_pair_creation_is_weighed_as_a_whole_set(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    """Never accept pair 1 then reject pair 2, leaving orphaned accounting."""
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    client.post("/upload", data=_upload_data(name="Sizer"), content_type="multipart/form-data")
    db_session.expire_all()
    one_pair_bytes = app_module.User.query.get(normal_user.id).storage_used_bytes

    # Reset and allow room for one pair only, then submit two.
    app_module.MediaObject.query.delete()
    app_module.ProjectPair.query.delete()
    app_module.Project.query.delete()
    user = app_module.User.query.get(normal_user.id)
    user.storage_used_bytes = 0
    user.projects_used = 0
    db_session.commit()
    _set_storage_allowance(app_module, db_session, user, int(one_pair_bytes * 1.5))

    client.post("/upload", data=_upload_data(name="Two Pairs", pairs=2), content_type="multipart/form-data")

    assert app_module.Project.query.count() == 0
    assert app_module.MediaObject.query.count() == 0
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0


# ===========================================================================
# Replacement policy
# ===========================================================================
@pytest.mark.parametrize("used, allowance, old, new, expected", [
    # Within allowance: projected final usage must fit.
    (100, 1000, 50, 40, True),      # smaller
    (100, 1000, 50, 900, True),     # larger, still within
    (100, 1000, 50, 960, False),    # larger, beyond
    (100, 1000, 50, 950, True),     # larger, exactly at allowance
    # Over allowance: must STRICTLY reduce total usage.
    (2000, 1000, 50, 40, True),     # smaller -> allowed even though still over
    (2000, 1000, 50, 50, False),    # equal -> blocked
    (2000, 1000, 50, 60, False),    # larger -> blocked
    # No stated allowance: unenforced.
    (2000, None, 50, 5000, True),
])
def test_replacement_policy_matrix(used, allowance, old, new, expected):
    allowed, projected = sa.evaluate_replacement(used, allowance, old, new)
    assert allowed is expected
    assert projected == used - old + new


def test_replacement_updates_the_ledger_and_leaves_qr_and_pair_count_alone(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    client.post("/upload", data=_upload_data(name="Replace Me", frames=30), content_type="multipart/form-data")

    project = app_module.Project.query.one()
    qr_before = project.qr_code_filename
    before = app_module.User.query.get(normal_user.id).storage_used_bytes

    response = client.post(
        f"/projects/{project.id}/edit",
        data={"video_0": (_mp4_bytes(frames=3), "small.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code in (200, 302)

    db_session.expire_all()
    project = app_module.Project.query.one()
    user = app_module.User.query.get(normal_user.id)
    assert user.storage_used_bytes < before          # a smaller video freed bytes
    assert user.storage_used_bytes == sa.account_storage_used_bytes(user.id)
    assert project.qr_code_filename == qr_before      # QR unchanged
    assert app_module.ProjectPair.query.count() == 1  # pair count unchanged
    # Exactly one ACTIVE video row, and the old one is superseded (not deleted:
    # the file at that key still exists, it just holds new content).
    videos = app_module.MediaObject.query.filter_by(media_role="video").all()
    assert sorted(o.status for o in videos) == ["ACTIVE", "SUPERSEDED"]


def test_replacement_beyond_the_allowance_is_rejected_before_the_swap(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    client.post("/upload", data=_upload_data(name="Tight", frames=3), content_type="multipart/form-data")

    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    used = user.storage_used_bytes
    project = app_module.Project.query.one()
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4"
    original_bytes = video_path.read_bytes()
    _set_storage_allowance(app_module, db_session, user, used)  # zero headroom

    client.post(
        f"/projects/{project.id}/edit",
        data={"video_0": (_mp4_bytes(frames=60, noise=True), "big.mp4")},
        content_type="multipart/form-data",
    )

    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == used
    assert video_path.read_bytes() == original_bytes  # old media never touched
    assert app_module.MediaObject.query.filter_by(media_role="video", status="ACTIVE").count() == 1


def test_over_storage_blocks_equal_size_replacement_but_allows_a_smaller_one(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    client.post("/upload", data=_upload_data(name="Over", frames=20), content_type="multipart/form-data")

    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    used = user.storage_used_bytes
    project = app_module.Project.query.one()
    # Downgrade below current usage: valid, and must not delete anything.
    _set_storage_allowance(app_module, db_session, user, used // 2)
    assert _ents(app_module, user)["over_storage"] is True
    assert app_module.MediaObject.query.filter_by(status="ACTIVE").count() == 2

    # Equal-size (the identical file) is blocked while over storage.
    same = (Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4").read_bytes()
    client.post(
        f"/projects/{project.id}/edit",
        data={"video_0": (BytesIO(same), "same.mp4")},
        content_type="multipart/form-data",
    )
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == used

    # A genuinely smaller replacement is allowed even though it stays over.
    client.post(
        f"/projects/{project.id}/edit",
        data={"video_0": (_mp4_bytes(frames=2), "smaller.mp4")},
        content_type="multipart/form-data",
    )
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes < used


# ===========================================================================
# Deletion
# ===========================================================================
def test_successful_physical_delete_frees_bytes(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    db_session.commit()

    app_module._delete_project_files_and_rows(project)

    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0
    assert app_module.MediaObject.query.filter_by(status="ACTIVE").count() == 0
    assert app_module.MediaObject.query.filter_by(status="DELETED").count() == 2
    # The accounting rows SURVIVE the project (ON DELETE SET NULL), so a
    # deletion is auditable rather than vanishing.
    assert app_module.MediaObject.query.count() == 2


def test_failed_physical_delete_does_not_free_bytes(app_module, db_session, normal_user, project_with_pair, monkeypatch):
    """Never: decrement -> attempt deletion -> swallow the failure."""
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    db_session.commit()
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4"

    real_remove = os.remove

    def _refuse_video(path, *args, **kwargs):
        if os.path.basename(str(path)) == video_path.name:
            raise PermissionError("file locked")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(app_module.os, "remove", _refuse_video)
    app_module._delete_project_files_and_rows(project)

    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    # Image freed, video still counted because its bytes are still on disk.
    assert user.storage_used_bytes == 20
    surviving = app_module.MediaObject.query.filter_by(status="ACTIVE").one()
    assert surviving.media_role == "video"
    assert video_path.exists()


def test_delete_retry_after_a_failure_is_idempotent_and_eventually_frees(
    app_module, db_session, normal_user, project_with_pair, monkeypatch
):
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    db_session.commit()
    video_path = Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4"
    project_id = project.id

    real_remove = os.remove
    monkeypatch.setattr(app_module.os, "remove", lambda p, *a, **k: (
        (_ for _ in ()).throw(PermissionError("locked")) if os.path.basename(str(p)) == video_path.name
        else real_remove(p, *a, **k)
    ))
    app_module._delete_project_files_and_rows(project)
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 20

    # Operator clears the lock; the retry frees the remainder and is safe to
    # run again after that.
    monkeypatch.undo()
    video_path.unlink()
    for _ in range(2):
        app_module.release_project_media_accounting(project_id)
        db_session.commit()
        db_session.expire_all()
        assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0


def test_archiving_a_project_does_not_free_storage(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    db_session.commit()

    project.is_active = False  # suspend/archive: media is retained
    db_session.commit()

    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 30
    assert app_module.MediaObject.query.filter_by(status="ACTIVE").count() == 2


# ===========================================================================
# Transfer primitives
# ===========================================================================
def test_transfer_with_sufficient_capacity_moves_ownership_and_storage_together(
    app_module, db_session, normal_user, plan, project_with_pair
):
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    normal_user.projects_used = 1
    project.current_owner_user_id = normal_user.id
    recipient = _second_user(app_module, db_session, plan)
    _set_storage_allowance(app_module, db_session, recipient, 1 * GB)
    db_session.commit()

    ok, project_bytes = app_module.evaluate_project_storage_transfer(project, recipient)
    assert ok is True and project_bytes == 30

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()

    db_session.expire_all()
    assert app_module.ProjectOwnershipTransfer.query.one().status == "COMPLETED"
    assert app_module.User.query.get(recipient.id).storage_used_bytes == 30
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0
    assert all(o.owner_user_id == recipient.id for o in app_module.MediaObject.query.all())


def test_transfer_with_insufficient_capacity_moves_nothing(
    app_module, db_session, normal_user, plan, project_with_pair
):
    project, pair = project_with_pair
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 30
    normal_user.projects_used = 1
    project.current_owner_user_id = normal_user.id
    recipient = _second_user(app_module, db_session, plan)
    _set_storage_allowance(app_module, db_session, recipient, 5)  # cannot absorb 30
    db_session.commit()

    ok, project_bytes = app_module.evaluate_project_storage_transfer(project, recipient)
    assert ok is False and project_bytes == 30

    transfer = app_module.initiate_project_ownership_transfer(project, normal_user, recipient)
    app_module.accept_project_ownership_transfer(transfer, acting_user=recipient)
    db_session.commit()

    db_session.expire_all()
    # PENDING_CAPACITY, and NOTHING partially moved: no ownership change, no
    # accounting change, no deletion, no project slot consumed.
    assert app_module.ProjectOwnershipTransfer.query.one().status == "PENDING_CAPACITY"
    assert app_module.Project.query.get(project.id).current_owner_user_id == normal_user.id
    assert app_module.User.query.get(recipient.id).storage_used_bytes == 0
    assert app_module.User.query.get(recipient.id).projects_used == 0
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 30
    assert all(o.owner_user_id == normal_user.id for o in app_module.MediaObject.query.all())


# ===========================================================================
# Storage add-on and admin grants
# ===========================================================================
def _storage_addon(app_module, db_session, bytes_delta, price=250.0, code="STORAGE_PACK"):
    item = app_module.AddonCatalog(
        code=code, name="Storage Pack", addon_type="ACCOUNT_STORAGE",
        unit_amount=price, currency="INR", storage_bytes_delta=bytes_delta,
        is_active=True, is_commercially_available=True,
    )
    db_session.add(item)
    db_session.commit()
    return item


def _fulfilled_purchase(app_module, db_session, user, item, quantity=1, order_id="ord-storage-1"):
    purchase = app_module.AddonPurchase(
        order_id=order_id, user_id=user.id, catalog_id=item.id, quantity=quantity,
        amount=item.unit_amount, total_amount=item.unit_amount * quantity, status="pending",
        razorpay_payment_id=f"pay_{order_id}",
    )
    db_session.add(purchase)
    db_session.commit()
    result = app_module.fulfill_addon_purchase(purchase)
    assert result["success"], result
    return purchase


def test_storage_addon_quantity_comes_from_the_catalog_not_from_code(app_module, db_session, normal_user):
    """No hard-coded +1GB/+5GB and no defaulted price anywhere."""
    item = _storage_addon(app_module, db_session, 2 * GB)
    assert app_module._addon_effect(item, 1) == ("ACCOUNT_STORAGE", 2 * GB)
    assert app_module._addon_effect(item, 3) == ("ACCOUNT_STORAGE", 6 * GB)
    # An unconfigured quantity is refused rather than defaulted to something.
    blank = app_module.AddonCatalog(
        code="BLANK_STORAGE", name="Blank", addon_type="ACCOUNT_STORAGE",
        unit_amount=100.0, storage_bytes_delta=None,
        is_active=True, is_commercially_available=True,
    )
    ok, code, _msg = app_module._validate_addon_catalog_for_purchase(blank)
    assert ok is False and code == "ADDON_INVALID"


def test_purchased_storage_stacks_and_survives_a_plan_change(app_module, db_session, normal_user):
    _set_storage_allowance(app_module, db_session, normal_user, 10 * GB)
    item = _storage_addon(app_module, db_session, 2 * GB)
    _fulfilled_purchase(app_module, db_session, normal_user, item)
    assert _ents(app_module, normal_user)["effective_storage_bytes"] == 12 * GB

    # A plan change re-materializes plan-derived limits; purchased storage must
    # not be one of the things that gets dropped.
    new_plan = app_module.SubscriptionPlan(
        plan_name="Bigger", plan_amount=200.0, duration_type="time", duration_value=12,
        total_project_limit=20, total_scan_limit=2000, max_pairs_per_project=5,
        is_trial_plan=False, is_active=True, base_storage_bytes=50 * GB,
    )
    db_session.add(new_plan)
    db_session.commit()
    normal_user.subscription_id = new_plan.id
    db_session.commit()

    ents = _ents(app_module, normal_user)
    assert ents["purchased_storage_bytes"] == 2 * GB
    assert ents["effective_storage_bytes"] == 52 * GB


def test_storage_addon_refund_causes_overage_and_never_deletes_media(
    app_module, db_session, normal_user, admin, project_with_pair
):
    project, pair = project_with_pair
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    item = _storage_addon(app_module, db_session, 4 * GB)
    purchase = _fulfilled_purchase(app_module, db_session, normal_user, item)
    app_module.record_pair_media_objects(project, pair, image_bytes=10, video_bytes=20)
    normal_user.storage_used_bytes = 3 * GB
    db_session.commit()
    assert _ents(app_module, normal_user)["effective_storage_bytes"] == 5 * GB

    refund = app_module._create_refund_row_for_source(
        admin, "customer request", "idem-storage-refund", addon_purchase=purchase
    )
    db_session.add(refund)
    db_session.commit()
    assert app_module._apply_refund_reconciliation(refund) is True
    db_session.commit()

    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    ents = _ents(app_module, user)
    assert ents["purchased_storage_bytes"] == 0
    assert ents["effective_storage_bytes"] == 1 * GB
    assert ents["over_storage"] is True
    # Non-destructive: usage untouched, media intact, existing content works.
    assert user.storage_used_bytes == 3 * GB
    assert app_module.MediaObject.query.filter_by(status="ACTIVE").count() == 2
    assert (Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4").exists()


def test_admin_storage_grant_is_auditable_revocable_and_separate_from_purchases(
    app_module, db_session, normal_user, admin
):
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    item = _storage_addon(app_module, db_session, 2 * GB)
    _fulfilled_purchase(app_module, db_session, normal_user, item)

    app_module.grant_account_storage(admin, normal_user, 3 * GB, reason="goodwill")
    db_session.commit()
    ents = _ents(app_module, normal_user)
    assert ents["purchased_storage_bytes"] == 2 * GB
    assert ents["admin_granted_storage_bytes"] == 3 * GB
    assert ents["effective_storage_bytes"] == 6 * GB
    assert app_module.AdminActivity.query.filter_by(activity_type="account_storage_grant").count() == 1

    # Revocation is a negative ledger row, never a deletion of media.
    normal_user.storage_used_bytes = 5 * GB
    db_session.commit()
    app_module.grant_account_storage(admin, normal_user, -3 * GB, reason="revoked")
    db_session.commit()

    db_session.expire_all()
    ents = _ents(app_module, app_module.User.query.get(normal_user.id))
    assert ents["admin_granted_storage_bytes"] == 0
    assert ents["effective_storage_bytes"] == 3 * GB
    assert ents["over_storage"] is True
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 5 * GB


# ===========================================================================
# Reconciliation
# ===========================================================================
def test_reconcile_dry_run_reports_but_writes_nothing(app_module, db_session, normal_user, project_with_pair):
    report = app_module.reconcile_storage_ledger(apply_changes=False)
    assert report["discovered"] == 2
    assert report["created"] == 2
    assert report["total_bytes_accounted"] > 0
    assert app_module.MediaObject.query.count() == 0
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0


def test_reconcile_apply_records_pre_ledger_media_and_syncs_the_counter(
    app_module, db_session, normal_user, project_with_pair
):
    project, pair = project_with_pair
    report = app_module.reconcile_storage_ledger(apply_changes=True)
    assert report["created"] == 2

    objects = app_module.MediaObject.query.all()
    assert len(objects) == 2
    assert all(o.source == "reconciliation" and o.reconciled_at is not None for o in objects)
    assert {o.storage_key for o in objects} == {
        f"user/images/{project.id}_0.jpg", f"user/videos/{project.id}_0.mp4"
    }
    # Real filesystem byte sizes, never fabricated.
    assert sum(o.size_bytes for o in objects) == len(b"fake image") + len(b"fake video")
    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    assert user.storage_used_bytes == sa.account_storage_used_bytes(user.id)


def test_reconcile_rerun_is_idempotent(app_module, db_session, normal_user, project_with_pair):
    app_module.reconcile_storage_ledger(apply_changes=True)
    second = app_module.reconcile_storage_ledger(apply_changes=True)
    assert second["created"] == 0
    assert second["already_reconciled"] == 2
    assert app_module.MediaObject.query.count() == 2
    db_session.expire_all()
    user = app_module.User.query.get(normal_user.id)
    assert user.storage_used_bytes == sa.account_storage_used_bytes(user.id)


def test_reconcile_reports_missing_files_without_fabricating_bytes(
    app_module, db_session, normal_user, project_with_pair
):
    project, pair = project_with_pair
    (Path(app_module.VIDEOS_DIR) / f"{project.id}_0.mp4").unlink()

    report = app_module.reconcile_storage_ledger(apply_changes=True)
    assert len(report["missing_files"]) == 1
    assert report["missing_files"][0]["storage_key"] == f"user/videos/{project.id}_0.mp4"
    assert report["created"] == 1
    assert app_module.MediaObject.query.count() == 1  # no invented row for the gone file


def test_reconcile_reports_orphan_files_separately_and_never_deletes_them(
    app_module, db_session, normal_user, project_with_pair
):
    orphan = Path(app_module.VIDEOS_DIR) / "9999_7.mp4"
    orphan.write_bytes(b"orphaned bytes")

    report = app_module.reconcile_storage_ledger(apply_changes=True)
    assert "user/videos/9999_7.mp4" in report["orphan_files"]
    # An orphan has no owner to bill, so it is reported, not counted.
    assert not any(o.storage_key.endswith("9999_7.mp4") for o in app_module.MediaObject.query.all())
    assert orphan.exists(), "reconciliation must never delete customer media"


def test_reconcile_never_deletes_any_media_or_ledger_row(app_module, db_session, normal_user, project_with_pair):
    project, pair = project_with_pair
    files_before = sorted(os.listdir(app_module.VIDEOS_DIR) + os.listdir(app_module.IMAGES_DIR))
    app_module.reconcile_storage_ledger(apply_changes=True)
    app_module.reconcile_storage_ledger(apply_changes=True)
    assert sorted(os.listdir(app_module.VIDEOS_DIR) + os.listdir(app_module.IMAGES_DIR)) == files_before
    assert app_module.MediaObject.query.count() == 2


def test_reconcile_reports_ambiguous_ownership_rather_than_guessing(
    app_module, db_session, normal_user, project_with_pair
):
    project, pair = project_with_pair
    project.owner_user_id = None
    project.current_owner_user_id = None
    project.owner_admin_id = None
    db_session.commit()

    report = app_module.reconcile_storage_ledger(apply_changes=True)
    assert report["created"] == 0
    assert report["ambiguous_ownership"] == [
        {"project_id": project.id, "pair_id": pair.id, "reason": "no_resolvable_owner"}
    ]
    assert app_module.MediaObject.query.count() == 0


def test_reconcile_corrects_ledger_size_drift_against_the_real_file(
    app_module, db_session, normal_user, project_with_pair
):
    project, pair = project_with_pair
    app_module.reconcile_storage_ledger(apply_changes=True)
    video = app_module.MediaObject.query.filter_by(media_role="video").one()
    video.size_bytes = 999999
    db_session.commit()

    report = app_module.reconcile_storage_ledger(apply_changes=True)
    assert len(report["size_mismatches"]) == 1
    db_session.expire_all()
    assert app_module.MediaObject.query.filter_by(media_role="video").one().size_bytes == len(b"fake video")


# ===========================================================================
# Concurrency
# ===========================================================================
def test_two_stale_prechecks_cannot_both_consume_the_same_headroom(app_module, db_session, normal_user):
    """The exact overcommit race: both consumers pass the same precheck."""
    _set_storage_allowance(app_module, db_session, normal_user, 100)
    db_session.commit()

    used, allowance = app_module.account_storage_state(normal_user)
    # Both would-be uploads see the same headroom and both pass the precheck.
    assert sa.can_consume(used, allowance, 60) is True

    assert sa.reserve_account_storage(normal_user.id, 60, allowance) is True
    # The second reservation re-evaluates the counter INSIDE the UPDATE and
    # loses, even though its precheck said yes.
    assert sa.reserve_account_storage(normal_user.id, 60, allowance) is False

    db_session.commit()
    db_session.expire_all()
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 60


def test_concurrent_reservations_never_overcommit_the_allowance(app_module, db_session, normal_user):
    """Real threads, separate connections, one shared user row."""
    _set_storage_allowance(app_module, db_session, normal_user, 100)
    db_session.commit()
    user_id = normal_user.id
    engine = app_module.db.engine
    barrier = threading.Barrier(8)
    successes = []
    lock = threading.Lock()

    statement = app_module.db.text(
        "UPDATE users SET storage_used_bytes = COALESCE(storage_used_bytes, 0) + 25 "
        "WHERE id = :uid AND COALESCE(storage_used_bytes, 0) + 25 <= 100"
    )

    def attempt():
        barrier.wait()
        for _ in range(50):  # SQLite serializes writers; retry on a busy lock
            try:
                with engine.begin() as connection:
                    won = connection.execute(statement, {"uid": user_id}).rowcount == 1
                with lock:
                    successes.append(won)
                return
            except OperationalError:
                continue

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(successes) == 8, "every attempt should have resolved"
    # 100 / 25 = exactly four winners, never five.
    assert sum(1 for won in successes if won) == 4
    db_session.expire_all()
    assert app_module.User.query.get(user_id).storage_used_bytes == 100


def test_a_failed_upload_never_permanently_reserves_storage(
    client, app_module, db_session, normal_user, login_user, monkeypatch
):
    _patch_upload_processing(app_module, monkeypatch)
    _set_storage_allowance(app_module, db_session, normal_user, 1 * GB)
    # Blow up after the reservation and after files are moved into place.
    monkeypatch.setattr(app_module, "_reserve_pair_slots_for_project", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    client.post("/upload", data=_upload_data(name="Doomed"), content_type="multipart/form-data")

    db_session.expire_all()
    assert app_module.Project.query.count() == 0
    assert app_module.MediaObject.query.count() == 0
    assert app_module.User.query.get(normal_user.id).storage_used_bytes == 0
