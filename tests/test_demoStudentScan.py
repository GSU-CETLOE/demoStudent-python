"""Tests for recognizing demo student accounts by username."""

import pytest

from demostudent.config import AppConfig
from demostudent.orphans import parseDemoStudentUsername


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
    )


@pytest.mark.parametrize("userName, expected", [
    ("demo.2469900", 2469900),
    ("demo.1", 1),
    ("demo.0000301", 301),      # leading zeros still parse to the same course
])
def test_validDemoUsernameYieldsItsOrgUnitId(config, userName, expected):
    assert parseDemoStudentUsername(userName, config) == expected


@pytest.mark.parametrize("userName", [
    "jsmith",                   # an ordinary account
    "demoinstructor",           # prefix-like but no separator
    "demo",                     # bare prefix
    "demo.",                    # separator, no id
    "demo.legacy",              # non-numeric tail
    "demo.301.extra",           # more than one segment
    "demo.-301",                # negative is not an org unit id
    "demo.3 01",                # whitespace inside
    "xdemo.301",                # prefix not at the start
    "",
])
def test_nonDemoUsernameYieldsNothing(config, userName):
    assert parseDemoStudentUsername(userName, config) is None


def test_matchingIsCaseInsensitiveOnThePrefix(config):
    """D2L usernames are not case sensitive, so neither is the match."""
    assert parseDemoStudentUsername("DEMO.301", config) == 301
    assert parseDemoStudentUsername("Demo.301", config) == 301


def test_prefixComesFromConfig(config):
    from dataclasses import replace
    renamed = replace(config, usernamePrefix="testuser")
    assert parseDemoStudentUsername("testuser.301", renamed) == 301
    assert parseDemoStudentUsername("demo.301", renamed) is None


def test_usernameTheToolWouldGenerateAlwaysParses(config):
    """Whatever demoUsername builds must be recognized by the parser."""
    for orgUnitId in (1, 301, 2469900, 999999999):
        assert parseDemoStudentUsername(config.demoUsername(orgUnitId), config) == orgUnitId


class FakeApi:
    """Org structure where some courses are deleted (404) or hidden (403)."""

    def __init__(self, deletedCourseIds=(), forbiddenCourseIds=()):
        self.deletedCourseIds = set(deletedCourseIds)
        self.forbiddenCourseIds = set(forbiddenCourseIds)
        self.orgUnitLookups = []

    def getOrgUnit(self, orgUnitId):
        from demostudent.d2lApi import D2lApiError
        self.orgUnitLookups.append(orgUnitId)
        if orgUnitId in self.deletedCourseIds:
            raise D2lApiError("Resource Not Found", httpStatus=404)
        if orgUnitId in self.forbiddenCourseIds:
            raise D2lApiError("Not Authorized", httpStatus=403)
        return {"Identifier": str(orgUnitId), "Type": {"Id": 3}}


@pytest.fixture
def db(config):
    from demostudent.db import MetricsDb
    metricsDb = MetricsDb(config.dbPath)
    yield metricsDb
    metricsDb.close()


def seedInventory(db, orgUnitIds):
    db.recordDemoStudents([
        {"userId": 9000 + orgUnitId, "userName": f"demo.{orgUnitId}",
         "orgUnitId": orgUnitId, "isActive": True}
        for orgUnitId in orgUnitIds
    ])


def test_confirmMarksDeletedCoursesOnly(config, db):
    from demostudent.orphans import confirmCourseStates

    seedInventory(db, [301, 302, 303])
    api = FakeApi(deletedCourseIds={302})

    confirmCourseStates(api, db, logFunc=lambda _line: None)

    byOrgUnit = {student["orgUnitId"]: student for student in db.demoStudents()}
    assert byOrgUnit[301]["courseState"] == "live"
    assert byOrgUnit[302]["courseState"] == "deleted"
    assert byOrgUnit[303]["courseState"] == "live"


def test_confirmLeavesHiddenCoursesUnknown(config, db):
    """A 403 is not proof of deletion, so the state stays unknown."""
    from demostudent.orphans import confirmCourseStates

    seedInventory(db, [301, 302])
    api = FakeApi(forbiddenCourseIds={302})

    confirmCourseStates(api, db, logFunc=lambda _line: None)

    byOrgUnit = {student["orgUnitId"]: student for student in db.demoStudents()}
    assert byOrgUnit[302]["courseState"] == "unknown"
    assert db.demoStudents(courseState="deleted") == []


def test_confirmLeavesUnexpectedErrorsUnknown(config, db):
    from demostudent.orphans import confirmCourseStates

    class ExplodingApi(FakeApi):
        def getOrgUnit(self, orgUnitId):
            self.orgUnitLookups.append(orgUnitId)
            raise TimeoutError("connection timed out")

    seedInventory(db, [301])
    api = ExplodingApi()

    confirmCourseStates(api, db, logFunc=lambda _line: None)

    assert db.demoStudents()[0]["courseState"] == "unknown"


def test_confirmChecksEachCourseOnce(config, db):
    """Several demo students could point at one course; look it up once."""
    from demostudent.orphans import confirmCourseStates

    db.recordDemoStudents([
        {"userId": 9301, "userName": "demo.301", "orgUnitId": 301, "isActive": True},
        {"userId": 9999, "userName": "demo.301", "orgUnitId": 301, "isActive": False},
    ])
    api = FakeApi()

    confirmCourseStates(api, db, logFunc=lambda _line: None)

    assert api.orgUnitLookups == [301]


def test_confirmReportsCounts(config, db):
    from demostudent.orphans import confirmCourseStates

    seedInventory(db, [301, 302, 303])
    api = FakeApi(deletedCourseIds={302}, forbiddenCourseIds={303})

    result = confirmCourseStates(api, db, logFunc=lambda _line: None)

    assert result.live == 1
    assert result.deleted == 1
    assert result.unknown == 1
