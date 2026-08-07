#!/usr/bin/env python3
"""Apply clickhouse/migrations/*.sql in filename order, once each.

One statement per file, every one idempotent — `IF NOT EXISTS` on creates,
`MODIFY`/`REPLACE` on alters — so a crash between executing a statement and
recording it self-heals on the next run.

Usage:  python scripts/apply_clickhouse_migrations.py [migrations_dir]
Exit:   0 applied or already current, 1 on failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "clickhouse" / "migrations"

_SCHEMA_MIGRATIONS = """
CREATE TABLE IF NOT EXISTS schema_migrations
(
    version    String,
    applied_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY version
"""


def connect() -> Client:
    settings = get_settings()
    return clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
    )


def apply(client: Client, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Returns the versions applied by this run."""
    client.command(_SCHEMA_MIGRATIONS)
    applied = {row[0] for row in client.query("SELECT version FROM schema_migrations").result_rows}

    fresh = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in applied:
            continue
        client.command(path.read_text())
        client.insert("schema_migrations", [[path.name]], column_names=["version"])
        fresh.append(path.name)
    return fresh


def main() -> int:
    migrations_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else MIGRATIONS_DIR
    try:
        client = connect()
        applied = apply(client, migrations_dir)
    except Exception as exc:
        print(f"clickhouse migrations failed: {exc}", file=sys.stderr)
        return 1

    for version in applied:
        print(f"applied {version}")
    print(f"clickhouse migrations: OK ({len(applied)} applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
