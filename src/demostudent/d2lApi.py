"""Thread-safe D2L Brightspace API client.

Wraps a shared httpx.Client (safe for concurrent use) and handles the
cross-cutting concerns: bearer auth, one refresh-and-retry on 401,
Retry-After backoff on 429, and product-version negotiation via
GET /d2l/api/versions/ so no API versions are hardcoded.
"""

import threading
import time
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

        D2L recovers a deleted user only through a support request, within
        30 days, and only if no account has since reused their username.
        Callers must be certain before reaching this.
        """
        self._request("DELETE", f"/d2l/api/lp/{self._lp()}/users/{userId}")

    def setUserActivation(self, userId: int, isActive: bool) -> None:
        """Activate or deactivate a user without touching the rest of the record.

        The dedicated activation route takes only this flag. The general
        user update route (PUT /users/{userId}) would replace every field
        on the user with whatever the request body carried, so it is not
        used for this.
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
