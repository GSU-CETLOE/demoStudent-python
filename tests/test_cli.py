"""Tests for the command-line interface, wired end-to-end with mocked HTTP."""

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from demostudent.auth import AUTHORIZATION_URL, TOKEN_URL, TokenSet, TokenStore
from demostudent.cli import main

BASE = "https://example.brightspace.com"

VERSIONS_RESPONSE = [
    {"ProductCode": "lp", "LatestVersion": "1.47", "SupportedVersions": ["1.47"]},
    {"ProductCode": "le", "LatestVersion": "1.79", "SupportedVersions": ["1.79"]},
]


@pytest.fixture
def configPath(tmp_path):
    path = tmp_path / "config.toml"
    # workingDir is a single-quoted TOML literal string: in a basic string the
    # backslashes of a Windows path are escape sequences and fail to parse.
    path.write_text(
        f"""
host = "example.brightspace.com"
clientId = "client-id"
clientSecret = "client-secret"
redirectUri = "https://localhost:8443/callback"
workingDir = '{tmp_path}/working'
maxWorkers = 4
"""
    )
    return path


@pytest.fixture
def storedTokens(tmp_path):
    store = TokenStore(tmp_path / "working" / "token" / "token.json")
    store.save(TokenSet(accessToken="valid-token", refreshToken="r",
                        expiresAt=time.time() + 3600))
    return store


def mockDiscovery(courseIds):
    respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE)
    )
    respx.get(f"{BASE}/d2l/api/lp/1.47/orgstructure/1070555/descendants/paged/").mock(
        return_value=httpx.Response(
            200,
            json={
                "PagingInfo": {"Bookmark": "end", "HasMoreItems": False},
                "Items": [
                    {"Identifier": str(courseId), "Name": f"Course {courseId}",
                     "Code": f"C{courseId}"}
                    for courseId in courseIds
                ],
            },
        )
    )


def mockCourseWork():
    respx.get(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\?userName=.*").mock(
        return_value=httpx.Response(404)
    )
    userIds = iter(range(9001, 9999))

    def createUser(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={"UserId": next(userIds),
                                         "UserName": body["UserName"]})

    respx.post(f"{BASE}/d2l/api/lp/1.47/users/").mock(side_effect=createUser)
    respx.post(f"{BASE}/d2l/api/lp/1.47/enrollments/").mock(
        return_value=httpx.Response(200, json={})
    )


def test_helpExitsZero(capsys):
    with pytest.raises(SystemExit) as excInfo:
        main(["--help"])
    assert excInfo.value.code == 0
    assert "demo-student" in capsys.readouterr().out


def test_missingConfigReportsError(tmp_path, capsys):
    exitCode = main(["--config", str(tmp_path / "nope.toml"), "status"])
    assert exitCode == 2
    assert "not found" in capsys.readouterr().err


@respx.mock
def test_runCommandEndToEnd(configPath, storedTokens, capsys):
    mockDiscovery([301, 302, 303])
    mockCourseWork()
    exitCode = main(["--config", str(configPath), "run"])
    assert exitCode == 0
    out = capsys.readouterr().out
    assert "3 to check" in out
    assert "Users created:        3" in out


@respx.mock
def test_runCommandSecondRunSkips(configPath, storedTokens, capsys):
    mockDiscovery([301, 302, 303])
    mockCourseWork()
    assert main(["--config", str(configPath), "run"]) == 0
    assert main(["--config", str(configPath), "run"]) == 0
    out = capsys.readouterr().out
    assert "3 already completed, 0 to check" in out


@respx.mock
def test_runCommandRecheckReprocessesCompletedCourses(configPath, storedTokens, capsys):
    mockDiscovery([301, 302, 303])
    mockCourseWork()
    assert main(["--config", str(configPath), "run"]) == 0
    assert main(["--config", str(configPath), "run"]) == 0
    capsys.readouterr()  # discard the first two runs' output
    assert main(["--config", str(configPath), "run", "--recheck"]) == 0
    out = capsys.readouterr().out
    assert "0 already completed, 3 to check" in out  # recheck cleared them back to pending


@respx.mock
def test_runCommandDryRun(configPath, storedTokens, capsys):
    mockDiscovery([301])
    respx.get(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\?userName=.*").mock(
        return_value=httpx.Response(404)
    )
    exitCode = main(["--config", str(configPath), "run", "--dry-run"])
    assert exitCode == 0
    assert "wouldCreateAndEnroll" in capsys.readouterr().out


