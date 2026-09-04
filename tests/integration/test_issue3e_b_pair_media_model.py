"""Issue 3E-B: PairMedia ORM-level model, relationship and cascade-delete
behaviour.

Migration-level backfill correctness (Alembic upgrade/downgrade against a
real revision history) is covered separately in
tests/migrations/test_pair_media_migration.py. This file covers what only
the live ORM can prove: ProjectPair.media_items/default_media, the
partial-unique default-enforcement index as db.create_all() builds it, and
that deleting a ProjectPair or a Project cascades to PairMedia with no
orphan rows left behind - exactly like ProjectPair's existing children
(ScanLog, ScanEvent) already do.

Nothing here exercises upload, scanner, or Creator UI - PairMedia is not
wired into any of those yet.
"""
import pytest
from sqlalchemy.exc import IntegrityError


def _add_media(app_module, db_session, pair, **kwargs):
    fields = dict(video_filename="extra.mp4", sort_order=1, is_default=False)
    fields.update(kwargs)
    media = app_module.PairMedia(pair_id=pair.id, **fields)
    db_session.add(media)
    db_session.commit()
    return media


# ===========================================================================
# Relationship / helpers
# ===========================================================================
def test_media_items_relationship_returns_ordered_rows(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    second = _add_media(app_module, db_session, pair, video_filename="second.mp4", sort_order=1)
    first = _add_media(app_module, db_session, pair, video_filename="first.mp4", sort_order=0)
    db_session.expire_all()

    refreshed = app_module.ProjectPair.query.get(pair.id)
    filenames = [m.video_filename for m in refreshed.media_items]
    assert filenames == ["first.mp4", "second.mp4"]
    assert {first.video_filename, second.video_filename} == {"first.mp4", "second.mp4"}


def test_default_media_helper_returns_the_flagged_row(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    _add_media(app_module, db_session, pair, video_filename="not_default.mp4", is_default=False)
    default = _add_media(app_module, db_session, pair, video_filename="is_default.mp4", is_default=True)
    db_session.expire_all()

    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.default_media is not None
    assert refreshed.default_media.id == default.id


def test_default_media_helper_returns_none_when_no_row_is_flagged(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    _add_media(app_module, db_session, pair, video_filename="not_default.mp4", is_default=False)
    db_session.expire_all()

    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.default_media is None


def test_default_media_helper_returns_none_with_no_media_at_all(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    assert pair.default_media is None
    assert pair.media_items == []


# ===========================================================================
# Default-media enforcement (partial unique index, as db.create_all() builds
# it - the test suite's own schema bootstrap, not just the migration).
# ===========================================================================
def test_only_one_default_media_row_allowed_per_pair(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    _add_media(app_module, db_session, pair, video_filename="first_default.mp4", is_default=True)
    with pytest.raises(IntegrityError):
        _add_media(app_module, db_session, pair, video_filename="second_default.mp4", is_default=True)
    db_session.rollback()


def test_multiple_non_default_media_rows_are_allowed_on_the_same_pair(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    _add_media(app_module, db_session, pair, video_filename="one.mp4", is_default=False)
    _add_media(app_module, db_session, pair, video_filename="two.mp4", is_default=False)
    db_session.expire_all()
    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert len(refreshed.media_items) == 2


def test_default_media_uniqueness_is_scoped_per_pair_not_global(app_module, db_session, project_with_pair):
    """Two DIFFERENT pairs may each have their own default row - the
    partial unique index is on pair_id, not a single global default."""
    project, pair_a = project_with_pair
    pair_b = app_module.ProjectPair(
        project_id=project.id, pair_index=1, video_filename="b.mp4",
    )
    db_session.add(pair_b)
    db_session.commit()

    _add_media(app_module, db_session, pair_a, video_filename="a_default.mp4", is_default=True)
    _add_media(app_module, db_session, pair_b, video_filename="b_default.mp4", is_default=True)
    db_session.expire_all()
    assert app_module.ProjectPair.query.get(pair_a.id).default_media.video_filename == "a_default.mp4"
    assert app_module.ProjectPair.query.get(pair_b.id).default_media.video_filename == "b_default.mp4"


# ===========================================================================
# Cascade delete - no orphan PairMedia rows.
# ===========================================================================
def test_deleting_pair_cascades_pair_media(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    media = _add_media(app_module, db_session, pair, video_filename="cascaded.mp4", is_default=True)
    media_id = media.id

    db_session.delete(pair)
    db_session.commit()

    assert app_module.PairMedia.query.get(media_id) is None


def test_deleting_project_cascades_pair_media_through_pair(app_module, db_session, project_with_pair):
    project, pair = project_with_pair
    media = _add_media(app_module, db_session, pair, video_filename="deep_cascaded.mp4", is_default=True)
    media_id = media.id
    pair_id = pair.id

    db_session.delete(project)
    db_session.commit()

    assert app_module.ProjectPair.query.get(pair_id) is None
    assert app_module.PairMedia.query.get(media_id) is None


# ===========================================================================
# Ownership stays inherited, never duplicated onto PairMedia.
# ===========================================================================
def test_pair_media_has_no_owner_columns(app_module):
    columns = {c.name for c in app_module.PairMedia.__table__.columns}
    assert "owner_user_id" not in columns
    assert "owner_admin_id" not in columns


# ===========================================================================
# Legacy runtime is untouched: ProjectPair.video_filename stays authoritative,
# creating PairMedia rows never mutates it.
# ===========================================================================
def test_adding_pair_media_does_not_change_legacy_video_filename(app_module, db_session, project_with_pair):
    _project, pair = project_with_pair
    original_filename = pair.video_filename
    _add_media(app_module, db_session, pair, video_filename="unrelated.mp4", is_default=True)
    db_session.expire_all()
    refreshed = app_module.ProjectPair.query.get(pair.id)
    assert refreshed.video_filename == original_filename
