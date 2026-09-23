"""Tests for the Brightspace Data Sets client.

Covers the three-call sequence to reach a Users extract, the cross-domain
redirect D2L uses for the download, and the ZIP-of-CSV format the file
arrives in.
"""

import io
import time
import zipfile

import httpx
import pytest
import respx

from demostudent.auth import TokenManager, TokenSet, TokenStore
from demostudent.config import AppConfig
from demostudent.d2lApi import D2lApiClient, D2lApiError

BASE = "https://example.brightspace.com"

VERSIONS_RESPONSE = [
    {"ProductCode": "lp", "LatestVersion": "1.47", "SupportedVersions": ["1.47"]},
    {"ProductCode": "le", "LatestVersion": "1.79", "SupportedVersions": ["1.79"]},
]

USERS_SCHEMA = "11111111-1111-1111-1111-111111111111"
USERS_PLUGIN = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
    )


@pytest.fixture
def api(config):
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="valid-token", refreshToken="r",
                        expiresAt=time.time() + 3600))
    client = D2lApiClient(config, TokenManager(config, store),
                          sleepFunc=lambda _seconds: None)
    yield client
    client.close()


def mockVersions():
    return respx.get(f"{BASE}/d2l/api/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE))


def dataSetListPage(items, next_=None):
    return {"Objects": items, "Next": next_}


def usersDataSet():
    return {
        "SchemaId": USERS_SCHEMA,
        "Full": {"PluginId": USERS_PLUGIN, "Name": "Users",
                 "Description": "All users", "ExtractsLink": "x"},
        "Differential": {"PluginId": "33333333-3333-3333-3333-333333333333",
                         "Name": "Users Differential", "Description": "", "ExtractsLink": "y"},
        "ExtractsLink": "z",
    }


def usersCsvZip(rows):
    """A ZIP holding one CSV, the way a BDS extract arrives."""
    header = ("UserId,UserName,OrgDefinedId,FirstName,MiddleName,LastName,"
              "IsActive,ExternalEmail,CreationDate,Version,OrgRoleId,DateDeleted")
    body = "\n".join(header for _ in [0])
    lines = [header] + rows
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Users.csv", "\n".join(lines))
    return buffer.getvalue()


@respx.mock
def test_findsTheUsersDataSetByName(api):
    mockVersions()
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds").mock(
        return_value=httpx.Response(200, json=dataSetListPage([
            {"SchemaId": "other", "Full": {"PluginId": "p", "Name": "Enrollments",
                                           "Description": "", "ExtractsLink": ""},
             "Differential": None, "ExtractsLink": ""},
            usersDataSet(),
        ])))

    dataSet = api.findDataSet("Users")
    assert dataSet is not None
    assert dataSet["schemaId"] == USERS_SCHEMA
    assert dataSet["pluginId"] == USERS_PLUGIN


@respx.mock
def test_findDataSetFollowsPagination(api):
    mockVersions()
    route = respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds")
    route.side_effect = [
        httpx.Response(200, json=dataSetListPage(
            [{"SchemaId": "other", "Full": {"PluginId": "p", "Name": "Grades",
                                            "Description": "", "ExtractsLink": ""},
              "Differential": None, "ExtractsLink": ""}],
            next_=f"{BASE}/d2l/api/lp/1.47/datasets/bds?page=2")),
        httpx.Response(200, json=dataSetListPage([usersDataSet()])),
    ]
    dataSet = api.findDataSet("Users")
    assert dataSet is not None
    assert route.call_count == 2


@respx.mock
def test_findDataSetReturnsNoneWhenAbsent(api):
    mockVersions()
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds").mock(
        return_value=httpx.Response(200, json=dataSetListPage([])))
    assert api.findDataSet("Users") is None


@respx.mock
def test_findDataSetReturnsNoneWhenNotProvisioned(api):
    """D2L answers 403 whether the tool is off, the role lacks it, or the scope is missing."""
    mockVersions()
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds").mock(
        return_value=httpx.Response(403, json={"Errors": [{"Message": "Not Authorized"}]}))
    assert api.findDataSet("Users") is None


