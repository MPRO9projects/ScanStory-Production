"""Migration tests for Issue 3E-B: PairMedia data model + legacy backfill.

Verifies the new revision is a clean linear child of the Issue 3E-A head,
upgrades/downgrades safely, and - the behaviour that matters most - that the
backfill gives every existing video-bearing ProjectPair exactly one correct
PairMedia row without ever touching the legacy ProjectPair columns
themselves, across every ownership/experience-type combination and the
duplicate-filename and no-video edge cases.
"""
from datetime import datetime
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
PRIOR_HEAD = "6e2ed8acdbcf"
NEW_REVISION = "f53b3c212bba"

PAIR_MEDIA_COLUMNS = {
    "id", "pair_id", "video_filename", "original_video_name", "video_size",
    "sort_order", "is_default", "created_at", "updated_at",
}


def _script_directory():
    config = AlembicConfig()
    config.set_main_option("script_location", MIGRATIONS_DIR)
    return ScriptDirectory.from_config(config)


def _migration_app(tmp_path, name):
    app = Flask(name)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.secret_key = "migration-test-only"
    shared_db.init_app(app)
    Migrate(app, shared_db, directory=MIGRATIONS_DIR)
    return app


def _seed_project_and_pair(conn, *, public_key, owner_user_id=None, owner_admin_id=None,
                            experience_type="image_video", pair_index=0, video_filename="v.mp4",
                            original_video_name="Original.mp4", video_size=100):
    conn.execute(
        text(
            "INSERT INTO projects (name, owner_user_id, owner_admin_id, experience_type, "
            "public_key, created_at, updated_at) "
            "VALUES (:name, :owner_user_id, :owner_admin_id, :experience_type, :public_key, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "name": f"Project {public_key}",
            "owner_user_id": owner_user_id,
            "owner_admin_id": owner_admin_id,
            "experience_type": experience_type,
            "public_key": public_key,
        },
    )
    project_id = conn.execute(
        text("SELECT id FROM projects WHERE public_key = :pk"), {"pk": public_key}
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO project_pairs (project_id, pair_index, video_filename, "
            "original_video_name, video_size, created_at, updated_at) "
            "VALUES (:project_id, :pair_index, :video_filename, :original_video_name, "
            ":video_size, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "project_id": project_id,
            "pair_index": pair_index,
            "video_filename": video_filename,
            "original_video_name": original_video_name,
            "video_size": video_size,
        },
    )
    return project_id


def test_revision_is_the_single_new_head_on_top_of_prior_head():
    script = _script_directory()
    revision = script.get_revision(NEW_REVISION)
    assert revision.down_revision == PRIOR_HEAD
    assert len(script.get_heads()) == 1
    assert script.get_current_head() == NEW_REVISION


def test_upgrade_creates_pair_media_table_with_expected_columns(tmp_path):
    app = _migration_app(tmp_path, "pairmedia_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        inspector = inspect(shared_db.engine)
        assert "pair_media" not in inspector.get_table_names()

        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        assert "pair_media" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("pair_media")}
        assert PAIR_MEDIA_COLUMNS <= columns


