"""
Copy the local SQLite crm.db into the configured Neon/Postgres database.

Usage:
  1. Put DATABASE_URL=... in crm/.env
  2. Run: python migrate_sqlite_to_postgres.py

Run this against an empty Neon database unless you intentionally want duplicate
rows to fail on primary/unique keys.
"""
import os
import sqlite3
import sys

import core


TABLES = [
    "users",
    "leads",
    "events",
    "settings",
    "ai_runs",
    "ai_run_items",
    "ai_run_log",
    "google_accounts",
]


def sqlite_columns(con, table):
    return [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def reset_identity(pg, table):
    row = pg.execute("SELECT MAX(id) max_id FROM " + table).fetchone()
    max_id = row["max_id"] if row else None
    if max_id is not None:
        pg.execute("SELECT setval(pg_get_serial_sequence(?, 'id'), ?)", (table, max_id))


def main():
    if not core.USING_POSTGRES:
        raise SystemExit("Set DATABASE_URL or NEON_DATABASE_URL in crm/.env before running this script.")

    sqlite_path = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else core.DB_PATH)
    if not os.path.exists(sqlite_path):
        raise SystemExit(f"SQLite database not found: {sqlite_path}")

    core.init_db()
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dest = core.connect()
    try:
        for table in TABLES:
            cols = sqlite_columns(src, table)
            if not cols:
                continue
            placeholders = ",".join("?" for _ in cols)
            col_sql = ",".join(cols)
            rows = src.execute(f"SELECT {col_sql} FROM {table}").fetchall()
            for row in rows:
                dest.execute(
                    f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
                    [row[c] for c in cols],
                )
            if "id" in cols:
                reset_identity(dest, table)
            print(f"{table}: copied {len(rows)} row(s)")
        dest.commit()
    except Exception:
        dest.rollback()
        raise
    finally:
        src.close()
        dest.close()


if __name__ == "__main__":
    main()
