"""SQLite store for completed courses, demo students, run metrics, and errors.

A future dashboard reads this database, so the schema is treated as a
public contract. All writes are serialized behind a lock so worker
threads can share one MetricsDb instance safely.

Note the split between completedCourses and demoStudents. The first is a
skip list answering "has this course been handled recently", and it is
pruned on the retention clock, since a stale answer there only costs a
re-check. The second inventories real accounts in D2L and is never
pruned. Ageing it out would lose track of the old demo students most
likely to be orphaned.
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Rows older than this are deleted at the start of every run.
ACTIVITY_RETENTION_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS completedCourses (
    orgUnitId   INTEGER PRIMARY KEY,
    courseName  TEXT,
    courseCode  TEXT,
    userName    TEXT,
    userId      INTEGER,
    runId       INTEGER,
    completedAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    runId              INTEGER PRIMARY KEY AUTOINCREMENT,
    startedAt          TEXT NOT NULL,
    finishedAt         TEXT,
    status             TEXT NOT NULL,
    coursesDiscovered  INTEGER NOT NULL DEFAULT 0,
    coursesSkipped     INTEGER NOT NULL DEFAULT 0,
    coursesChecked     INTEGER NOT NULL DEFAULT 0,
    usersCreated       INTEGER NOT NULL DEFAULT 0,
    enrollmentsCreated INTEGER NOT NULL DEFAULT 0,
    errorCount         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS demoStudents (
    userId      INTEGER PRIMARY KEY,
    userName    TEXT NOT NULL,
    orgUnitId   INTEGER,
    isActive    INTEGER,
    lastSeenAt  TEXT NOT NULL,
    courseState TEXT NOT NULL DEFAULT 'unknown',
    checkedAt   TEXT
);
CREATE INDEX IF NOT EXISTS demoStudentsByOrgUnit
    ON demoStudents (orgUnitId);
CREATE TABLE IF NOT EXISTS errors (
    errorId    INTEGER PRIMARY KEY AUTOINCREMENT,
    runId      INTEGER,
    orgUnitId  INTEGER,
    phase      TEXT NOT NULL,
    httpStatus INTEGER,
    message    TEXT NOT NULL,
    occurredAt TEXT NOT NULL
);
"""


