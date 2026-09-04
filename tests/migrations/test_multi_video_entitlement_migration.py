"""Migration tests for Issue 3E-A: multi-video-per-target plan entitlement
foundation.

Verifies the new revision is a clean linear child of the current head, that
it upgrades and downgrades safely, and - the one behaviour this migration
must get right - that every existing plan row backfills to the feature
being OFF, never silently enabled.
"""
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
PRIOR_HEAD = "a4f2c8d9e1b7"
NEW_REVISION = "6e2ed8acdbcf"

PLAN_COLUMNS = {"allow_multi_video_per_target", "max_videos_per_target"}


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


def test_revision_is_the_single_new_head_on_top_of_prior_head():
    # NEW_REVISION is not asserted to BE the current Alembic head - later
    # work (Issue 3E-B's PairMedia migration and onward) legitimately
    # extends the chain past it, same as the Wave 2 migration test's own
    # precedent. This only asserts it stays exactly once in the graph,
    # directly on top of the prior head, on the single linear path that
    # leads to whatever the current head actually is.
    script = _script_directory()
    revision = script.get_revision(NEW_REVISION)
    assert revision.down_revision == PRIOR_HEAD
    assert len(script.get_heads()) == 1

    current_head = script.get_current_head()
    ancestry = [r.revision for r in script.iterate_revisions(current_head, "base")]
    assert ancestry.count(NEW_REVISION) == 1
    assert NEW_REVISION in ancestry


def test_upgrade_from_prior_head_adds_both_columns(tmp_path):
    app = _migration_app(tmp_path, "issue3ea_upgrade")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        inspector = inspect(shared_db.engine)
        assert not (PLAN_COLUMNS & {c["name"] for c in inspector.get_columns("subscription_plans")})

        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        assert PLAN_COLUMNS <= {c["name"] for c in inspector.get_columns("subscription_plans")}


def test_existing_plan_rows_backfill_with_feature_disabled(tmp_path):
    """The one behaviour that matters most: a pre-existing plan must NOT
    silently gain a brand-new sellable capability for free."""
    app = _migration_app(tmp_path, "issue3ea_backfill")
    with app.app_context():
        migrate_upgrade(revision=PRIOR_HEAD)
        shared_db.session.execute(
            text(
                "INSERT INTO subscription_plans "
                "(plan_name, plan_amount, total_project_limit, total_scan_limit, "
                "max_pairs_per_project, allow_direct_qr, allow_detect_once, allow_tracked_overlay, "
                "plan_revision) "
                "VALUES ('Legacy', 99.0, 3, 300, 4, 1, 1, 1, 1)"
            )
        )
        shared_db.session.commit()

        migrate_upgrade(revision=NEW_REVISION)
        row = shared_db.session.execute(
            text(
                "SELECT allow_multi_video_per_target, max_videos_per_target, "
                "plan_revision, max_pairs_per_project "
                "FROM subscription_plans WHERE plan_name = 'Legacy'"
            )
        ).one()

    allow_multi_video, max_videos, revision, pairs = row
    assert bool(allow_multi_video) is False
    assert max_videos is None
    # The migration itself must never bump plan_revision - only an admin
    # edit through _apply_plan_values() does that.
    assert revision == 1
    # Unrelated pre-existing commercial fields are untouched.
    assert pairs == 4


def test_downgrade_removes_both_columns(tmp_path):
    app = _migration_app(tmp_path, "issue3ea_downgrade")
    with app.app_context():
        migrate_upgrade(revision=NEW_REVISION)
        migrate_downgrade(revision=PRIOR_HEAD)
        inspector = inspect(shared_db.engine)
        assert not (PLAN_COLUMNS & {c["name"] for c in inspector.get_columns("subscription_plans")})


def test_upgrade_is_rerunnable_after_downgrade(tmp_path):
    app = _migration_app(tmp_path, "issue3ea_roundtrip")
    with app.app_context():
        migrate_upgrade(revision=NEW_REVISION)
        migrate_downgrade(revision=PRIOR_HEAD)
        migrate_upgrade(revision=NEW_REVISION)
        inspector = inspect(shared_db.engine)
        assert PLAN_COLUMNS <= {c["name"] for c in inspector.get_columns("subscription_plans")}
