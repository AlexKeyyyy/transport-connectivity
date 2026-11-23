"""Database migration runner for PostgreSQL/PostGIS."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo


def get_connection_string() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "transport_connectivity")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    return make_conninfo(host=host, port=port, dbname=db, user=user, password=password)


def run_migrations(migrations_dir: Path | None = None) -> list[str]:
    """Apply pending SQL migration files in order, each in its own transaction."""
    if migrations_dir is None:
        migrations_dir = Path(__file__).parent / "migrations"

    conn_str = get_connection_string()
    applied_versions: list[str] = []

    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Ensure schema_migrations table exists
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )

            # Get already applied versions
            cur.execute("SELECT version FROM schema_migrations;")
            applied = {row[0] for row in cur.fetchall()}

        # Find migration files
        sql_files = sorted(migrations_dir.glob("*.sql"))
        for file_path in sql_files:
            version_name = file_path.name
            if version_name in applied:
                continue

            sql_content = file_path.read_text(encoding="utf-8")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql_content)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s);",
                        (version_name,),
                    )
            applied_versions.append(version_name)

    return applied_versions
