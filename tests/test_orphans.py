"""Tests for orphaned demo student detection.

An orphaned demo student is one whose course offering no longer exists in
D2L. Detection must never mistake a course the service account cannot see
for a deleted one, so these tests pin down the 403 and error behavior as
firmly as the 404 behavior.
"""

import pytest

from demostudent.config import AppConfig
from demostudent.d2lApi import D2lApiError
from demostudent.db import MetricsDb
from demostudent.orphans import (OrphanScanAborted, deleteOrphanedDemoStudents,
                                 findOrphanedDemoStudents)


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


def markCompleted(db, orgUnitIds, runId=1):
    for orgUnitId in orgUnitIds:
        db.markCourseCompleted(
            orgUnitId=orgUnitId, courseName=f"Course {orgUnitId}",
            courseCode=f"C{orgUnitId}", userName=f"demo.{orgUnitId}",
            userId=9000 + orgUnitId, runId=runId,
        )


def test_deletedCourseIsReportedAsOrphaned(config, db):
    markCompleted(db, [300, 301, 302])
    api = FakeApi(liveCourseIds=[300, 301], deletedCourseIds={302})

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert [orphan.orgUnitId for orphan in report.orphans] == [302]
    assert report.orphans[0].userName == "demo.302"
    assert report.candidatesChecked == 1


def test_liveCourseIsNeverOrphaned(config, db):
    markCompleted(db, [300, 301])
    api = FakeApi(liveCourseIds=[300, 301])

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert report.orphans == []
    assert api.orgUnitLookups == []  # nothing to confirm; discovery saw them both


def test_courseMissingFromDiscoveryButStillAliveIsNotOrphaned(config, db):
    """Discovery can omit a course the account cannot reach; a 200 clears it."""
    markCompleted(db, [300, 301])
    api = FakeApi(liveCourseIds=[300])  # 301 absent from discovery but not deleted

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert report.orphans == []
    assert api.orgUnitLookups == [301]  # it was confirmed rather than assumed
    assert report.candidatesChecked == 1


def test_forbiddenCourseIsSkippedNotDeleted(config, db):
    """A 403 means the account cannot see the course, not that it is gone."""
    markCompleted(db, [300, 301])
    api = FakeApi(liveCourseIds=[300], forbiddenCourseIds={301})

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert report.orphans == []
    assert report.unreachable == [301]


def test_unexpectedApiErrorIsSkippedNotDeleted(config, db):
    """A 500 is not proof of anything; the course is left alone."""
    class FlakyApi(FakeApi):
        def getOrgUnit(self, orgUnitId):
            self.orgUnitLookups.append(orgUnitId)
            raise D2lApiError("Server error", httpStatus=500)

    markCompleted(db, [300, 301])
    api = FlakyApi(liveCourseIds=[300])

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert report.orphans == []
    assert report.unreachable == [301]


def test_orphanCarriesTheStoredUserDetails(config, db):
    markCompleted(db, [302])
    api = FakeApi(liveCourseIds=[], deletedCourseIds={302})

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    orphan = report.orphans[0]
    assert orphan.orgUnitId == 302
    assert orphan.userName == "demo.302"
    assert orphan.userId == 9302
    assert orphan.courseName == "Course 302"
    assert orphan.courseCode == "C302"


def test_legacyRowWithoutUserDetailsIsResolvedFromD2l(config, db):
    """import-legacy rows carry only an org unit id; the user is looked up live."""
    db.importCompletedOrgUnits([302])
    api = FakeApi(liveCourseIds=[], deletedCourseIds={302})

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    orphan = report.orphans[0]
    assert orphan.userName == "demo.302"
    assert orphan.userId == 9302


def test_legacyRowWhoseUserNoLongerExistsIsNotAnOrphan(config, db):
    """Nothing to delete if the demo student is already gone from D2L."""
    class NoUserApi(FakeApi):
        def getUserByUsername(self, userName):
            return None

    db.importCompletedOrgUnits([302])
    api = NoUserApi(liveCourseIds=[], deletedCourseIds={302})

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert report.orphans == []
    assert report.alreadyGone == [302]


