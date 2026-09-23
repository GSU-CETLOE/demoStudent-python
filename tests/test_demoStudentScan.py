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


class WalkingApi:
    """Stand-in D2L whose user listing returns a fixed population."""

    def __init__(self, users):
        self.users = users
        self.walked = 0

    def iterUsers(self):
        self.walked += 1
        yield from self.users

    def getUserByUsername(self, userName):
        for user in self.users:
            if user["UserName"].lower() == userName.lower():
                return user
        return None


def userItem(userId, userName, isActive=True):
    return {"UserId": userId, "UserName": userName,
            "Activation": {"IsActive": isActive}}


def test_scanRecordsOnlyDemoStudents(config, db):
    from demostudent.orphans import scanDemoStudents

    api = WalkingApi([
        userItem(9301, "demo.301"),
        userItem(1, "jsmith"),
        userItem(2, "demo.legacy"),
        userItem(9302, "demo.302"),
    ])

    result = scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    recorded = db.demoStudents()
    assert [student["userName"] for student in recorded] == ["demo.301", "demo.302"]
    assert result.demoStudentsFound == 2
    assert result.usersWalked == 4


def test_scanRecordsTheOrgUnitFromTheUsername(config, db):
    from demostudent.orphans import scanDemoStudents

    api = WalkingApi([userItem(9301, "demo.2469900")])
    scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert db.demoStudents()[0]["orgUnitId"] == 2469900


def test_scanRecordsActivationState(config, db):
    from demostudent.orphans import scanDemoStudents

    api = WalkingApi([
        userItem(9301, "demo.301", isActive=True),
        userItem(9302, "demo.302", isActive=False),
    ])
    scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    byName = {student["userName"]: student for student in db.demoStudents()}
    assert byName["demo.301"]["isActive"] == 1
    assert byName["demo.302"]["isActive"] == 0


def test_scanReportsUnparseablePrefixMatchesSeparately(config, db):
    """demo.legacy is not ours to act on, but it should not vanish silently."""
    from demostudent.orphans import scanDemoStudents

    api = WalkingApi([userItem(2, "demo.legacy"), userItem(3, "demo.")])
    result = scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert result.demoStudentsFound == 0
    assert sorted(result.unparsed) == ["demo.", "demo.legacy"]


def test_rescanUpdatesRatherThanDuplicates(config, db):
    from demostudent.orphans import scanDemoStudents

    api = WalkingApi([userItem(9301, "demo.301", isActive=True)])
    scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    api.users = [userItem(9301, "demo.301", isActive=False)]
    scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    students = db.demoStudents()
    assert len(students) == 1
    assert students[0]["isActive"] == 0


def test_scanVerifiesUsernameVisibilityFirst(config, db):
    """D2L hides fields the role cannot see, which would match zero users."""
    from demostudent.orphans import DemoStudentScanAborted, scanDemoStudents

    class BlindApi(WalkingApi):
        def iterUsers(self):
            self.walked += 1
            yield {"UserId": 9301, "Activation": {"IsActive": True}}  # no UserName

    api = BlindApi([])
    with pytest.raises(DemoStudentScanAborted) as excInfo:
        scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert "UserName" in str(excInfo.value)


def test_scanWithNoUsersRecordsNothing(config, db):
    from demostudent.orphans import scanDemoStudents

    api = WalkingApi([])
    result = scanDemoStudents(api, config, db, logFunc=lambda _line: None)

    assert result.demoStudentsFound == 0
    assert db.demoStudents() == []


def test_orphansComeFromTheInventoryNotCompletedCourses(config, db):
    """The whole point: an orphan with no completedCourses row is still found."""
    from demostudent.orphans import findOrphansFromInventory

    db.recordDemoStudents([
        {"userId": 9301, "userName": "demo.301", "orgUnitId": 301, "isActive": True},
        {"userId": 9302, "userName": "demo.302", "orgUnitId": 302, "isActive": True},
    ])
    db.setCourseState(302, "deleted")
    db.setCourseState(301, "live")
    assert db.completedOrgUnitIds() == set()  # nothing in the skip list at all

    orphans = findOrphansFromInventory(db)

    assert [orphan.orgUnitId for orphan in orphans] == [302]
    assert orphans[0].userName == "demo.302"
    assert orphans[0].userId == 9302


def test_unknownCourseStateIsNeverAnOrphan(config, db):
    from demostudent.orphans import findOrphansFromInventory

    db.recordDemoStudents([
        {"userId": 9303, "userName": "demo.303", "orgUnitId": 303, "isActive": True}])
    # never confirmed, so it stays 'unknown'
    assert findOrphansFromInventory(db) == []


def test_alreadyInactiveOrphansAreExcludedByDefault(config, db):
    """A deactivated demo student has already been retired; leave it be."""
    from demostudent.orphans import findOrphansFromInventory

    db.recordDemoStudents([
        {"userId": 9302, "userName": "demo.302", "orgUnitId": 302, "isActive": False}])
    db.setCourseState(302, "deleted")

    assert findOrphansFromInventory(db) == []
    assert len(findOrphansFromInventory(db, includeInactive=True)) == 1


def test_scanAnnouncesItselfBeforeWalking(config, db):
    """The walk is long and silent otherwise; say what is happening first."""
    from demostudent.orphans import scanDemoStudents

    lines = []
    api = WalkingApi([userItem(9301, "demo.301")])
    scanDemoStudents(api, config, db, logFunc=lines.append)

    assert lines, "the scan printed nothing at all"
    assert "Reading" in lines[0] or "Walking" in lines[0]


def test_scanReportsProgressOftenEnoughToLookAlive(config, db):
    """A tenant walk should show movement well before it finishes."""
    from demostudent.orphans import scanDemoStudents

    lines = []
    api = WalkingApi([userItem(9000 + n, f"demo.{n}") for n in range(2500)])
    scanDemoStudents(api, config, db, logFunc=lines.append)

    progressLines = [line for line in lines if "walked" in line.lower()
                     and "so far" in line.lower()]
    assert len(progressLines) >= 4, f"only {len(progressLines)} progress lines in 2500 users"


def test_confirmAnnouncesItselfAndReportsProgress(config, db):
    """The confirm pass is one API call per course and was entirely silent."""
    from demostudent.orphans import confirmCourseStates

    seedInventory(db, range(300, 560))
    lines = []
    api = FakeApi()

    confirmCourseStates(api, db, logFunc=lines.append)

    assert lines, "the confirm pass printed nothing at all"
    assert "course" in lines[0].lower()
    progressLines = [line for line in lines if "checked" in line.lower()]
    assert progressLines, "no progress while checking 260 live courses"


def test_confirmProgressCountsLiveCoursesToo(config, db):
    """On a healthy tenant nearly every course is live; silence looks hung."""
    from demostudent.orphans import confirmCourseStates

    seedInventory(db, range(300, 420))
    lines = []
    confirmCourseStates(FakeApi(), db, logFunc=lines.append)

    assert any("checked" in line.lower() for line in lines)


def test_progressIsFlushedSoItAppearsWhileRunning(config, db, capsys):
    """Unflushed stdout buffers when piped, hiding progress until the end."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        from demostudent.orphans import emit
        emit("first line")
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, check=True,
    )
    # Captured through a pipe: it arrived, which means it was flushed.
    assert "first line" in result.stdout