def test_backfill_covers_every_ownership_and_experience_type_combination(tmp_path):
    """Items 3-8, 12-15: filename/original name/size/sort_order/is_default
    copied correctly, across user-owned, admin-owned, Direct QR and
    image-recognition pairs alike - none of those distinctions live on the
    video columns, so the backfill must treat them identically."""
    app = _migration_app(tmp_path, "pairmedia_backfill_combos")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        _seed_project_and_pair(
            conn, public_key="pk_user_iv", owner_user_id=1, experience_type="image_video",
            video_filename="user_iv.mp4", original_video_name="User IV.mp4", video_size=1000,
        )
        _seed_project_and_pair(
            conn, public_key="pk_admin_iv", owner_admin_id=1, experience_type="image_video",
            video_filename="admin_iv.mp4", original_video_name="Admin IV.mp4", video_size=2000,
        )
        _seed_project_and_pair(
            conn, public_key="pk_user_qr", owner_user_id=1, experience_type="direct_qr",
            video_filename="user_qr.mp4", original_video_name="User QR.mp4", video_size=3000,
        )
        _seed_project_and_pair(
            conn, public_key="pk_admin_qr", owner_admin_id=1, experience_type="direct_qr",
            video_filename="admin_qr.mp4", original_video_name="Admin QR.mp4", video_size=4000,
        )
        shared_db.session.commit()

        migrate_upgrade(revision=NEW_REVISION)

        rows = shared_db.session.execute(
            text(
                "SELECT pp.video_filename AS legacy_filename, pm.video_filename, "
                "pm.original_video_name, pm.video_size, pm.sort_order, pm.is_default "
                "FROM project_pairs pp JOIN pair_media pm ON pm.pair_id = pp.id "
                "JOIN projects p ON p.id = pp.project_id "
                "WHERE p.public_key = :pk"
            ),
            {"pk": "pk_user_iv"},
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.video_filename == row.legacy_filename == "user_iv.mp4"
        assert row.original_video_name == "User IV.mp4"
        assert row.video_size == 1000
        assert row.sort_order == 0
        assert bool(row.is_default) is True

        for pk, expected_filename in (
            ("pk_admin_iv", "admin_iv.mp4"),
            ("pk_user_qr", "user_qr.mp4"),
            ("pk_admin_qr", "admin_qr.mp4"),
        ):
            count = shared_db.session.execute(
                text(
                    "SELECT count(*) FROM pair_media pm "
                    "JOIN project_pairs pp ON pp.id = pm.pair_id "
                    "JOIN projects p ON p.id = pp.project_id "
                    "WHERE p.public_key = :pk AND pm.video_filename = :fn"
                ),
                {"pk": pk, "fn": expected_filename},
            ).scalar_one()
            assert count == 1


def test_pair_without_video_gets_no_media_row(tmp_path):
    """Item 9: video_filename is NOT NULL on project_pairs today, but the
    backfill still guards against an empty string rather than assuming."""
    app = _migration_app(tmp_path, "pairmedia_no_video")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        _seed_project_and_pair(conn, public_key="pk_no_video", video_filename="")
        shared_db.session.commit()

        migrate_upgrade(revision=NEW_REVISION)
        count = shared_db.session.execute(text("SELECT count(*) FROM pair_media")).scalar_one()
        assert count == 0


def test_two_pairs_get_two_independent_rows(tmp_path):
    """Item 10."""
    app = _migration_app(tmp_path, "pairmedia_two_pairs")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        project_id = _seed_project_and_pair(
            conn, public_key="pk_two_pairs", pair_index=0, video_filename="first.mp4",
        )
        conn.execute(
            text(
                "INSERT INTO project_pairs (project_id, pair_index, video_filename, "
                "created_at, updated_at) "
                "VALUES (:project_id, 1, 'second.mp4', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id},
        )
        shared_db.session.commit()

        migrate_upgrade(revision=NEW_REVISION)
        rows = shared_db.session.execute(
            text("SELECT pair_id, video_filename FROM pair_media ORDER BY pair_id")
        ).all()
        assert len(rows) == 2
        assert rows[0].pair_id != rows[1].pair_id
        assert {r.video_filename for r in rows} == {"first.mp4", "second.mp4"}


def test_identical_filename_on_different_pairs_stays_independent(tmp_path):
    """Item 11: same physical file referenced by two pairs must become two
    separate PairMedia rows, keyed by pair_id, never merged/deduplicated."""
    app = _migration_app(tmp_path, "pairmedia_dup_filename")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        _seed_project_and_pair(conn, public_key="pk_dup_a", video_filename="shared.mp4")
        _seed_project_and_pair(conn, public_key="pk_dup_b", video_filename="shared.mp4")
        shared_db.session.commit()

        migrate_upgrade(revision=NEW_REVISION)
        rows = shared_db.session.execute(
            text("SELECT pair_id, video_filename FROM pair_media WHERE video_filename = 'shared.mp4'")
        ).all()
        assert len(rows) == 2
        assert rows[0].pair_id != rows[1].pair_id


def test_legacy_project_pair_video_columns_are_never_modified(tmp_path):
    """Item 18."""
    app = _migration_app(tmp_path, "pairmedia_legacy_untouched")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        _seed_project_and_pair(
            conn, public_key="pk_untouched", video_filename="untouched.mp4",
            original_video_name="Untouched.mp4", video_size=12345,
        )
        shared_db.session.commit()
        before = shared_db.session.execute(
            text(
                "SELECT video_filename, original_video_name, video_size FROM project_pairs "
                "WHERE project_id = (SELECT id FROM projects WHERE public_key = 'pk_untouched')"
            )
        ).one()

        migrate_upgrade(revision=NEW_REVISION)
        after = shared_db.session.execute(
            text(
                "SELECT video_filename, original_video_name, video_size FROM project_pairs "
                "WHERE project_id = (SELECT id FROM projects WHERE public_key = 'pk_untouched')"
            )
        ).one()
        assert before == after


def test_migration_creates_no_media_objects(tmp_path):
    """Item 19: PairMedia is a catalog record over an already-accounted-for
    file, never a second upload/storage charge."""
    app = _migration_app(tmp_path, "pairmedia_no_storage_charge")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        _seed_project_and_pair(conn, public_key="pk_storage_check", video_filename="charged.mp4")
        shared_db.session.commit()

        migrate_upgrade(revision=NEW_REVISION)
        media_object_count = shared_db.session.execute(
            text("SELECT count(*) FROM media_objects")
        ).scalar_one()
        assert media_object_count == 0


def test_downgrade_removes_pair_media_without_altering_project_pairs(tmp_path):
    """Item 20."""
    app = _migration_app(tmp_path, "pairmedia_downgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        conn = shared_db.session.connection()
        _seed_project_and_pair(
            conn, public_key="pk_downgrade", video_filename="survives.mp4",
            original_video_name="Survives.mp4", video_size=777,
        )
        shared_db.session.commit()
        migrate_upgrade(revision=NEW_REVISION)
        assert shared_db.session.execute(text("SELECT count(*) FROM pair_media")).scalar_one() == 1

        migrate_downgrade(revision=PRIOR_HEAD)
        inspector = inspect(shared_db.engine)
        assert "pair_media" not in inspector.get_table_names()

        row = shared_db.session.execute(
            text(
                "SELECT video_filename, original_video_name, video_size FROM project_pairs "
                "WHERE project_id = (SELECT id FROM projects WHERE public_key = 'pk_downgrade')"
            )
        ).one()
        assert row.video_filename == "survives.mp4"
        assert row.original_video_name == "Survives.mp4"
        assert row.video_size == 777


def test_upgrade_is_rerunnable_after_downgrade(tmp_path):
    app = _migration_app(tmp_path, "pairmedia_roundtrip")
    with app.app_context():
        migrate_upgrade(revision=NEW_REVISION)
        migrate_downgrade(revision=PRIOR_HEAD)
        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        assert "pair_media" in inspector.get_table_names()
