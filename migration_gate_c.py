import argparse
import json
import os
import sys


PRODUCTION_MARKERS = ("postgres://", "postgresql://", "mysql://", "mssql://")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _configure_database_url(database_url):
    if not database_url:
        raise RuntimeError("Gate C migration requires explicit --database-url or DATABASE_URL")
    lowered = database_url.lower()
    if lowered.startswith(PRODUCTION_MARKERS) and os.environ.get("SCANSTORY_ALLOW_GATE_C_PRODUCTION") != "1":
        raise RuntimeError("Refusing production-like database URL without SCANSTORY_ALLOW_GATE_C_PRODUCTION=1")
    os.environ["SCANSTORY_TESTING"] = "1"
    os.environ["TEST_DATABASE_URL"] = database_url


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gate C additive compatibility migration")
    parser.add_argument("command", choices=["status", "dry-run", "apply", "verify"])
    parser.add_argument("--database-url", default=os.environ.get("GATE_C_DATABASE_URL"))
    args = parser.parse_args(argv)

    try:
        _configure_database_url(args.database_url)
        import app as app_module
        from gate_c_migration import run_gate_c_migration, verify_gate_c_migration

        with app_module.app.app_context():
            app_module.db.create_all()
            if args.command == "status":
                payload = verify_gate_c_migration()
            elif args.command == "dry-run":
                payload = run_gate_c_migration(dry_run=True).__dict__
            elif args.command == "apply":
                payload = run_gate_c_migration(dry_run=False).__dict__
            else:
                payload = verify_gate_c_migration()
            print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Gate C migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
