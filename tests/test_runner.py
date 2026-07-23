"""Tests for the multithreaded run orchestrator."""

import threading

import pytest

from demostudent.config import AppConfig
from demostudent.db import MetricsDb
from demostudent.d2lApi import D2lApiError
from demostudent.runner import runEnrollment


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
        maxWorkers=4,
    )


@pytest.fixture
def db(config):
    metricsDb = MetricsDb(config.dbPath)
    yield metricsDb
    metricsDb.close()


def courseItem(identifier):
    return {"Identifier": str(identifier), "Name": f"Course {identifier}", "Code": f"C{identifier}"}


class FakeApi:
    """In-memory D2L: org structure plus user/enrollment state, thread-safe."""

    def __init__(self, courseIds, failingCourseIds=()):
        self.courseIds = list(courseIds)
        self.failingCourseIds = set(failingCourseIds)
        self.users = {}
        self.enrollments = set()
        self._lock = threading.Lock()
        self._nextUserId = 9000
        self.threadNames = set()

    def getChildOrgUnits(self, orgUnitId, ouTypeId):
        return [courseItem(201)]

    def iterDescendants(self, orgUnitId, ouTypeId):
        yield from (courseItem(courseId) for courseId in self.courseIds)

    def getOrgUnit(self, orgUnitId):
        raise NotImplementedError

    def getUserByUsername(self, userName):
        self.threadNames.add(threading.current_thread().name)
        with self._lock:
            return self.users.get(userName)

    def getEnrollment(self, orgUnitId, userId):
        with self._lock:
            if (orgUnitId, userId) in self.enrollments:
                return {"OrgUnitId": orgUnitId, "UserId": userId, "RoleId": 783}
            return None

    def createUser(self, userName, firstName, lastName, externalEmail, roleId):
        orgUnitId = int(userName.split(".")[1])
        if orgUnitId in self.failingCourseIds:
            raise D2lApiError("Forbidden", httpStatus=403)
        with self._lock:
            self._nextUserId += 1
            user = {"UserId": self._nextUserId, "UserName": userName}
            self.users[userName] = user
            return user

    def enrollUser(self, orgUnitId, userId, roleId):
        with self._lock:
            self.enrollments.add((orgUnitId, userId))
            return {"OrgUnitId": orgUnitId, "UserId": userId, "RoleId": roleId}


def test_runProcessesAllCoursesAndRecordsMetrics(config, db):
    api = FakeApi(courseIds=range(300, 320))
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)

    assert summary.status == "completed"
    assert summary.coursesDiscovered == 20
    assert summary.coursesSkipped == 0
    assert summary.coursesChecked == 20
    assert summary.usersCreated == 20
    assert summary.enrollmentsCreated == 20
    assert summary.errorCount == 0
    assert db.completedOrgUnitIds() == set(range(300, 320))

    runs = db.recentRuns(limit=1)
    assert runs[0]["status"] == "completed"
    assert runs[0]["usersCreated"] == 20


def test_completedCoursesAreSkipped(config, db):
    db.importCompletedOrgUnits([300, 301, 302])
    api = FakeApi(courseIds=range(300, 310))
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert summary.coursesSkipped == 3
    assert summary.coursesChecked == 7
    assert summary.usersCreated == 7


def test_secondRunDoesNothing(config, db):
    api = FakeApi(courseIds=range(300, 310))
    runEnrollment(config, api, db, logFunc=lambda _line: None)
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert summary.coursesSkipped == 10
    assert summary.coursesChecked == 0
    assert summary.usersCreated == 0


def test_recheckReverifiesCompletedCoursesWithoutDuplicating(config, db):
    api = FakeApi(courseIds=range(300, 310))
    runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert db.completedOrgUnitIds() == set(range(300, 310))

    summary = runEnrollment(config, api, db, recheck=True, logFunc=lambda _line: None)
    assert summary.coursesSkipped == 0
    assert summary.coursesChecked == 10
    assert summary.usersCreated == 0  # already enrolled, nothing recreated
    assert db.completedOrgUnitIds() == set(range(300, 310))


def test_recheckRecreatesEnrollmentThatWentMissing(config, db):
    api = FakeApi(courseIds=[300])
    runEnrollment(config, api, db, logFunc=lambda _line: None)
    userName = next(iter(api.users))
    del api.users[userName]  # simulate the demo student having been removed in D2L

    summary = runEnrollment(config, api, db, recheck=True, logFunc=lambda _line: None)
    assert summary.usersCreated == 1
    assert db.completedOrgUnitIds() == {300}


def test_recheckOnlyClearsInScopeCourses(config, db):
    api = FakeApi(courseIds=range(300, 305))
    runEnrollment(config, api, db, logFunc=lambda _line: None)

    class NarrowApi(FakeApi):
        def iterDescendants(self, orgUnitId, ouTypeId):
            yield courseItem(300)

    narrowApi = NarrowApi(courseIds=[300])
    narrowApi.users = api.users
    narrowApi.enrollments = api.enrollments
    summary = runEnrollment(config, narrowApi, db, recheck=True, logFunc=lambda _line: None)
    assert summary.coursesDiscovered == 1
    assert summary.coursesChecked == 1
    assert db.completedOrgUnitIds() == set(range(300, 305))  # others untouched


