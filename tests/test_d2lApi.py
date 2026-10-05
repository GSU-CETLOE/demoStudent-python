"""Tests for the thread-safe D2L API client."""

import time

import httpx
import pytest
import respx

from demostudent.auth import TOKEN_URL, TokenManager, TokenSet, TokenStore
from demostudent.config import AppConfig
from demostudent.d2lApi import D2lApiClient, D2lApiError, D2lUnavailableError

PROXY = "http://127.0.0.1:8080"
TENANT_API = "https://example.brightspace.com/d2l/api"

VERSIONS_RESPONSE = [
    {"ProductCode": "bas", "LatestVersion": "1.1", "SupportedVersions": ["1.0", "1.1"]},
    {"ProductCode": "lp", "LatestVersion": "1.47", "SupportedVersions": ["1.46", "1.47"]},
    {"ProductCode": "le", "LatestVersion": "1.79", "SupportedVersions": ["1.78", "1.79"]},
]


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        proxyUrl="http://127.0.0.1:8080",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
    )


@pytest.fixture
def api(config):
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="valid-token", refreshToken="r", expiresAt=time.time() + 3600))
    client = D2lApiClient(config, TokenManager(config, store), sleepFunc=lambda _seconds: None)
    yield client
    client.close()


@pytest.fixture
def directApi(config):
    """A client configured without proxyUrl, so it calls D2L itself."""
    from dataclasses import replace
    directConfig = replace(config, proxyUrl=None)
    store = TokenStore(directConfig.tokenPath)
    store.save(TokenSet(accessToken="valid-token", refreshToken="r", expiresAt=time.time() + 3600))
    client = D2lApiClient(directConfig, TokenManager(directConfig, store),
                          sleepFunc=lambda _seconds: None)
    yield client
    client.close()


@pytest.fixture
def proxyKeyConfig(tmp_path):
    """A config for apiProxy2's proxy authentication: a key, and no OAuth settings."""
    return AppConfig(
        host="example.brightspace.com",
        proxyUrl="http://127.0.0.1:8080",
        proxyKey="app-guid",
        workingDir=tmp_path / "working",
    )


@pytest.fixture
def proxyKeyApi(proxyKeyConfig):
    """A client that holds no D2L token and sends its proxy key instead."""
    client = D2lApiClient(proxyKeyConfig, None, sleepFunc=lambda _seconds: None)
    yield client
    client.close()


def mockVersions():
    return respx.get(f"{PROXY}/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE)
    )


@respx.mock
def test_proxyKeyIsSentInsteadOfABearerToken(proxyKeyApi):
    """apiProxy2 holds the D2L token, so this client has none to send."""
    versionsRoute = mockVersions()
    whoAmIRoute = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"}))
    assert proxyKeyApi.whoAmI() == {"Identifier": "1"}
    for route in (versionsRoute, whoAmIRoute):
        headers = route.calls.last.request.headers
        assert headers["X-Proxy-Key"] == "app-guid"
        assert "Authorization" not in headers
        assert "X-ICollege-User" not in headers
        assert headers["Cache-Control"] == "no-cache"


@respx.mock
def test_proxyUserNamesTheAccountToActAs(proxyKeyConfig):
    """An application granted several accounts has to say which one it means."""
    from dataclasses import replace
    api = D2lApiClient(replace(proxyKeyConfig, proxyUser="jdoe"), None)
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"}))
    api.whoAmI()
    assert route.calls.last.request.headers["X-ICollege-User"] == "jdoe"
    api.close()


@respx.mock
def test_proxyRefusalOfTheKeyIsReportedWithoutARetry(proxyKeyApi):
    """There is no token here to refresh, so a 401 is the proxy's final answer."""
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(401, json={
            "error": "proxyAuthorizationRequired",
            "message": "Send your application's proxy key as X-Proxy-Key.",
            "requestId": "request-1"}))
    with pytest.raises(D2lApiError) as excInfo:
        proxyKeyApi.whoAmI()
    assert excInfo.value.httpStatus == 401
    assert "proxyAuthorizationRequired" in str(excInfo.value)
    assert route.call_count == 1


