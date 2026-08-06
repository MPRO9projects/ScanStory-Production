"""Dry-run-first SQLite to PostgreSQL rehearsal importer.

This is development tooling only. It never creates schema, never generates
migrations, never copies media files, and never prints password-bearing URLs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models import db  # noqa: E402


EXCLUDED_TABLES = {
    "alembic_version",
}
POLICY_REVIEW_TABLES = {
    "otp_codes": "security-sensitive; migrate only for a controlled rehearsal requiring auth-state continuity",
    "user_login_activity": "audit/history; optional for rehearsal evidence",
    "admin_activity": "audit/history; optional for rehearsal evidence",
}


def safe_url_label(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname or '<local>'}/{(parsed.path or '/').lstrip('/') or '<unknown>'}"


def current_heads() -> set[str]:
    versions = ROOT / "migrations" / "versions"
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for path in versions.glob("*.py"):
        text_value = path.read_text(encoding="utf-8", errors="ignore")
        rev = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", text_value, re.MULTILINE)
        down = re.search(r"^down_revision\s*=\s*([^#\n]+)", text_value, re.MULTILINE)
        if rev:
            revisions.add(rev.group(1))
        if down:
            raw = down.group(1).strip()
            for item in re.findall(r"['\"]([^'\"]+)['\"]", raw):
                down_revisions.add(item)
    return revisions - down_revisions


def alembic_version(engine: Engine) -> set[str]:
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return set()
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text("select version_num from alembic_version")).fetchall()}


def validate_urls(sqlite_source: str, postgres_dest: str) -> None:
    src = urlparse(sqlite_source)
    dst = urlparse(postgres_dest)
    if not src.scheme.startswith("sqlite"):
        raise SystemExit("Source must be an explicit sqlite:/// URL.")
    if not dst.scheme.startswith("postgresql"):
        raise SystemExit("Destination must be an explicit postgresql:// or postgresql+driver:// URL.")
    if sqlite_source == postgres_dest:
        raise SystemExit("Source and destination must differ.")


def table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def destination_row_count(engine: Engine, tables: Iterable) -> int:
    total = 0
    existing = table_names(engine)
    with engine.connect() as conn:
        for table in tables:
            if table.name in EXCLUDED_TABLES or table.name not in existing:
                continue
            total += int(conn.execute(select(text("count(*)")).select_from(table)).scalar() or 0)
    return total


def count_rows(engine: Engine, table) -> int:
    if table.name not in table_names(engine):
        return 0
    with engine.connect() as conn:
        return int(conn.execute(select(text("count(*)")).select_from(table)).scalar() or 0)


def reset_sequences(conn, tables: Iterable) -> list[str]:
    reset = []
    for table in tables:
        if "id" not in table.c:
            continue
        conn.execute(text(
            "select setval(pg_get_serial_sequence(:table_name, 'id'), "
            "coalesce((select max(id) from " + table.name + "), 1), "
            "coalesce((select max(id) from " + table.name + "), 0) > 0)"
        ), {"table_name": table.name})
        reset.append(table.name)
    return reset


def run(args: argparse.Namespace) -> int:
    validate_urls(args.sqlite_source, args.postgres_dest)
    source = create_engine(args.sqlite_source)
    dest = create_engine(args.postgres_dest)
    metadata = db.metadata
    ordered_tables = [table for table in metadata.sorted_tables if table.name not in EXCLUDED_TABLES]

    heads = current_heads()
    dest_version = alembic_version(dest)
    if dest_version != heads:
        raise SystemExit(
            "Destination schema is not at expected Alembic head. "
            f"expected={sorted(heads)} current={sorted(dest_version)}"
        )

    existing_rows = destination_row_count(dest, ordered_tables)
    if existing_rows and not args.allow_non_empty_destination:
        raise SystemExit("Destination is not empty. Re-run with --allow-non-empty-destination only for a controlled rehearsal.")

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "source": safe_url_label(args.sqlite_source),
        "destination": safe_url_label(args.postgres_dest),
        "tables": {},
        "policy_review_tables": POLICY_REVIEW_TABLES,
        "media_files_copied": False,
        "sequence_resets": [],
    }

    source_existing = table_names(source)
    dest_existing = table_names(dest)
    with source.connect() as source_conn:
        if not args.apply:
            for table in ordered_tables:
                if table.name not in source_existing:
                    report["tables"][table.name] = {"status": "missing_source", "rows": 0}
                elif table.name not in dest_existing:
                    report["tables"][table.name] = {"status": "missing_destination", "rows": count_rows(source, table)}
                else:
                    report["tables"][table.name] = {"status": "planned", "rows": count_rows(source, table)}
        else:
            with dest.begin() as dest_conn:
                for table in ordered_tables:
                    if table.name not in source_existing:
                        report["tables"][table.name] = {"status": "missing_source", "rows": 0}
                        continue
                    if table.name not in dest_existing:
                        report["tables"][table.name] = {"status": "missing_destination", "rows": count_rows(source, table)}
                        continue
                    rows = [dict(row._mapping) for row in source_conn.execute(select(table))]
                    if rows:
                        dest_conn.execute(table.insert(), rows)
                    report["tables"][table.name] = {"status": "copied", "rows": len(rows)}
                report["sequence_resets"] = reset_sequences(dest_conn, ordered_tables)

    output = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        Path(args.report).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rehearse SQLite to PostgreSQL data import safely.")
    parser.add_argument("--sqlite-source", required=True, help="Explicit sqlite:/// source URL.")
    parser.add_argument("--postgres-dest", required=True, help="Explicit PostgreSQL destination URL.")
    parser.add_argument("--apply", action="store_true", help="Actually copy rows. Default is dry-run.")
    parser.add_argument("--allow-non-empty-destination", action="store_true", help="Allow importing into a non-empty destination.")
    parser.add_argument("--report", help="Optional path for JSON reconciliation report.")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
