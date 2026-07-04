"""Shared helpers for tests."""

import sqlite3
from datetime import datetime, timedelta, timezone


def backdateAllRows(dbPath, days):
    """Rewrite every timestamp in the database to `days` ago."""
    past = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    connection = sqlite3.connect(dbPath)
    connection.execute("UPDATE completedCourses SET completedAt = ?", (past,))
    connection.execute("UPDATE runs SET startedAt = ?", (past,))
    connection.execute("UPDATE errors SET occurredAt = ?", (past,))
    connection.commit()
    connection.close()