@respx.mock
def test_ownTokenModeSendsNoProxyHeaders(api):
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"}))
    api.whoAmI()
    headers = route.calls.last.request.headers
    assert "X-Proxy-Key" not in headers
    assert "X-ICollege-User" not in headers


@respx.mock
def test_proxyKeyOutranksATokenManager(proxyKeyConfig):
    """The config picks the credential, so a stored token is never sent with a key."""
    store = TokenStore(proxyKeyConfig.tokenPath)
    store.save(TokenSet(accessToken="valid-token", refreshToken="r", expiresAt=time.time() + 3600))
    api = D2lApiClient(proxyKeyConfig, TokenManager(proxyKeyConfig, store))
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"}))
    api.whoAmI()
    headers = route.calls.last.request.headers
    assert headers["X-Proxy-Key"] == "app-guid"
    assert "Authorization" not in headers
    api.close()


def test_clientWithoutProxyKeyNeedsATokenManager(config):
    """Without a key the bearer token is the only credential there is."""
    with pytest.raises(ValueError, match="TokenManager"):
        D2lApiClient(config, None)


@respx.mock
def test_versionNegotiationUsesLatestSupported(api):
    mockVersions()
    whoAmIRoute = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"})
    )
    api.whoAmI()
    assert whoAmIRoute.called
    request = whoAmIRoute.calls.last.request
    assert request.headers["Authorization"] == "Bearer valid-token"


@respx.mock
def test_versionsFetchedOnlyOnce(api):
    versionsRoute = mockVersions()
    respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"})
    )
    api.whoAmI()
    api.whoAmI()
    assert versionsRoute.call_count == 1


@respx.mock
def test_withoutProxyUrlCallsD2lDirectly(directApi):
    respx.get(f"{TENANT_API}/versions/").mock(
        return_value=httpx.Response(200, json=VERSIONS_RESPONSE))
    route = respx.get(f"{TENANT_API}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(200, json={"Identifier": "1"}))
    directApi.whoAmI()
    assert route.calls.last.request.headers["Authorization"] == "Bearer valid-token"


@respx.mock
def test_unreachableD2lIsNamedInTheError(directApi):
    """Direct mode has no proxy to blame, so the message must not mention one."""
    respx.get(f"{TENANT_API}/versions/").mock(
        side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(D2lUnavailableError) as excInfo:
        directApi.whoAmI()
    assert TENANT_API in str(excInfo.value)
    assert "apiProxy2" not in str(excInfo.value)


@respx.mock
def test_everyRequestAsksTheProxyNotToCache(api):
    """Writes are decided from reads, so a cached read could act on stale state."""
    versionsRoute = mockVersions()
    readRoute = respx.get(f"{PROXY}/lp/1.47/orgstructure/55").mock(
        return_value=httpx.Response(200, json={"Identifier": "55"}))
    writeRoute = respx.put(f"{PROXY}/lp/1.47/users/9002/activation").mock(
        return_value=httpx.Response(200))
    api.getOrgUnit(55)
    api.setUserActivation(9002, isActive=False)
    for route in (versionsRoute, readRoute, writeRoute):
        assert route.calls.last.request.headers["Cache-Control"] == "no-cache"


@respx.mock
def test_unreachableProxyIsNamedInTheError(api):
    """Every call goes through the proxy, so a stopped proxy is the likeliest failure."""
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/orgstructure/55").mock(
        side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(D2lUnavailableError) as excInfo:
        api.getOrgUnit(55)
    assert PROXY in str(excInfo.value)
    assert excInfo.value.httpStatus is None


@respx.mock
def test_unreachableProxyDuringVersionNegotiationKeepsItsType(api):
    """Callers stop on an unreachable proxy, so the negotiation must not hide it."""
    respx.get(f"{PROXY}/versions/").mock(side_effect=httpx.ConnectError("Connection refused"))
    with pytest.raises(D2lUnavailableError):
        api.getOrgUnit(55)


@respx.mock
def test_failedVersionNegotiationCarriesNoStatusOfItsOwnRoute(api):
    """A 404 on /versions/ must not read as the org unit being gone.

    The orphan check treats a 404 from getOrgUnit as a deleted course.
    If the negotiation's status leaked through, a wrong base URL would
    mark every course deleted.
    """
    respx.get(f"{PROXY}/versions/").mock(return_value=httpx.Response(404))
    with pytest.raises(D2lUnavailableError) as excInfo:
        api.getOrgUnit(55)
    assert excInfo.value.httpStatus is None
    assert "versions" in str(excInfo.value)


@respx.mock
def test_getUserByUsernameFound(api):
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/users/", params={"userName": "demo.123"}).mock(
        return_value=httpx.Response(200, json={"UserId": 9001, "UserName": "demo.123"})
    )
    user = api.getUserByUsername("demo.123")
    assert user is not None
    assert user["UserId"] == 9001


@respx.mock
def test_getUserByUsernameMissingReturnsNone(api):
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/users/", params={"userName": "demo.404"}).mock(
        return_value=httpx.Response(404)
    )
    assert api.getUserByUsername("demo.404") is None


