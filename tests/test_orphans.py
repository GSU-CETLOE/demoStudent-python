"""Tests for retiring orphaned demo students.

Detection lives in test_demoStudentScan.py. These cover what happens
afterwards: deactivating or deleting the accounts, keeping the inventory
honest about which ones were retired, and making sure a failure leaves
the account on the books instead of losing it.
"""

import pytest

from demostudent.config import AppConfig
from demostudent.d2lApi import D2lApiError
from demostudent.db import MetricsDb
from demostudent.orphans import deleteOrphanedDemoStudents, findOrphansFromInventory


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
    return {"Identifier": str(identifier), "Name": f"Course {identifier}",
            "Code": f"C{identifier}"}


class FakeApi:
    """Org structure where some org units are deleted (404) or unreachable (403)."""

    def __init__(self, liveCourseIds, deletedCourseIds=(), forbiddenCourseIds=()):
        self.liveCourseIds = list(liveCourseIds)
        self.deletedCourseIds = set(deletedCourseIds)
        self.forbiddenCourseIds = set(forbiddenCourseIds)
        self.orgUnitLookups = []

    def iterDescendants(self, orgUnitId, ouTypeId):
        yield from (courseItem(courseId) for courseId in self.liveCourseIds)

    def getOrgUnit(self, orgUnitId):
        self.orgUnitLookups.append(orgUnitId)
        if orgUnitId in self.deletedCourseIds:
            raise D2lApiError("Resource Not Found", httpStatus=404)
        if orgUnitId in self.forbiddenCourseIds:
            raise D2lApiError("Not Authorized", httpStatus=403)
        return {"Identifier": str(orgUnitId), "Name": f"Course {orgUnitId}",
                "Code": f"C{orgUnitId}", "Type": {"Id": 3}}

    def getUserByUsername(self, userName):
        return {"UserId": 9000 + int(userName.split(".")[1]), "UserName": userName}


class DeletingFakeApi(FakeApi):
    """FakeApi that also records deletions and deactivations."""

    def __init__(self, *args, failingUserIds=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.deletedUserIds = []
        self.deactivatedUserIds = []
        self.failingUserIds = set(failingUserIds)

    def deleteUser(self, userId):
        if userId in self.failingUserIds:
            raise D2lApiError("Not Authorized", httpStatus=403)
        self.deletedUserIds.append(userId)

    def setUserActivation(self, userId, isActive):
        if userId in self.failingUserIds:
            raise D2lApiError("Not Authorized", httpStatus=403)
        assert isActive is False, "orphan cleanup only ever deactivates"
        self.deactivatedUserIds.append(userId)


def orphanedInventory(db, orgUnitIds):
    """Seed the inventory with demo students whose courses are gone."""
    db.recordDemoStudents([
        {"userId": 9000 + orgUnitId, "userName": f"demo.{orgUnitId}",
         "orgUnitId": orgUnitId, "isActive": True}
        for orgUnitId in orgUnitIds
    ])
    for orgUnitId in orgUnitIds:
        db.setCourseState(orgUnitId, "deleted")
    return findOrphansFromInventory(db)


def test_defaultDeactivatesRatherThanDeletes(config, db):
    """Deletion is effectively irreversible, so it is never the default."""
    orphans = orphanedInventory(db, [302])
    api = DeletingFakeApi(liveCourseIds=[])

    outcome = deleteOrphanedDemoStudents(api, db, orphans, logFunc=lambda _line: None)

    assert api.deactivatedUserIds == [9302]
    assert api.deletedUserIds == []
    assert outcome.deleted == 1


def test_hardDeleteRemovesTheUser(config, db):
    orphans = orphanedInventory(db, [302])
    api = DeletingFakeApi(liveCourseIds=[])

    outcome = deleteOrphanedDemoStudents(api, db, orphans, hardDelete=True,
                                         logFunc=lambda _line: None)

    assert api.deletedUserIds == [9302]
    assert api.deactivatedUserIds == []
    assert outcome.deleted == 1


def test_hardDeleteForgetsTheInventoryRow(config, db):
    """The account no longer exists in D2L, so tracking it serves nothing."""
    orphans = orphanedInventory(db, [302])
    api = DeletingFakeApi(liveCourseIds=[])

    deleteOrphanedDemoStudents(api, db, orphans, hardDelete=True,
                               logFunc=lambda _line: None)

    assert db.demoStudents() == []


def test_deactivationKeepsTheRowButMarksItInactive(config, db):
    """It still exists in D2L, so keep it, but stop offering it every run."""
    orphans = orphanedInventory(db, [302])
    api = DeletingFakeApi(liveCourseIds=[])

    deleteOrphanedDemoStudents(api, db, orphans, logFunc=lambda _line: None)

    students = db.demoStudents()
    assert len(students) == 1
    assert students[0]["isActive"] == 0
    assert findOrphansFromInventory(db) == []


def test_failureLeavesTheOrphanOnTheBooks(config, db):
    """A user that could not be retired must come back on the next run."""
    orphans = orphanedInventory(db, [302])
    api = DeletingFakeApi(liveCourseIds=[], failingUserIds={9302})

    outcome = deleteOrphanedDemoStudents(api, db, orphans, logFunc=lambda _line: None)

    assert outcome.deleted == 0
    assert outcome.failed == 1
    assert len(findOrphansFromInventory(db)) == 1


def test_deleteContinuesPastAFailure(config, db):
    orphans = orphanedInventory(db, [302, 303])
    api = DeletingFakeApi(liveCourseIds=[], failingUserIds={9302})

    outcome = deleteOrphanedDemoStudents(api, db, orphans, logFunc=lambda _line: None)

    assert outcome.deleted == 1
    assert outcome.failed == 1
    assert api.deactivatedUserIds == [9303]
    assert [orphan.orgUnitId for orphan in findOrphansFromInventory(db)] == [302]


def test_deleteWithNoOrphansDoesNothing(config, db):
    api = DeletingFakeApi(liveCourseIds=[])

    outcome = deleteOrphanedDemoStudents(api, db, [], logFunc=lambda _line: None)

    assert outcome.deleted == 0
    assert api.deactivatedUserIds == []


def test_deleteRecordsFailuresAsErrors(config, db):
    orphans = orphanedInventory(db, [302])
    api = DeletingFakeApi(liveCourseIds=[], failingUserIds={9302})

    deleteOrphanedDemoStudents(api, db, orphans, runId=7, logFunc=lambda _line: None)

    errors = db.recentErrors(limit=5)
    assert len(errors) == 1
    assert errors[0]["phase"] == "deleteOrphan"
    assert errors[0]["orgUnitId"] == 302
    assert errors[0]["httpStatus"] == 403


def test_nonApiErrorDuringCleanupIsRecordedAndOthersContinue(config, db):
    """A timeout mid-cleanup must not abandon the remaining orphans."""
    class TimingOutApi(DeletingFakeApi):
        def setUserActivation(self, userId, isActive):
            if userId == 9302:
                raise TimeoutError("connection timed out")
            return super().setUserActivation(userId, isActive)

    orphans = orphanedInventory(db, [302, 303])
    api = TimingOutApi(liveCourseIds=[])

    outcome = deleteOrphanedDemoStudents(api, db, orphans, runId=9,
                                         logFunc=lambda _line: None)

    assert outcome.failed == 1
    assert outcome.deleted == 1
    assert api.deactivatedUserIds == [9303]
    errors = db.recentErrors(limit=5)
    assert "TimeoutError" in errors[0]["message"]