@respx.mock
def test_runErrorsAreWrittenToLogFile(configPath, storedTokens, tmp_path, capsys):
    mockDiscovery([301])
    respx.get(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\?userName=.*").mock(
        return_value=httpx.Response(404)
    )
    respx.post(f"{BASE}/d2l/api/lp/1.47/users/").mock(
        return_value=httpx.Response(500, text="server exploded")
    )

    exitCode = main(["--config", str(configPath), "run"])
    assert exitCode == 1

    logFiles = list((tmp_path / "working" / "logs").glob("demoStudent_*.log"))
    assert len(logFiles) == 1
    content = logFiles[0].read_text()
    assert "ERROR" in content
    assert "301" in content
    assert "500" in content
    assert "finished" in content  # run lifecycle is recorded too


@respx.mock
def test_cleanRunLifecycleIsLogged(configPath, storedTokens, tmp_path, capsys):
    mockDiscovery([301])
    mockCourseWork()
    assert main(["--config", str(configPath), "run"]) == 0
    logFiles = list((tmp_path / "working" / "logs").glob("demoStudent_*.log"))
    content = logFiles[0].read_text()
    assert "Discovered 1 course offerings" in content
    assert "finished" in content


def test_fatalErrorsAreLoggedToFile(configPath, tmp_path, capsys):
    # No stored tokens: run fails with an AuthError after logging is set up
    exitCode = main(["--config", str(configPath), "run"])
    assert exitCode == 2
    logFiles = list((tmp_path / "working" / "logs").glob("demoStudent_*.log"))
    assert len(logFiles) == 1
    assert "demo-student auth" in logFiles[0].read_text()


def test_interruptedRunExits130(configPath, storedTokens, capsys, monkeypatch):
    from demostudent.runner import RunSummary

    def fakeRunEnrollment(*args, **kwargs):
        return RunSummary(runId=1, status="interrupted", coursesDiscovered=10,
                          coursesSkipped=0, coursesChecked=4, usersCreated=4,
                          enrollmentsCreated=4, errorCount=0,
                          finishedAt="2026-08-06T12:00:00+00:00", elapsedSeconds=252.0)

    monkeypatch.setattr("demostudent.cli.runEnrollment", fakeRunEnrollment)
    exitCode = main(["--config", str(configPath), "run"])
    assert exitCode == 130
    assert "interrupted" in capsys.readouterr().out


def test_runWithoutTokensTellsUserToAuth(configPath, capsys):
    exitCode = main(["--config", str(configPath), "run"])
    assert exitCode == 2
    assert "demo-student auth" in capsys.readouterr().err


@respx.mock
def test_statusShowsRunsAndErrors(configPath, storedTokens, capsys):
    mockDiscovery([301])
    mockCourseWork()
    main(["--config", str(configPath), "run"])
    capsys.readouterr()

    exitCode = main(["--config", str(configPath), "status"])
    assert exitCode == 0
    out = capsys.readouterr().out
    assert "completed" in out

    exitCode = main(["--config", str(configPath), "status", "--errors"])
    assert exitCode == 0
    assert "No errors recorded" in capsys.readouterr().out


@respx.mock
def test_whoamiShowsAuthenticatedIdentity(configPath, storedTokens, capsys):
    respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE)
    )
    respx.get(f"{BASE}/d2l/api/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(
            200,
            json={"Identifier": "1234", "FirstName": "Api", "LastName": "User",
                  "UniqueName": "cetloe.api", "ProfileIdentifier": "profile-guid"},
        )
    )
    exitCode = main(["--config", str(configPath), "whoami"])
    assert exitCode == 0
    out = capsys.readouterr().out
    assert "example.brightspace.com" in out
    assert "Api User" in out
    assert "cetloe.api" in out
    assert "1234" in out
    assert "token.json" in out


def test_whoamiWithoutTokensTellsUserToAuth(configPath, capsys):
    exitCode = main(["--config", str(configPath), "whoami"])
    assert exitCode == 2
    assert "demo-student auth" in capsys.readouterr().err


def test_importLegacy(configPath, tmp_path, capsys):
    legacyFile = tmp_path / "demo_783_newuser_co.txt"
    legacyFile.write_text("301\n302\n\n303\n302\n")
    exitCode = main(["--config", str(configPath), "import-legacy", str(legacyFile)])
    assert exitCode == 0
    assert "3" in capsys.readouterr().out

    # Idempotent: importing again adds nothing new
    exitCode = main(["--config", str(configPath), "import-legacy", str(legacyFile)])
    assert exitCode == 0
    assert "0" in capsys.readouterr().out