def enrollmentItem(orgUnitId: int, roleId: int) -> dict:
    """One entry as /enrollments/users/{userId}/orgUnits/ returns it."""
    return {
        "OrgUnit": {"Id": orgUnitId, "Type": {"Id": 3, "Code": "Course Offering"},
                    "Name": f"Course {orgUnitId}", "Code": f"CO.{orgUnitId}"},
        "Role": {"Id": roleId, "Code": None, "Name": "Demo Student"},
    }


@respx.mock
def test_getEnrollmentMissingReturnsNone(api):
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/enrollments/users/9001/orgUnits/").mock(
        return_value=httpx.Response(404)
    )
    assert api.getEnrollment(55, 9001) is None


@respx.mock
def test_getEnrollmentNotInThatOrgUnitReturnsNone(api):
    """The org-level enrollment every created user gets is not a course enrollment."""
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/enrollments/users/9001/orgUnits/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "1070555", "HasMoreItems": False},
            "Items": [enrollmentItem(1070555, 783)],
        })
    )
    assert api.getEnrollment(55, 9001) is None


@respx.mock
def test_getEnrollmentFound(api):
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/enrollments/users/9001/orgUnits/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "55", "HasMoreItems": False},
            "Items": [enrollmentItem(1070555, 783), enrollmentItem(55, 783)],
        })
    )
    enrollment = api.getEnrollment(55, 9001)
    assert enrollment is not None
    assert enrollment["RoleId"] == 783
    assert enrollment["OrgUnitId"] == 55


@respx.mock
def test_getEnrollmentFollowsPagingBookmarks(api):
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/enrollments/users/9001/orgUnits/")
    route.side_effect = [
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "page1", "HasMoreItems": True},
            "Items": [enrollmentItem(1070555, 783)],
        }),
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "page2", "HasMoreItems": False},
            "Items": [enrollmentItem(55, 783)],
        }),
    ]
    enrollment = api.getEnrollment(55, 9001)
    assert enrollment is not None
    assert enrollment["RoleId"] == 783
    assert route.calls[1].request.url.params["bookmark"] == "page1"


@respx.mock
def test_createUserPostsExpectedBody(api, config):
    mockVersions()
    route = respx.post(f"{PROXY}/lp/1.47/users/").mock(
        return_value=httpx.Response(200, json={"UserId": 9002, "UserName": "demo.55"})
    )
    user = api.createUser(
        userName="demo.55",
        firstName="Demo",
        lastName="Student",
        externalEmail="demo.55@student.gsu.edu",
        roleId=783,
    )
    assert user["UserId"] == 9002
    import json
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "OrgDefinedId": "",
        "FirstName": "Demo",
        "MiddleName": "",
        "LastName": "Student",
        "ExternalEmail": "demo.55@student.gsu.edu",
        "UserName": "demo.55",
        "RoleId": 783,
        "IsActive": True,
        "SendCreationEmail": False,
    }