def test_errorsAreRecordedAndOtherCoursesContinue(config, db):
    api = FakeApi(courseIds=range(300, 310), failingCourseIds={305})
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert summary.status == "completedWithErrors"
    assert summary.errorCount == 1
    assert summary.usersCreated == 9
    assert 305 not in db.completedOrgUnitIds()

    errors = db.recentErrors(limit=5)
    assert len(errors) == 1
    assert errors[0]["orgUnitId"] == 305
    assert errors[0]["httpStatus"] == 403
    assert errors[0]["phase"] == "course"


def test_failedCourseIsRetriedOnNextRun(config, db):
    api = FakeApi(courseIds=range(300, 310), failingCourseIds={305})
    runEnrollment(config, api, db, logFunc=lambda _line: None)
    api.failingCourseIds = set()
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert summary.coursesChecked == 1
    assert summary.usersCreated == 1
    assert db.completedOrgUnitIds() == set(range(300, 310))


def test_dryRunChangesNothingAndMarksNothing(config, db):
    api = FakeApi(courseIds=range(300, 310))
    summary = runEnrollment(config, api, db, dryRun=True, logFunc=lambda _line: None)
    assert summary.status == "dryRun"
    assert summary.coursesChecked == 10
    assert summary.usersCreated == 0
    assert summary.enrollmentsCreated == 0
    assert api.users == {}
    assert db.completedOrgUnitIds() == set()


def test_alreadyEnrolledCourseMarkedCompleteWithoutChanges(config, db):
    api = FakeApi(courseIds=[300])
    api.users["demo.300"] = {"UserId": 42, "UserName": "demo.300"}
    api.enrollments.add((300, 42))
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert summary.usersCreated == 0
    assert summary.enrollmentsCreated == 0
    assert db.completedOrgUnitIds() == {300}


def test_multipleWorkerThreadsAreUsed(config, db):
    api = FakeApi(courseIds=range(300, 340))
    runEnrollment(config, api, db, maxWorkers=4, logFunc=lambda _line: None)
    assert len(api.threadNames) > 1


def test_unexpectedWorkerErrorRecordsExceptionType(config, db):
    class ShapeErrorApi(FakeApi):
        def createUser(self, userName, firstName, lastName, externalEmail, roleId):
            raise KeyError("Role")

    api = ShapeErrorApi(courseIds=[300])
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    assert summary.errorCount == 1
    errors = db.recentErrors(limit=1)
    assert "KeyError" in errors[0]["message"]
    assert "Role" in errors[0]["message"]


def test_keyboardInterruptFinishesRunAsInterrupted(config, db):
    class InterruptingApi(FakeApi):
        def createUser(self, userName, firstName, lastName, externalEmail, roleId):
            orgUnitId = int(userName.split(".")[1])
            if orgUnitId == 305:
                raise KeyboardInterrupt
            return super().createUser(userName, firstName, lastName, externalEmail, roleId)

    api = InterruptingApi(courseIds=range(300, 320))
    summary = runEnrollment(config, api, db, maxWorkers=1, logFunc=lambda _line: None)

    assert summary.status == "interrupted"
    assert summary.coursesChecked < 20  # remaining courses were cancelled
    assert db.recentRuns(limit=1)[0]["status"] == "interrupted"
    # Courses completed before the interrupt stay recorded and are skipped next time
    assert 300 in db.completedOrgUnitIds()
    assert 319 not in db.completedOrgUnitIds()


def test_activityOlderThanAYearIsPrunedAtRunStart(config, db):
    from tests.helpers import backdateAllRows

    staleRunId = db.startRun()
    db.markCourseCompleted(orgUnitId=300, courseName="Stale", courseCode="S",
                           userName="demo.300", userId=1, runId=staleRunId)
    backdateAllRows(config.dbPath, days=400)

    api = FakeApi(courseIds=[300])
    summary = runEnrollment(config, api, db, logFunc=lambda _line: None)
    # The year-old completion record was pruned, so the course was re-checked
    assert summary.coursesSkipped == 0
    assert summary.coursesChecked == 1
    assert db.completedOrgUnitIds() == {300}
    # Only the current run remains
    assert [run["status"] for run in db.recentRuns(limit=10)] == ["completed"]


def test_discoveryFailureRecordsErrorAndFailsRun(config, db):
    class BrokenApi(FakeApi):
        def getChildOrgUnits(self, orgUnitId, ouTypeId):
            raise D2lApiError("Server error", httpStatus=500)

    api = BrokenApi(courseIds=[])
    with pytest.raises(D2lApiError):
        runEnrollment(config, api, db, logFunc=lambda _line: None)

    runs = db.recentRuns(limit=1)
    assert runs[0]["status"] == "failed"
    errors = db.recentErrors(limit=1)
    assert errors[0]["phase"] == "discovery"
