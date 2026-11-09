#!/usr/bin/env python3
"""Validate the generated EPG SQLite database before it is published."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

EXPECTED_COLUMNS = {
    "channels": ("id", "name", "logo"),
    "programs": (
        "channel_id",
        "title",
        "desc",
        "start_time",
        "stop_time",
        "start_utc",
        "stop_utc",
    ),
}


def validate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise SystemExit(f"EPG database is missing or empty: {path}")

    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise SystemExit(f"SQLite integrity check failed: {check}")

        for table, expected in EXPECTED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = tuple(row[1] for row in rows)
            if actual != expected:
                raise SystemExit(
                    f"Unexpected {table} schema: expected {expected}, got {actual}"
                )

        channels = connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        programmes = connection.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        matched = connection.execute(
            """SELECT COUNT(*) FROM programs p
               WHERE EXISTS (SELECT 1 FROM channels c WHERE c.id = p.channel_id)"""
        ).fetchone()[0]
        valid_times = connection.execute(
            """SELECT COUNT(*) FROM programs
               WHERE start_utc IS NOT NULL AND stop_utc IS NOT NULL
                 AND start_utc < stop_utc"""
        ).fetchone()[0]

        if channels <= 0 or programmes <= 0:
            raise SystemExit(
                f"Refusing to publish empty EPG data: channels={channels}, "
                f"programmes={programmes}"
            )
        if matched <= 0 or valid_times <= 0:
            raise SystemExit(
                f"EPG data has no usable schedule rows: matched={matched}, "
                f"valid_times={valid_times}"
            )

        print(
            f"Validated {path}: channels={channels}, programmes={programmes}, "
            f"matched_programmes={matched}, valid_time_programmes={valid_times}"
        )
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: validate_db.py PATH_TO_SQLITE_DATABASE")
    validate(Path(sys.argv[1]))