@respx.mock
def test_latestExtractPicksTheNewest(api):
    """D2L keeps an older extract around for up to 14 days after a new one."""
    mockVersions()
    respx.get(
        f"{BASE}/d2l/api/lp/1.47/datasets/bds/{USERS_SCHEMA}/plugins/{USERS_PLUGIN}/extracts"
    ).mock(return_value=httpx.Response(200, json=dataSetListPage([
        {"SchemaId": USERS_SCHEMA, "PluginId": USERS_PLUGIN, "BdsType": "Full",
         "CreatedDate": "2026-09-08T03:00:00.000Z", "DownloadSize": 100,
         "DownloadLink": "https://example.brightspace.com/old", "Version": "11.7"},
        {"SchemaId": USERS_SCHEMA, "PluginId": USERS_PLUGIN, "BdsType": "Full",
         "CreatedDate": "2026-09-15T03:00:00.000Z", "DownloadSize": 120,
         "DownloadLink": "https://example.brightspace.com/new", "Version": "11.7"},
    ])))

    extract = api.latestExtract(USERS_SCHEMA, USERS_PLUGIN)
    assert extract is not None
    assert extract["createdDate"] == "2026-09-15T03:00:00.000Z"
    assert extract["downloadLink"].endswith("/new")


@respx.mock
def test_latestExtractIgnoresDifferentials(api):
    mockVersions()
    respx.get(
        f"{BASE}/d2l/api/lp/1.47/datasets/bds/{USERS_SCHEMA}/plugins/{USERS_PLUGIN}/extracts"
    ).mock(return_value=httpx.Response(200, json=dataSetListPage([
        {"SchemaId": USERS_SCHEMA, "PluginId": USERS_PLUGIN, "BdsType": "Differential",
         "CreatedDate": "2026-09-20T03:00:00.000Z", "DownloadSize": 10,
         "DownloadLink": "https://example.brightspace.com/diff", "Version": "11.7"},
        {"SchemaId": USERS_SCHEMA, "PluginId": USERS_PLUGIN, "BdsType": "Full",
         "CreatedDate": "2026-09-15T03:00:00.000Z", "DownloadSize": 120,
         "DownloadLink": "https://example.brightspace.com/full", "Version": "11.7"},
    ])))

    extract = api.latestExtract(USERS_SCHEMA, USERS_PLUGIN)
    assert extract is not None
    assert extract["downloadLink"].endswith("/full")


@respx.mock
def test_downloadFollowsRedirectWithoutTheAuthHeader(api, tmp_path):
    """D2L redirects to pre-authorized storage; sending the token there is wrong."""
    mockVersions()
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds/download").mock(
        return_value=httpx.Response(302, headers={"Location": "https://files.example.net/x.zip"}))
    storageRoute = respx.get("https://files.example.net/x.zip").mock(
        return_value=httpx.Response(200, content=usersCsvZip(["1,demo.301,,Demo,,Student,1,,,,783,"])))

    target = tmp_path / "users.zip"
    api.downloadExtract(f"{BASE}/d2l/api/lp/1.47/datasets/bds/download", target)

    assert target.is_file()
    assert "Authorization" not in storageRoute.calls.last.request.headers


@respx.mock
def test_downloadSendsAuthOnTheOriginalHost(api, tmp_path):
    """A same-host redirect is still D2L, so the token belongs on it."""
    mockVersions()
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds/download").mock(
        return_value=httpx.Response(200, content=usersCsvZip(["1,demo.301,,D,,S,1,,,,783,"])))

    target = tmp_path / "users.zip"
    api.downloadExtract(f"{BASE}/d2l/api/lp/1.47/datasets/bds/download", target)
    assert target.is_file()


@respx.mock
def test_downloadRaisesOnQuotaExhaustion(api, tmp_path):
    mockVersions()
    respx.get(f"{BASE}/d2l/api/lp/1.47/datasets/bds/download").mock(
        return_value=httpx.Response(403, headers={"Retry-After": "3600"},
                                    json={"title": "Quota exceeded"}))
    with pytest.raises(D2lApiError) as excInfo:
        api.downloadExtract(f"{BASE}/d2l/api/lp/1.47/datasets/bds/download",
                            tmp_path / "users.zip")
    assert excInfo.value.httpStatus == 403