def test_scanAbortsWhenCandidatesExceedTheCap(config, db):
    """A permissions gap looks like mass deletion; refuse rather than report it."""
    markCompleted(db, range(300, 400))
    api = FakeApi(liveCourseIds=[300])  # 99 of 100 missing from discovery

    with pytest.raises(OrphanScanAborted) as excInfo:
        findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert "99" in str(excInfo.value)
    assert api.orgUnitLookups == []  # aborted before any confirmation call


def test_capCanBeOverridden(config, db):
    markCompleted(db, range(300, 400))
    api = FakeApi(liveCourseIds=[300], deletedCourseIds=set(range(301, 400)))

    report = findOrphanedDemoStudents(api, config, db, maxCandidateRatio=1.0,
                                      logFunc=lambda _line: None)

    assert len(report.orphans) == 99


def test_emptyCompletedCoursesScansNothing(config, db):
    api = FakeApi(liveCourseIds=[300, 301])

    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert report.orphans == []
    assert report.candidatesChecked == 0


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


def test_deleteRemovesTheOrphanedUser(config, db):
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    outcome = deleteOrphanedDemoStudents(api, db, report, hardDelete=True,
                                         logFunc=lambda _line: None)

    assert api.deletedUserIds == [9302]
    assert outcome.deleted == 1
    assert outcome.failed == 0


def test_deleteClearsTheStaleCompletedCourseRow(config, db):
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    deleteOrphanedDemoStudents(api, db, report, hardDelete=True,
                               logFunc=lambda _line: None)

    assert db.completedOrgUnitIds() == {300}


def test_deleteFailureLeavesTheDatabaseRowInPlace(config, db):
    """A user that could not be deleted must stay on the books for a retry."""
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302},
                          failingUserIds={9302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    outcome = deleteOrphanedDemoStudents(api, db, report, hardDelete=True,
                                         logFunc=lambda _line: None)

    assert outcome.deleted == 0
    assert outcome.failed == 1
    assert db.completedOrgUnitIds() == {300, 302}  # row kept for the retry


def test_deleteContinuesPastAFailure(config, db):
    markCompleted(db, [300, 302, 303])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302, 303},
                          failingUserIds={9302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    outcome = deleteOrphanedDemoStudents(api, db, report, hardDelete=True,
                                         logFunc=lambda _line: None)

    assert outcome.deleted == 1
    assert outcome.failed == 1
    assert api.deletedUserIds == [9303]
    assert db.completedOrgUnitIds() == {300, 302}  # 303 cleared, 302 kept


def test_deleteWithNoOrphansDoesNothing(config, db):
    markCompleted(db, [300])
    api = DeletingFakeApi(liveCourseIds=[300])
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    outcome = deleteOrphanedDemoStudents(api, db, report, hardDelete=True,
                                         logFunc=lambda _line: None)

    assert outcome.deleted == 0
    assert api.deletedUserIds == []


def test_deleteRecordsFailuresAsErrors(config, db):
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302},
                          failingUserIds={9302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    deleteOrphanedDemoStudents(api, db, report, runId=7, hardDelete=True,
                               logFunc=lambda _line: None)

    errors = db.recentErrors(limit=5)
    assert len(errors) == 1
    assert errors[0]["phase"] == "deleteOrphan"
    assert errors[0]["orgUnitId"] == 302
    assert errors[0]["httpStatus"] == 403


def test_defaultDeactivatesRatherThanDeletes(config, db):
    """Deletion is effectively irreversible, so it is never the default."""
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    outcome = deleteOrphanedDemoStudents(api, db, report, logFunc=lambda _line: None)

    assert api.deactivatedUserIds == [9302]
    assert api.deletedUserIds == []
    assert outcome.deleted == 1


def test_deactivationClearsTheStaleRowToo(config, db):
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    deleteOrphanedDemoStudents(api, db, report, logFunc=lambda _line: None)

    assert db.completedOrgUnitIds() == {300}


def test_failedDeactivationKeepsTheRow(config, db):
    markCompleted(db, [300, 302])
    api = DeletingFakeApi(liveCourseIds=[300], deletedCourseIds={302},
                          failingUserIds={9302})
    report = findOrphanedDemoStudents(api, config, db, logFunc=lambda _line: None)

    outcome = deleteOrphanedDemoStudents(api, db, report, logFunc=lambda _line: None)

    assert outcome.failed == 1
    assert db.completedOrgUnitIds() == {300, 302}
