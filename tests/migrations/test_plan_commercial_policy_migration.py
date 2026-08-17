"""Migration tests for the Wave 2 plan commercial policy foundation.

Verifies the new revision is a clean linear child of the Wave 1 head, that it
upgrades and downgrades safely, that byte-valued columns are genuinely 64-bit,
and that every default backfills existing rows in a way that PRESERVES current
behaviour rather than inventing commercial values.
"""
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from flask import Flask
from flask_migrate import Migrate
from flask_migrate import downgrade as migrate_downgrade
from flask_migrate import upgrade as migrate_upgrade
from sqlalchemy import BigInteger, inspect, text

from models import db as shared_db


MIGRATIONS_DIR = str(Path(__file__).resolve().parents[2] / "migrations")
WAVE1_HEAD = "d4e8b2c6a0f3"
WAVE2_REVISION = "e7a3f9c2b1d5"

PLAN_COLUMNS = {
    "plan_family",
    "lifecycle_status",
    "plan_revision",
    "max_image_bytes",
    "max_video_bytes",
    "max_video_duration_seconds",
    "max_image_dimension_px",
    "max_image_pixels",
    "base_storage_bytes",
    "allow_direct_qr",
    "allow_detect_once",
    "allow_tracked_overlay",
}
USER_COLUMNS = {"pending_plan_id", "pending_plan_effective_at"}
ORDER_COLUMNS = {"plan_policy_snapshot_json", "is_deferred_plan_change"}
BYTE_COLUMNS = {"max_image_bytes", "max_video_bytes", "max_image_pixels", "base_storage_bytes"}


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


def test_wave2_revision_is_the_single_new_head_on_top_of_wave1():
    # WAVE2_REVISION is not the current Alembic head -- later waves (Wave 4's
    # a9d3c7e1b502 and onward) legitimately extend the chain past it. This no
    # longer asserts WAVE2_REVISION is the head -- only that it stays exactly
    # once in the graph, directly on top of Wave 1, on the single linear path
    # that leads to whatever the current head actually is.
    script = _script_directory()
    revision = script.get_revision(WAVE2_REVISION)
    assert revision.down_revision == WAVE1_HEAD
    assert len(script.get_heads()) == 1

    current_head = script.get_current_head()
    ancestry = [r.revision for r in script.iterate_revisions(current_head, "base")]
    assert ancestry.count(WAVE2_REVISION) == 1
    assert WAVE2_REVISION in ancestry


def test_wave1_revisions_are_untouched_ancestors():
    script = _script_directory()
    ancestry = {r.revision for r in script.iterate_revisions(script.get_current_head(), "base")}
    assert {"c3f7a1d5e9b4", "d4e8b2c6a0f3"} <= ancestry


def test_upgrade_from_wave1_head_adds_every_column(tmp_path):
    app = _migration_app(tmp_path, "wave2_upgrade")
    with app.app_context():
        migrate_upgrade(revision=WAVE1_HEAD)
        inspector = inspect(shared_db.engine)
        assert not (PLAN_COLUMNS & {c["name"] for c in inspector.get_columns("subscription_plans")})

        migrate_upgrade(revision=WAVE2_REVISION)
        inspector = inspect(shared_db.engine)
        assert PLAN_COLUMNS <= {c["name"] for c in inspector.get_columns("subscription_plans")}
        assert USER_COLUMNS <= {c["name"] for c in inspector.get_columns("users")}
        assert ORDER_COLUMNS <= {c["name"] for c in inspector.get_columns("payment_orders")}


def test_byte_valued_columns_are_64_bit(tmp_path):
    """Integer caps at ~2.1GB - a real risk flagged by the Wave 1 audit."""
    app = _migration_app(tmp_path, "wave2_bigint")
    with app.app_context():
        migrate_upgrade(revision=WAVE2_REVISION)
        inspector = inspect(shared_db.engine)
        types = {c["name"]: c["type"] for c in inspector.get_columns("subscription_plans")}
        for column in BYTE_COLUMNS:
            assert isinstance(types[column], BigInteger), f"{column} is not 64-bit"


def test_existing_plan_rows_backfill_to_behaviour_preserving_defaults(tmp_path):
    """A pre-Wave-2 plan must come out the other side behaving identically."""
    app = _migration_app(tmp_path, "wave2_backfill")
    with app.app_context():
        migrate_upgrade(revision=WAVE1_HEAD)
        shared_db.session.execute(
            text(
                "INSERT INTO subscription_plans "
                "(plan_name, plan_amount, total_project_limit, total_scan_limit, max_pairs_per_project) "
                "VALUES ('Legacy', 99.0, 3, 300, 4)"
            )
        )
        shared_db.session.commit()

        migrate_upgrade(revision=WAVE2_REVISION)
        row = shared_db.session.execute(
            text(
                "SELECT plan_family, lifecycle_status, plan_revision, max_image_bytes, "
                "max_video_bytes, base_storage_bytes, allow_direct_qr, allow_detect_once, "
                "allow_tracked_overlay, total_project_limit, total_scan_limit, max_pairs_per_project "
                "FROM subscription_plans WHERE plan_name = 'Legacy'"
            )
        ).one()

    (family, lifecycle, revision, img_bytes, vid_bytes, storage,
     direct, detect, tracked, projects, scans, pairs) = row

    # Safe inferred family; still sellable; version marker starts at 1.
    assert family == "INDIVIDUAL"
    assert lifecycle == "ACTIVE"
    assert revision == 1
    # No invented commercial numbers: media/storage policy stays unstated, so
    # the immutable server ceiling remains the only effective limit (today's rule).
    assert img_bytes is None and vid_bytes is None and storage is None
    # Every experience stays available - defaulting these off would retroactively
    # revoke capability from live accounts.
    assert bool(direct) and bool(detect) and bool(tracked)
    # Pre-existing commercial limits are untouched.
    assert (projects, scans, pairs) == (3, 300, 4)


def test_indexes_are_created(tmp_path):
    app = _migration_app(tmp_path, "wave2_indexes")
    with app.app_context():
        migrate_upgrade(revision=WAVE2_REVISION)
        inspector = inspect(shared_db.engine)
        names = {i["name"] for i in inspector.get_indexes("subscription_plans")}
    assert {"ix_subscription_plans_plan_family", "ix_subscription_plans_lifecycle_status"} <= names


def test_downgrade_removes_every_added_column(tmp_path):
    app = _migration_app(tmp_path, "wave2_downgrade")
    with app.app_context():
        migrate_upgrade(revision=WAVE2_REVISION)
        migrate_downgrade(revision=WAVE1_HEAD)
        inspector = inspect(shared_db.engine)
        assert not (PLAN_COLUMNS & {c["name"] for c in inspector.get_columns("subscription_plans")})
        assert not (USER_COLUMNS & {c["name"] for c in inspector.get_columns("users")})
        assert not (ORDER_COLUMNS & {c["name"] for c in inspector.get_columns("payment_orders")})


def test_upgrade_is_rerunnable_after_downgrade(tmp_path):
    app = _migration_app(tmp_path, "wave2_roundtrip")
    with app.app_context():
        migrate_upgrade(revision=WAVE2_REVISION)
        migrate_downgrade(revision=WAVE1_HEAD)
        migrate_upgrade(revision=WAVE2_REVISION)
        inspector = inspect(shared_db.engine)
        assert PLAN_COLUMNS <= {c["name"] for c in inspector.get_columns("subscription_plans")}