def test_importLegacyRejectsMalformedLine(configPath, tmp_path, capsys):
    legacyFile = tmp_path / "bad.txt"
    legacyFile.write_text("301\nnot-a-number\n")
    exitCode = main(["--config", str(configPath), "import-legacy", str(legacyFile)])
    assert exitCode == 2
    assert "not-a-number" in capsys.readouterr().err


@respx.mock
def test_authManualCommand(configPath, capsys, monkeypatch, tmp_path):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "new-access", "refresh_token": "new-refresh",
                  "expires_in": 3600, "token_type": "Bearer"},
        )
    )
    respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE)
    )
    respx.get(f"{BASE}/d2l/api/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"FirstName": "Api", "LastName": "User"})
    )

    def fakeInput(prompt):
        printed = capsys.readouterr().out
        authUrl = next(line for line in printed.splitlines()
                       if line.startswith(AUTHORIZATION_URL))
        state = parse_qs(urlparse(authUrl).query)["state"][0]
        return f"https://localhost:8443/callback?code=the-code&state={state}"

    monkeypatch.setattr("builtins.input", fakeInput)
    exitCode = main(["--config", str(configPath), "auth", "--manual"])
    assert exitCode == 0
    assert "Api User" in capsys.readouterr().out

    saved = TokenStore(tmp_path / "working" / "token" / "token.json").load()
    assert saved is not None
    assert saved.accessToken == "new-access"


def mockOrgUnit(orgUnitId, status=200):
    """Org structure lookup for one course: 200 live, 404 deleted, 403 hidden."""
    url = f"{BASE}/d2l/api/lp/1.47/orgstructure/{orgUnitId}"
    if status == 200:
        return respx.get(url).mock(return_value=httpx.Response(
            200, json={"Identifier": str(orgUnitId), "Name": f"Course {orgUnitId}",
                       "Code": f"C{orgUnitId}", "Type": {"Id": 3}}))
    return respx.get(url).mock(return_value=httpx.Response(
        status, json={"Errors": [{"Message": "Resource Not Found"}]}))


def mockNoDataSets():
    """Data Sets unavailable, so the scan falls back to the live API."""
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds").mock(
        return_value=httpx.Response(403, json={"Errors": [{"Message": "Not Authorized"}]}))


def mockUserWalk(users):
    """GET /users/ returning one page of User.UserData entries."""
    respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE))
    mockNoDataSets()
    respx.get(f"{BASE}/d2l/api/lp/1.47/users/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "end", "HasMoreItems": False},
            "Items": [
                {"UserId": userId, "UserName": userName,
                 "Activation": {"IsActive": True}}
                for userId, userName in users
            ],
        })
    )


@respx.mock
def test_findOrphansWalksUsersWhenInventoryIsEmpty(configPath, storedTokens, capsys):
    """No cache yet, so the first run must build one rather than report nothing."""
    mockUserWalk([(9301, "demo.301"), (9302, "demo.302"), (1, "jsmith")])
    mockOrgUnit(301, status=200)
    mockOrgUnit(302, status=404)

    exitCode = main(["--config", str(configPath), "find-orphans"])
    assert exitCode == 0
    out = capsys.readouterr().out
    assert "demo.302" in out
    assert "1 orphaned demo student" in out


@respx.mock
def test_findOrphansFindsOrphanWithNoCompletedCourseRow(configPath, storedTokens,
                                                        capsys, tmp_path):
    """The bug this rework fixes: a pruned course row must not hide an orphan."""
    import sqlite3

    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)

    assert main(["--config", str(configPath), "find-orphans"]) == 0

    dbPath = tmp_path / "working" / "db" / "demoStudent.db"
    connection = sqlite3.connect(dbPath)
    completed = connection.execute("SELECT COUNT(*) FROM completedCourses").fetchone()[0]
    connection.close()
    assert completed == 0  # nothing in the skip list, yet the orphan was found
    assert "demo.302" in capsys.readouterr().out