@respx.mock
def test_enrollUserPostsExpectedBody(api):
    mockVersions()
    route = respx.post(f"{PROXY}/lp/1.47/enrollments/").mock(
        return_value=httpx.Response(200, json={"OrgUnitId": 55, "UserId": 9002, "RoleId": 783})
    )
    api.enrollUser(orgUnitId=55, userId=9002, roleId=783)
    import json
    body = json.loads(route.calls.last.request.content)
    assert body == {"OrgUnitId": 55, "UserId": 9002, "RoleId": 783, "SendEnrollmentEmail": False}


@respx.mock
def test_iterDescendantsFollowsBookmarks(api):
    mockVersions()
    url = f"{PROXY}/lp/1.47/orgstructure/201/descendants/paged/"
    firstPage = httpx.Response(
        200,
        json={
            "PagingInfo": {"Bookmark": "bm1", "HasMoreItems": True},
            "Items": [{"Identifier": "301"}, {"Identifier": "302"}],
        },
    )
    secondPage = httpx.Response(
        200,
        json={
            "PagingInfo": {"Bookmark": "bm2", "HasMoreItems": False},
            "Items": [{"Identifier": "303"}],
        },
    )
    route = respx.get(url).mock(side_effect=[firstPage, secondPage])
    items = list(api.iterDescendants(201, ouTypeId=3))
    assert [item["Identifier"] for item in items] == ["301", "302", "303"]
    assert route.call_count == 2
    secondRequest = route.calls[1].request
    assert "bookmark=bm1" in str(secondRequest.url)


@respx.mock
def test_unauthorizedTriggersSingleRefreshAndRetry(config):
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="revoked", refreshToken="old", expiresAt=time.time() + 3600))
    api = D2lApiClient(config, TokenManager(config, store), sleepFunc=lambda _seconds: None)

    mockVersions()
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "fresh", "refresh_token": "rotated",
                  "expires_in": 3600, "token_type": "Bearer"},
        )
    )

    def whoAmIResponder(request):
        if request.headers["Authorization"] == "Bearer fresh":
            return httpx.Response(200, json={"Identifier": "1"})
        return httpx.Response(401)

    route = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(side_effect=whoAmIResponder)
    assert api.whoAmI() == {"Identifier": "1"}
    assert route.call_count == 2
    api.close()


@respx.mock
def test_rateLimitRetriesWithRetryAfter(api):
    mockVersions()
    sleeps = []
    api._sleepFunc = sleeps.append
    route = respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"Identifier": "1"}),
        ]
    )
    assert api.whoAmI() == {"Identifier": "1"}
    assert route.call_count == 2
    assert sleeps == [7.0]


@respx.mock
def test_persistentServerErrorRaisesApiError(api):
    mockVersions()
    respx.get(f"{PROXY}/lp/1.47/users/whoami").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(D2lApiError) as excInfo:
        api.whoAmI()
    assert excInfo.value.httpStatus == 500


@respx.mock
def test_deleteUserCallsTheDeleteRoute(api):
    mockVersions()
    route = respx.delete(f"{PROXY}/lp/1.47/users/9002").mock(
        return_value=httpx.Response(200)
    )
    api.deleteUser(9002)
    assert route.called


@respx.mock
def test_deleteUserRaisesOnForbidden(api):
    mockVersions()
    respx.delete(f"{PROXY}/lp/1.47/users/9002").mock(
        return_value=httpx.Response(403, json={"Errors": [{"Message": "Not Authorized"}]})
    )
    with pytest.raises(D2lApiError) as excInfo:
        api.deleteUser(9002)
    assert excInfo.value.httpStatus == 403


@respx.mock
def test_setUserActivationSendsOnlyTheFlag(api):
    """The activation route takes one boolean; it never rewrites the record."""
    mockVersions()
    route = respx.put(f"{PROXY}/lp/1.47/users/9002/activation").mock(
        return_value=httpx.Response(200)
    )
    api.setUserActivation(9002, isActive=False)
    import json
    assert json.loads(route.calls.last.request.content) == {"IsActive": False}