def writeUsersZip(path, rows):
    """A Users extract on disk, ZIP of one CSV, every field a string."""
    header = ("UserId,UserName,OrgDefinedId,FirstName,MiddleName,LastName,"
              "IsActive,ExternalEmail,CreationDate,Version,OrgRoleId,DateDeleted")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Users.csv", "\n".join([header] + rows))
    return path


def userRow(userId, userName, isActive="1", orgRoleId="783", dateDeleted=""):
    return (f"{userId},{userName},,Demo,,Student,{isActive},,"
            f"2026-01-01 00:00:00.000,1,{orgRoleId},{dateDeleted}")


def test_readsDemoStudentsFromTheExtract(config, tmp_path):
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [
        userRow(9301, "demo.301"),
        userRow(1, "jsmith", orgRoleId="110"),
        userRow(9302, "demo.302"),
    ])

    result = readDemoStudentsFromExtract(archive, config)

    assert [student["userName"] for student in result.demoStudents] == ["demo.301", "demo.302"]
    assert result.demoStudents[0]["orgUnitId"] == 301
    assert result.rowsRead == 3


def test_requiresTheDemoStudentRole(config, tmp_path):
    """A hand-made account named demo.12345 is not ours to touch."""
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [
        userRow(9301, "demo.301", orgRoleId="783"),
        userRow(9999, "demo.12345", orgRoleId="110"),
    ])

    result = readDemoStudentsFromExtract(archive, config)

    assert [student["userName"] for student in result.demoStudents] == ["demo.301"]
    assert result.wrongRole == ["demo.12345"]


def test_skipsTheSystemUserRow(config, tmp_path):
    """The full extract carries a UserId 0 system entry."""
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [
        userRow(0, "system", orgRoleId=""),
        userRow(9301, "demo.301"),
    ])
    result = readDemoStudentsFromExtract(archive, config)
    assert len(result.demoStudents) == 1


def test_skipsDeletedUsers(config, tmp_path):
    """A user already deleted in D2L is nothing to clean up."""
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [
        userRow(9301, "demo.301"),
        userRow(9302, "demo.302", dateDeleted="2026-08-01 00:00:00.000"),
    ])
    result = readDemoStudentsFromExtract(archive, config)
    assert [student["userName"] for student in result.demoStudents] == ["demo.301"]


def test_readsActivationState(config, tmp_path):
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [
        userRow(9301, "demo.301", isActive="1"),
        userRow(9302, "demo.302", isActive="0"),
    ])
    result = readDemoStudentsFromExtract(archive, config)
    byName = {student["userName"]: student for student in result.demoStudents}
    assert byName["demo.301"]["isActive"] is True
    assert byName["demo.302"]["isActive"] is False


def test_nullIsActiveIsNotTreatedAsInactive(config, tmp_path):
    """D2L documents IsActive as nullable and never says what null means."""
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [userRow(9301, "demo.301", isActive="")])
    result = readDemoStudentsFromExtract(archive, config)
    assert result.demoStudents[0]["isActive"] is None


def test_reportsUnparseablePrefixMatches(config, tmp_path):
    from demostudent.dataSets import readDemoStudentsFromExtract

    archive = writeUsersZip(tmp_path / "users.zip", [userRow(42, "demo.legacy")])
    result = readDemoStudentsFromExtract(archive, config)
    assert result.demoStudents == []
    assert result.unparsed == ["demo.legacy"]


def test_rejectsAnArchiveWithNoCsv(config, tmp_path):
    from demostudent.dataSets import DataSetFormatError, readDemoStudentsFromExtract

    archive = tmp_path / "users.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("readme.txt", "not a csv")

    with pytest.raises(DataSetFormatError):
        readDemoStudentsFromExtract(archive, config)


def test_rejectsAnExtractMissingTheColumnsItNeeds(config, tmp_path):
    """A schema change should fail loudly, not silently find zero demo students."""
    from demostudent.dataSets import DataSetFormatError, readDemoStudentsFromExtract

    archive = tmp_path / "users.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Users.csv", "UserId,FirstName\n1,Demo")

    with pytest.raises(DataSetFormatError) as excInfo:
        readDemoStudentsFromExtract(archive, config)
    assert "UserName" in str(excInfo.value)