@respx.mock
def test_findOrphansReusesTheInventoryWithoutRescan(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    assert main(["--config", str(configPath), "find-orphans"]) == 0
    capsys.readouterr()

    walkRoute = respx.get(f"{BASE}/d2l/api/lp/1.47/users/")
    walkRoute.reset()
    assert main(["--config", str(configPath), "find-orphans"]) == 0
    assert not walkRoute.called  # the cached inventory was used


@respx.mock
def test_findOrphansRescanWalksAgain(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    assert main(["--config", str(configPath), "find-orphans"]) == 0
    capsys.readouterr()

    exitCode = main(["--config", str(configPath), "find-orphans", "--rescan"])
    assert exitCode == 0
    assert "Users walked" in capsys.readouterr().out


@respx.mock
def test_findOrphansAbortsWhenUsernamesAreHidden(configPath, storedTokens, capsys):
    """An under-permissioned role would otherwise report a clean empty tenant."""
    respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE))
    mockNoDataSets()
    respx.get(f"{BASE}/d2l/api/lp/1.47/users/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "end", "HasMoreItems": False},
            "Items": [{"UserId": 9301, "Activation": {"IsActive": True}}],
        })
    )

    exitCode = main(["--config", str(configPath), "find-orphans"])
    assert exitCode == 2
    assert "See Usernames" in capsys.readouterr().err


@respx.mock
def test_findOrphansWithNothingOrphanedSaysSo(configPath, storedTokens, capsys):
    mockUserWalk([(9301, "demo.301")])
    mockOrgUnit(301, status=200)

    assert main(["--config", str(configPath), "find-orphans"]) == 0
    assert "No orphaned demo students found" in capsys.readouterr().out


@respx.mock
def test_findOrphansSkipsCoursesItCannotSee(configPath, storedTokens, capsys):
    """A 403 is not proof of deletion; it must never produce an orphan."""
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=403)

    assert main(["--config", str(configPath), "find-orphans"]) == 0
    out = capsys.readouterr().out
    assert "No orphaned demo students found" in out
    assert "Could not confirm:    1" in out