def userItem(userId, userName, isActive=True):
    """One entry as GET /users/ returns it (User.UserData)."""
    return {
        "OrgId": 6606, "UserId": userId, "FirstName": "Demo", "MiddleName": None,
        "LastName": "Student", "UserName": userName, "ExternalEmail": None,
        "OrgDefinedId": None, "UniqueIdentifier": userName,
        "Activation": {"IsActive": isActive},
        "DisplayName": "Demo Student", "LastAccessedDate": None, "Pronouns": "",
    }


@respx.mock
def test_iterUsersFollowsPagingBookmarks(api):
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/")
    route.side_effect = [
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "9002", "HasMoreItems": True},
            "Items": [userItem(9001, "demo.301"), userItem(9002, "demo.302")],
        }),
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "9003", "HasMoreItems": False},
            "Items": [userItem(9003, "demo.303")],
        }),
    ]
    users = list(api.iterUsers())
    assert [user["UserId"] for user in users] == [9001, 9002, 9003]
    assert route.call_count == 2
    # The server-supplied bookmark is echoed back, not one we computed
    assert route.calls[1].request.url.params["bookmark"] == "9002"


@respx.mock
def test_iterUsersFirstPageSendsNoBookmark(api):
    """A bookmark is the only parameter; userName would outrank it and break paging."""
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "9001", "HasMoreItems": False},
            "Items": [userItem(9001, "demo.301")],
        })
    )
    list(api.iterUsers())
    assert "bookmark" not in route.calls.last.request.url.params
    assert "userName" not in route.calls.last.request.url.params


@respx.mock
def test_iterUsersStopsOnEmptyFinalPage(api):
    """The end may arrive as an empty page rather than a flag on a full one."""
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/")
    route.side_effect = [
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "9001", "HasMoreItems": True},
            "Items": [userItem(9001, "demo.301")],
        }),
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "9001", "HasMoreItems": True},
            "Items": [],
        }),
    ]
    users = list(api.iterUsers())
    assert [user["UserId"] for user in users] == [9001]
    assert route.call_count == 2


@respx.mock
def test_iterUsersStopsWhenBookmarkDoesNotAdvance(api):
    """A repeated bookmark would otherwise loop forever over one page."""
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/")
    route.side_effect = [
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "stuck", "HasMoreItems": True},
            "Items": [userItem(9001, "demo.301")],
        }),
        httpx.Response(200, json={
            "PagingInfo": {"Bookmark": "stuck", "HasMoreItems": True},
            "Items": [userItem(9002, "demo.302")],
        }),
    ]
    users = list(api.iterUsers())
    # The second page repeated the bookmark, so the walk stopped rather
    # than requesting it a third time.
    assert [user["UserId"] for user in users] == [9001, 9002]
    assert route.call_count == 2


@respx.mock
def test_iterUsersStopsOnMissingBookmark(api):
    """HasMoreItems true with no bookmark leaves nowhere to go."""
    mockVersions()
    route = respx.get(f"{PROXY}/lp/1.47/users/").mock(
        return_value=httpx.Response(200, json={
            "PagingInfo": {"HasMoreItems": True},
            "Items": [userItem(9001, "demo.301")],
        })
    )
    assert [user["UserId"] for user in api.iterUsers()] == [9001]
    assert route.call_count == 1


def test_theToolNeverWritesToAnOrgUnit():
    """Courses are read, never modified. This tool must not delete a course.

    Guards the boundary rather than a behavior: every route that touches
    an org unit is a GET, and the only destructive call in the client
    targets a user. A change that added a course deletion, whether on
    purpose or by mistake, fails here.
    """
    import inspect
    import re

    from demostudent import d2lApi

    source = inspect.getsource(d2lApi)
    writes = re.findall(r'_request\(\s*"(POST|PUT|DELETE|PATCH)",\s*\n?\s*f?"([^"]+)"',
                        source)
    # Every write must target a user or an enrollment, never an org unit.
    for method, path in writes:
        assert "orgstructure" not in path, (
            f"{method} {path} writes to an org unit; this tool only reads courses")
        assert "recyclebin" not in path, (
            f"{method} {path} touches the course recycle bin")

    assert not re.search(r'_request\(\s*"DELETE"[^)]*orgstructure', source)
