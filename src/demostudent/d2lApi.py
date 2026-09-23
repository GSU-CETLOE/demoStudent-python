"""Thread-safe D2L Brightspace API client.

Wraps a shared httpx.Client (safe for concurrent use) and handles the
cross-cutting concerns: bearer auth, one refresh-and-retry on 401,
Retry-After backoff on 429, and product-version negotiation via
GET /d2l/api/versions/ so no API versions are hardcoded.
"""

import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .auth import TokenManager
from .config import AppConfig

RATE_LIMIT_MAX_RETRIES = 5
DEFAULT_RETRY_AFTER_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 30.0


class D2lApiError(Exception):
    """An API call failed after retries; carries the HTTP status and body."""

    def __init__(self, message: str, httpStatus: int | None = None):
        super().__init__(message)
        self.httpStatus = httpStatus


class D2lApiClient:
    """Client for the D2L routes this application needs."""

    def __init__(
        self,
        config: AppConfig,
        tokenManager: TokenManager,
        httpClient: httpx.Client | None = None,
        sleepFunc: Callable[[float], None] = time.sleep,
    ):
        self._config = config
        self._tokenManager = tokenManager
        self._baseUrl = f"https://{config.host}"
        self._http = httpClient or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._sleepFunc = sleepFunc
        self._versionLock = threading.Lock()
        self._lpVersion: str | None = None
        self._leVersion: str | None = None

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    # --- version negotiation ---

    def _negotiateVersions(self) -> None:
        response = self._request("GET", "/d2l/api/versions/")
        products = {entry["ProductCode"]: entry["LatestVersion"] for entry in response.json()}
        try:
            self._lpVersion = products["lp"]
            self._leVersion = products["le"]
        except KeyError as error:
            raise D2lApiError(f"/d2l/api/versions/ did not report product {error}") from error

    def _lp(self) -> str:
        """Latest supported Learning Platform API version, fetched once."""
        with self._versionLock:
            if self._lpVersion is None:
                self._negotiateVersions()
            assert self._lpVersion is not None
            return self._lpVersion

    # --- request plumbing ---

    def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        allowNotFound: bool = False,
        allowRedirect: bool = False,
    ) -> httpx.Response:
        """Issue a request with auth, 401 refresh-retry, and 429 backoff."""
        accessToken = self._tokenManager.getAccessToken()
        refreshed = False
        rateLimitRetries = 0
        while True:
            response = self._http.request(
                method,
                f"{self._baseUrl}{path}",
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {accessToken}"},
            )
            if response.status_code == 401 and not refreshed:
                accessToken = self._tokenManager.handleUnauthorized(accessToken)
                refreshed = True
                continue
            if response.status_code == 429 and rateLimitRetries < RATE_LIMIT_MAX_RETRIES:
                rateLimitRetries += 1
                retryAfter = response.headers.get("Retry-After")
                self._sleepFunc(float(retryAfter) if retryAfter else DEFAULT_RETRY_AFTER_SECONDS)
                continue
            if response.status_code == 404 and allowNotFound:
                return response
            if allowRedirect and response.status_code in (301, 302, 303, 307, 308):
                return response
            if response.is_success:
                return response
            raise D2lApiError(
                f"{method} {path} failed with HTTP {response.status_code}: {response.text}",
                httpStatus=response.status_code,
            )

    # --- routes ---

    def whoAmI(self) -> dict[str, Any]:
        """Identity of the authorized user; cheap credential sanity check."""
        return self._request("GET", f"/d2l/api/lp/{self._lp()}/users/whoami").json()

    def getOrgUnit(self, orgUnitId: int) -> dict[str, Any]:
        """Org unit properties (name, type, code)."""
        return self._request("GET", f"/d2l/api/lp/{self._lp()}/orgstructure/{orgUnitId}").json()

    def iterDescendants(self, orgUnitId: int, ouTypeId: int) -> Iterator[dict[str, Any]]:
        """All descendants of an org unit of one type, following paging bookmarks."""
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {"ouTypeId": ouTypeId}
            if bookmark:
                params["bookmark"] = bookmark
            response = self._request(
                "GET",
                f"/d2l/api/lp/{self._lp()}/orgstructure/{orgUnitId}/descendants/paged/",
                params=params,
            )
            page = response.json()
            yield from page["Items"]
            pagingInfo = page["PagingInfo"]
            if not pagingInfo.get("HasMoreItems") or not page["Items"]:
                return
            bookmark = pagingInfo.get("Bookmark")

    def getUserByUsername(self, userName: str) -> dict[str, Any] | None:
        """Exact-match user lookup; None when the user does not exist."""
        response = self._request(
            "GET",
            f"/d2l/api/lp/{self._lp()}/users/",
            params={"userName": userName},
            allowNotFound=True,
        )
        if response.status_code == 404:
            return None
        return response.json()

    def iterUsers(self) -> Iterator[dict[str, Any]]:
        """Every user the calling account may see, following paging bookmarks.

        Sends nothing but the bookmark. D2L picks among orgDefinedId,
        userName, externalEmail and bookmark in that fixed order no
        matter what order the URL puts them in, so adding any of the
        first three would outrank the bookmark and re-return page one
        forever.

        The bookmark D2L hands back happens to be a UserId. It is echoed
        as given rather than recomputed from the last item, since the
        server's value is the authoritative one.

        Caution: D2L filters this listing by what the calling role may
        see, and mentions that only in its documentation. A short list
        arrives as a clean, well-formed result with nothing to say
        anything was withheld, so callers must not treat what comes back
        as a complete census.
        """
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {"bookmark": bookmark} if bookmark else {}
            response = self._request(
                "GET", f"/d2l/api/lp/{self._lp()}/users/", params=params)
            page = response.json()
            items = page["Items"]
            yield from items

            pagingInfo = page["PagingInfo"]
            if not pagingInfo.get("HasMoreItems") or not items:
                return

            nextBookmark = pagingInfo.get("Bookmark")
            # A bookmark that does not move would re-request the same page
            # for as long as HasMoreItems stays true.
            if not nextBookmark or nextBookmark == bookmark:
                return
            bookmark = nextBookmark

    # --- Brightspace Data Sets ---

    def _iterObjectListPages(self, url: str) -> Iterator[dict[str, Any]]:
        """Walk an ObjectListPage, following its Next links.

        Data Sets routes page differently from the rest of the API: each
        response carries an absolute Next URL rather than a bookmark to
        feed back as a parameter.
        """
        while url:
            path = url[len(self._baseUrl):] if url.startswith(self._baseUrl) else url
            page = self._request("GET", path).json()
            yield from page.get("Objects", [])
            url = page.get("Next") or ""

    def findDataSet(self, name: str) -> dict[str, Any] | None:
        """The schema and full-extract plugin ids for a named data set.

        Returns None when Data Sets is unavailable. D2L answers 403
        whether the tool is switched off for the tenant, the role lacks
        Can Access Brightspace Data Sets, or the token lacks the
        datasets:bds:read scope, and publishes nothing to tell those
        apart. Callers treat any of them the same way: use another
        source.

        The name lives on the plugin rather than the data set, so this
        matches on the full extract's Name.
        """
        try:
            dataSets = self._iterObjectListPages(
                f"{self._baseUrl}/d2l/api/lp/{self._lp()}/datasets/bds")
            for dataSet in dataSets:
                full = dataSet.get("Full")
                if full and full.get("Name") == name:
                    return {"schemaId": dataSet["SchemaId"],
                            "pluginId": full["PluginId"],
                            "name": full["Name"]}
        except D2lApiError as error:
            if error.httpStatus in (403, 404):
                return None
            raise
        return None

    def latestExtract(self, schemaId: str, pluginId: str) -> dict[str, Any] | None:
        """The newest full extract for a data set plugin, or None.

        D2L keeps a previous extract for up to 14 days after generating a
        new one, so the listing routinely holds more than one and the
        newest CreatedDate wins. Differentials are skipped: they record
        changes since the last differential, which is useless without a
        full import to apply them to.
        """
        extracts = [
            extract for extract in self._iterObjectListPages(
                f"{self._baseUrl}/d2l/api/lp/{self._lp()}/datasets/bds/"
                f"{schemaId}/plugins/{pluginId}/extracts")
            if extract.get("BdsType") == "Full"
        ]
        if not extracts:
            return None
        newest = max(extracts, key=lambda extract: extract["CreatedDate"])
        return {"createdDate": newest["CreatedDate"],
                "downloadLink": newest["DownloadLink"],
                "downloadSize": newest.get("DownloadSize"),
                "version": newest.get("Version")}

    def downloadExtract(self, downloadLink: str, target: Path) -> Path:
        """Download an extract to a local file, following D2L's redirect.

        The download may answer 302 with a pre-authorized URL on separate
        storage. The bearer token must not follow it there: that host is
        not D2L, and the URL already carries its own authorization. The
        redirect is handled here rather than by httpx so the header can
        be dropped on the cross-host hop.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        path = (downloadLink[len(self._baseUrl):]
                if downloadLink.startswith(self._baseUrl) else downloadLink)

        response = self._request("GET", path, allowRedirect=True)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers["Location"]
            with self._http.stream("GET", location, follow_redirects=True) as streamed:
                if not streamed.is_success:
                    streamed.read()
                    raise D2lApiError(
                        f"Extract download failed with HTTP {streamed.status_code}",
                        httpStatus=streamed.status_code)
                with target.open("wb") as handle:
                    for chunk in streamed.iter_bytes():
                        handle.write(chunk)
            return target

        target.write_bytes(response.content)
        return target

    def getEnrollment(self, orgUnitId: int, userId: int) -> dict[str, Any] | None:
        """A user's enrollment in an org unit; None when not enrolled.

        Asks from the user side rather than the org unit side. Both need
        only enrollment:orgunit:read, but the org unit side
        (/enrollments/orgUnits/{orgUnitId}/users/{userId}) returns 403 in
        any course the service account cannot reach, which aborts the
        course; the user side returns 200 and simply omits those courses,
        so the run keeps going. Note the tradeoff: an omitted course reads
        back as "not enrolled" rather than failing, so if the account lacks
        access the check is silently wrong and the 403 surfaces on the
        enrollment write instead. See "Course Access Options" in README.md.
        A demo student belongs to one course, so this list stays short.
        """
        bookmark: str | None = None
        while True:
            params: dict[str, Any] = {"bookmark": bookmark} if bookmark else {}
            response = self._request(
                "GET",
                f"/d2l/api/lp/{self._lp()}/enrollments/users/{userId}/orgUnits/",
                params=params,
                allowNotFound=True,
            )
            if response.status_code == 404:
                return None
            page = response.json()
            for item in page["Items"]:
                if item["OrgUnit"]["Id"] == orgUnitId:
                    return {"OrgUnitId": orgUnitId, "UserId": userId,
                            "RoleId": item["Role"]["Id"]}
            pagingInfo = page["PagingInfo"]
            if not pagingInfo.get("HasMoreItems") or not page["Items"]:
                return None
            bookmark = pagingInfo.get("Bookmark")

    def createUser(
        self,
        userName: str,
        firstName: str,
        lastName: str,
        externalEmail: str,
        roleId: int,
    ) -> dict[str, Any]:
        """Create a new user (no creation email is sent)."""
        response = self._request(
            "POST",
            f"/d2l/api/lp/{self._lp()}/users/",
            json={
                "OrgDefinedId": "",
                "FirstName": firstName,
                "MiddleName": "",
                "LastName": lastName,
                "ExternalEmail": externalEmail,
                "UserName": userName,
                "RoleId": roleId,
                "IsActive": True,
                "SendCreationEmail": False,
            },
        )
        return response.json()

    def deleteUser(self, userId: int) -> None:
        """Permanently delete a user.

        D2L gets a deleted user back only through a support request,
        within 30 days, and only if no account has reused their username
        since. Be sure before calling this.
        """
        self._request("DELETE", f"/d2l/api/lp/{self._lp()}/users/{userId}")

    def setUserActivation(self, userId: int, isActive: bool) -> None:
        """Activate or deactivate a user, leaving the rest of the record alone.

        This route takes only the flag. PUT /users/{userId} would do the
        job too, but its body replaces every field on the user, so it is
        not used here.
        """
        self._request(
            "PUT",
            f"/d2l/api/lp/{self._lp()}/users/{userId}/activation",
            json={"IsActive": isActive},
        )

    def enrollUser(self, orgUnitId: int, userId: int, roleId: int) -> dict[str, Any]:
        """Enroll a user in an org unit (updates the role in place if enrolled)."""
        response = self._request(
            "POST",
            f"/d2l/api/lp/{self._lp()}/enrollments/",
            json={
                "OrgUnitId": orgUnitId,
                "UserId": userId,
                "RoleId": roleId,
                "SendEnrollmentEmail": False,
            },
        )
        return response.json()
