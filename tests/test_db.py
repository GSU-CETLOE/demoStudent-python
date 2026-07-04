"""Tests for the SQLite metrics and completed-courses store."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from demostudent.db import MetricsDb
from tests.helpers import backdateAllRows


@pytest.fixture
def db(tmp_path):
    metricsDb = MetricsDb(tmp_path / "working" / "demoStudent.db")
    yield metricsDb
    metricsDb.close()


def test_createsParentDirectoryAndSchema(tmp_path):
    dbPath = tmp_path / "working" / "demoStudent.db"
    metricsDb = MetricsDb(dbPath)
    assert dbPath.is_file()
    metricsDb.close()


def test_completedCoursesRoundTrip(db):
    assert db.completedOrgUnitIds() == set()
    runId = db.startRun()
    db.markCourseCompleted(
        orgUnitId=111, courseName="Biology 101", courseCode="BIO101",
        userName="demo.111", userId=9001, runId=runId,
    )
    assert db.completedOrgUnitIds() == {111}


def test_markCourseCompletedIsIdempotent(db):
    runId = db.startRun()
    for _ in range(2):
        db.markCourseCompleted(
            orgUnitId=222, courseName="Chem", courseCode="CHM",
            userName="demo.222", userId=1, runId=runId,
        )
    assert db.completedOrgUnitIds() == {222}


def test_runLifecycle(db):
    runId = db.startRun()
    db.finishRun(
        runId, status="completed", coursesDiscovered=10, coursesSkipped=4,
        coursesChecked=6, usersCreated=2, enrollmentsCreated=3, errorCount=1,
    )
    runs = db.recentRuns(limit=5)
    assert len(runs) == 1
    run = runs[0]
    assert run["runId"] == runId
    assert run["status"] == "completed"
    assert run["coursesDiscovered"] == 10
    assert run["coursesSkipped"] == 4
    assert run["coursesChecked"] == 6
    assert run["usersCreated"] == 2
    assert run["enrollmentsCreated"] == 3
    assert run["errorCount"] == 1
    assert run["startedAt"] is not None
    assert run["finishedAt"] is not None


def test_recentRunsNewestFirst(db):
    firstRunId = db.startRun()
    secondRunId = db.startRun()
    runs = db.recentRuns(limit=5)
    assert [run["runId"] for run in runs] == [secondRunId, firstRunId]


def test_recordAndReadErrors(db):
    runId = db.startRun()
    db.recordError(runId, orgUnitId=333, phase="enroll", httpStatus=403, message="Forbidden")
    db.recordError(runId, orgUnitId=None, phase="discovery", httpStatus=None, message="boom")
    errors = db.recentErrors(limit=10)
    assert len(errors) == 2
    assert errors[0]["phase"] == "discovery"  # newest first
    assert errors[1]["orgUnitId"] == 333
    assert errors[1]["httpStatus"] == 403
    assert errors[1]["message"] == "Forbidden"
    assert errors[1]["runId"] == runId


def test_importCompletedOrgUnits(db):
    added = db.importCompletedOrgUnits([101, 102, 103])
    assert added == 3
    assert db.completedOrgUnitIds() == {101, 102, 103}
    # Re-importing overlapping ids only adds the new ones
    added = db.importCompletedOrgUnits([103, 104])
    assert added == 1
    assert db.completedOrgUnitIds() == {101, 102, 103, 104}


def test_pruneRemovesActivityOlderThanAYear(tmp_path):
    dbPath = tmp_path / "demoStudent.db"
    db = MetricsDb(dbPath)
    try:
        oldRunId = db.startRun()
        db.markCourseCompleted(orgUnitId=111, courseName="Old", courseCode="OLD",
                               userName="demo.111", userId=1, runId=oldRunId)
        db.recordError(oldRunId, orgUnitId=111, phase="course", httpStatus=500, message="old")
        backdateAllRows(dbPath, days=400)

        recentRunId = db.startRun()
        db.markCourseCompleted(orgUnitId=222, courseName="New", courseCode="NEW",
                               userName="demo.222", userId=2, runId=recentRunId)
        db.recordError(recentRunId, orgUnitId=222, phase="course", httpStatus=500, message="new")

        removed = db.pruneOldActivity()
        assert removed == 3  # one run, one course, one error

        assert db.completedOrgUnitIds() == {222}
        assert [run["runId"] for run in db.recentRuns(limit=10)] == [recentRunId]
        errors = db.recentErrors(limit=10)
        assert len(errors) == 1
        assert errors[0]["message"] == "new"
    finally:
        db.close()


def test_pruneKeepsEverythingWithinAYear(db):
    runId = db.startRun()
    db.markCourseCompleted(orgUnitId=111, courseName="C", courseCode="C",
                           userName="demo.111", userId=1, runId=runId)
    assert db.pruneOldActivity() == 0
    assert db.completedOrgUnitIds() == {111}


def test_concurrentWritesFromThreads(db):
    runId = db.startRun()

    def markCompleted(orgUnitId):
        db.markCourseCompleted(
            orgUnitId=orgUnitId, courseName=f"Course {orgUnitId}", courseCode="C",
            userName=f"demo.{orgUnitId}", userId=orgUnitId, runId=runId,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(markCompleted, range(1000, 1100)))

    assert db.completedOrgUnitIds() == set(range(1000, 1100))