@respx.mock
def test_findOrphansWritesCsvReport(configPath, storedTokens, tmp_path, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    reportPath = tmp_path / "orphans.csv"

    assert main(["--config", str(configPath), "find-orphans",
                 "--out", str(reportPath)]) == 0
    content = reportPath.read_text()
    assert "orgUnitId,userName,userId" in content
    assert "302,demo.302,9302" in content


@respx.mock
def test_deleteOrphansRequiresConfirmFlag(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    activationRoute = respx.put(
        url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+/activation")

    assert main(["--config", str(configPath), "delete-orphans"]) == 0
    assert not activationRoute.called
    out = capsys.readouterr().out
    assert "--confirm" in out
    assert "demo.302" in out


@respx.mock
def test_deleteOrphansWithConfirmDeactivatesByDefault(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    deleteRoute = respx.delete(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+")
    activationRoute = respx.put(
        url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+/activation").mock(
        return_value=httpx.Response(200))

    assert main(["--config", str(configPath), "delete-orphans", "--confirm"]) == 0
    assert activationRoute.called
    assert not deleteRoute.called
    assert json.loads(activationRoute.calls.last.request.content) == {"IsActive": False}
    assert "Deactivated:          1" in capsys.readouterr().out


@respx.mock
def test_deleteOrphansHardDeleteRemovesTheUser(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    deleteRoute = respx.delete(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+").mock(
        return_value=httpx.Response(200))

    assert main(["--config", str(configPath), "delete-orphans",
                 "--confirm", "--hard-delete"]) == 0
    assert deleteRoute.called
    out = capsys.readouterr().out
    assert "Deleted:              1" in out
    assert "support request" in out  # irreversibility stated before acting


@respx.mock
def test_deactivatedOrphanIsNotOfferedAgain(configPath, storedTokens, capsys):
    """Once retired, an account should stop appearing in every later run."""
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    respx.put(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+/activation").mock(
        return_value=httpx.Response(200))
    assert main(["--config", str(configPath), "delete-orphans", "--confirm"]) == 0
    capsys.readouterr()

    assert main(["--config", str(configPath), "find-orphans"]) == 0
    assert "No orphaned demo students found" in capsys.readouterr().out


@respx.mock
def test_deleteOrphansNeverActsOnAnUnconfirmableCourse(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=403)
    deleteRoute = respx.delete(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+")
    activationRoute = respx.put(
        url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+/activation")

    assert main(["--config", str(configPath), "delete-orphans", "--confirm"]) == 0
    assert not deleteRoute.called
    assert not activationRoute.called
    assert "Could not confirm:    1" in capsys.readouterr().out


@respx.mock
def test_deleteOrphansReportsFailureAndExitsOne(configPath, storedTokens, capsys):
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    respx.put(url__regex=rf"{BASE}/d2l/api/lp/1\.47/users/\d+/activation").mock(
        return_value=httpx.Response(403, json={"Errors": [{"Message": "Not Authorized"}]}))

    assert main(["--config", str(configPath), "delete-orphans", "--confirm"]) == 1
    assert "Failed:               1" in capsys.readouterr().out


def mockDataSetsUsers(rows):
    """A Users data set with one downloadable full extract."""
    import io
    import zipfile

    respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE))
    schemaId = "11111111-1111-1111-1111-111111111111"
    pluginId = "22222222-2222-2222-2222-222222222222"
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds").mock(
        return_value=httpx.Response(200, json={"Objects": [{
            "SchemaId": schemaId,
            "Full": {"PluginId": pluginId, "Name": "Users",
                     "Description": "", "ExtractsLink": ""},
            "Differential": None, "ExtractsLink": "",
        }], "Next": None}))
    respx.get(
        f"{BASE}/d2l/api/lp/1.47/datasets/bds/{schemaId}/plugins/{pluginId}/extracts"
    ).mock(return_value=httpx.Response(200, json={"Objects": [{
        "SchemaId": schemaId, "PluginId": pluginId, "BdsType": "Full",
        "CreatedDate": "2026-09-15T03:00:00.000Z", "DownloadSize": 100,
        "DownloadLink": f"{BASE}/d2l/api/lp/1.47/datasets/bds/dl", "Version": "11.7",
    }], "Next": None}))

    header = ("UserId,UserName,OrgDefinedId,FirstName,MiddleName,LastName,"
              "IsActive,ExternalEmail,CreationDate,Version,OrgRoleId,DateDeleted")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Users.csv", "\n".join([header] + rows))
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds/dl").mock(
        return_value=httpx.Response(200, content=buffer.getvalue()))


def dataSetRow(userId, userName, orgRoleId="783"):
    return f"{userId},{userName},,Demo,,Student,1,,2026-01-01 00:00:00.000,1,{orgRoleId},"


@respx.mock
def test_findOrphansPrefersDataSetsOverTheApiWalk(configPath, storedTokens, capsys):
    mockDataSetsUsers([dataSetRow(9302, "demo.302")])
    mockOrgUnit(302, status=404)
    walkRoute = respx.get(f"{BASE}/d2l/api/lp/1.47/users/")

    assert main(["--config", str(configPath), "find-orphans"]) == 0
    assert not walkRoute.called
    out = capsys.readouterr().out
    assert "2026-09-15" in out  # the extract date is shown, so staleness is visible
    assert "demo.302" in out


@respx.mock
def test_dataSetsScanRequiresTheDemoStudentRole(configPath, storedTokens, capsys):
    """An account named like a demo student but holding another role is left alone."""
    mockDataSetsUsers([
        dataSetRow(9302, "demo.302", orgRoleId="783"),
        dataSetRow(9999, "demo.12345", orgRoleId="110"),
    ])
    mockOrgUnit(302, status=404)
    mockOrgUnit(12345, status=404)

    assert main(["--config", str(configPath), "find-orphans"]) == 0
    out = capsys.readouterr().out
    assert "demo.302" in out
    assert "demo.12345" not in out.split("orphaned demo students")[-1]


@respx.mock
def test_apiWalkFlagSkipsDataSets(configPath, storedTokens, capsys):
    mockDataSetsUsers([dataSetRow(9302, "demo.302")])
    dataSetRoute = respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds")
    dataSetRoute.reset()
    respx.get(f"{BASE}/d2l/api/lp/1.47/users/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "end", "HasMoreItems": False},
            "Items": [{"UserId": 9302, "UserName": "demo.302",
                       "Activation": {"IsActive": True}}],
        }))
    mockOrgUnit(302, status=404)

    assert main(["--config", str(configPath), "find-orphans", "--api-walk"]) == 0
    assert not dataSetRoute.called
    assert "Users walked" in capsys.readouterr().out


@respx.mock
def test_findOrphansSaysUpFrontThatItChangesNothing(configPath, storedTokens, capsys):
    """A long read-only run should say so before the wait, not after."""
    mockUserWalk([(9302, "demo.302")])
    mockOrgUnit(302, status=404)

    assert main(["--config", str(configPath), "find-orphans"]) == 0
    firstLine = capsys.readouterr().out.splitlines()[0]
    assert "report only" in firstLine.lower()
    assert "is changed" in firstLine.lower()