def utcNow() -> str:
    """Current UTC time in ISO-8601, the timestamp format for all tables."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MetricsDb:
    """Thread-safe access to the application's SQLite database."""

    def __init__(self, dbPath: Path | str):
        dbPath = Path(dbPath)
        dbPath.parent.mkdir(parents=True, exist_ok=True)
        self.dbPath = dbPath
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(dbPath, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    def completedOrgUnitIds(self) -> set[int]:
        """All org unit ids already processed; used to skip courses up front."""
        with self._lock:
            rows = self._connection.execute("SELECT orgUnitId FROM completedCourses").fetchall()
        return {row["orgUnitId"] for row in rows}

    def completedCourses(self) -> list[dict]:
        """Every completed course row, with the demo student recorded for it.

        Rows seeded by import-legacy carry only an org unit id, so
        userName and userId can be None.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT orgUnitId, courseName, courseCode, userName, userId"
                " FROM completedCourses ORDER BY orgUnitId"
            ).fetchall()
        return [dict(row) for row in rows]

    def markCourseCompleted(
        self,
        orgUnitId: int,
        courseName: str | None,
        courseCode: str | None,
        userName: str | None,
        userId: int | None,
        runId: int | None,
    ) -> None:
        """Record that a course has its demo student, so it is never re-checked."""
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO completedCourses"
                " (orgUnitId, courseName, courseCode, userName, userId, runId, completedAt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (orgUnitId, courseName, courseCode, userName, userId, runId, utcNow()),
            )
            self._connection.commit()

    def clearCompleted(self, orgUnitIds: list[int]) -> int:
        """Drop the given org units from completedCourses so they get rechecked.

        Returns the number of rows actually removed.
        """
        if not orgUnitIds:
            return 0
        with self._lock:
            cursor = self._connection.executemany(
                "DELETE FROM completedCourses WHERE orgUnitId = ?",
                [(orgUnitId,) for orgUnitId in orgUnitIds],
            )
            self._connection.commit()
        return cursor.rowcount

    def importCompletedOrgUnits(self, orgUnitIds: list[int]) -> int:
        """Seed completedCourses from a plain org unit id list (legacy migration).

        Returns the number of ids newly added.
        """
        now = utcNow()
        with self._lock:
            before = self._connection.execute("SELECT COUNT(*) FROM completedCourses").fetchone()[0]
            self._connection.executemany(
                "INSERT OR IGNORE INTO completedCourses (orgUnitId, completedAt) VALUES (?, ?)",
                [(orgUnitId, now) for orgUnitId in orgUnitIds],
            )
            self._connection.commit()
            after = self._connection.execute("SELECT COUNT(*) FROM completedCourses").fetchone()[0]
        return after - before

    def recordDemoStudents(self, students: list[dict]) -> int:
        """Add or refresh demo student rows from a scan of D2L.

        Keyed on userId, so re-scanning updates a user in place instead
        of duplicating it. courseState and checkedAt are left alone: a
        scan reports what exists in D2L, and whether the course is still
        there is a separate question.

        Returns the number of rows newly added.
        """
        if not students:
            return 0
        now = utcNow()
        with self._lock:
            before = self._connection.execute("SELECT COUNT(*) FROM demoStudents").fetchone()[0]
            self._connection.executemany(
                "INSERT INTO demoStudents"
                " (userId, userName, orgUnitId, isActive, lastSeenAt)"
                " VALUES (:userId, :userName, :orgUnitId, :isActive, :lastSeenAt)"
                " ON CONFLICT(userId) DO UPDATE SET"
                " userName = excluded.userName, orgUnitId = excluded.orgUnitId,"
                " isActive = excluded.isActive, lastSeenAt = excluded.lastSeenAt",
                [
                    {
                        "userId": student["userId"],
                        "userName": student["userName"],
                        "orgUnitId": student.get("orgUnitId"),
                        "isActive": student.get("isActive"),
                        "lastSeenAt": now,
                    }
                    for student in students
                ],
            )
            self._connection.commit()
            after = self._connection.execute("SELECT COUNT(*) FROM demoStudents").fetchone()[0]
        return after - before

    def demoStudents(self, courseState: str | None = None) -> list[dict]:
        """Known demo students, optionally only those in one course state."""
        query = "SELECT * FROM demoStudents"
        params: tuple = ()
        if courseState is not None:
            query += " WHERE courseState = ?"
            params = (courseState,)
        query += " ORDER BY orgUnitId"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def setCourseState(self, orgUnitId: int, courseState: str) -> int:
        """Record what a course lookup concluded about one org unit.

        courseState is 'live', 'deleted', or 'unknown'. Updates by org
        unit rather than by user, since more than one demo student could
        point at the same course.
        """
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE demoStudents SET courseState = ?, checkedAt = ? WHERE orgUnitId = ?",
                (courseState, utcNow(), orgUnitId),
            )
            self._connection.commit()
        return cursor.rowcount

    def forgetDemoStudent(self, userId: int) -> int:
        """Drop a demo student row, once the account is gone from D2L."""
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM demoStudents WHERE userId = ?", (userId,))
            self._connection.commit()
        return cursor.rowcount

    def startRun(self) -> int:
        """Create a run row in 'running' state and return its id."""
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO runs (startedAt, status) VALUES (?, 'running')", (utcNow(),)
            )
            self._connection.commit()
        runId = cursor.lastrowid
        assert runId is not None
        return runId

    def finishRun(
        self,
        runId: int,
        status: str,
        coursesDiscovered: int,
        coursesSkipped: int,
        coursesChecked: int,
        usersCreated: int,
        enrollmentsCreated: int,
        errorCount: int,
    ) -> str:
        """Finalize a run row with its outcome counts; returns the finish timestamp."""
        finishedAt = utcNow()
        with self._lock:
            self._connection.execute(
                "UPDATE runs SET finishedAt = ?, status = ?, coursesDiscovered = ?,"
                " coursesSkipped = ?, coursesChecked = ?, usersCreated = ?,"
                " enrollmentsCreated = ?, errorCount = ? WHERE runId = ?",
                (
                    finishedAt, status, coursesDiscovered, coursesSkipped,
                    coursesChecked, usersCreated, enrollmentsCreated, errorCount, runId,
                ),
            )
            self._connection.commit()
        return finishedAt

    def recordError(
        self,
        runId: int | None,
        orgUnitId: int | None,
        phase: str,
        httpStatus: int | None,
        message: str,
    ) -> None:
        """Store an error for later review by the status command or a dashboard."""
        with self._lock:
            self._connection.execute(
                "INSERT INTO errors (runId, orgUnitId, phase, httpStatus, message, occurredAt)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (runId, orgUnitId, phase, httpStatus, message, utcNow()),
            )
            self._connection.commit()

    def recentRuns(self, limit: int = 10) -> list[dict]:
        """Most recent runs, newest first."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs ORDER BY runId DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def pruneOldActivity(self, retentionDays: int = ACTIVITY_RETENTION_DAYS) -> int:
        """Delete activity older than the retention window.

        Covers errors, runs, and completedCourses. demoStudents is
        deliberately exempt, since it tracks accounts that exist in D2L
        and the oldest of those are the likeliest orphans.

        Timestamps are uniform ISO-8601 UTC strings, so lexicographic
        comparison is chronological. Returns the number of rows removed.
        A pruned course that is somehow rediscovered later is simply
        re-checked and re-recorded; nothing is enrolled twice.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retentionDays)).isoformat(
            timespec="seconds")
        with self._lock:
            removed = 0
            removed += self._connection.execute(
                "DELETE FROM errors WHERE occurredAt < ?", (cutoff,)).rowcount
            removed += self._connection.execute(
                "DELETE FROM runs WHERE startedAt < ?", (cutoff,)).rowcount
            removed += self._connection.execute(
                "DELETE FROM completedCourses WHERE completedAt < ?", (cutoff,)).rowcount
            self._connection.commit()
        return removed

    def recentErrors(self, limit: int = 20) -> list[dict]:
        """Most recent errors, newest first."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM errors ORDER BY errorId DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]
