"""Tests for OAuth2 flows, token storage, and token refresh."""

import json
import os
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from demostudent import auth
from demostudent.auth import (
    AUTHORIZATION_URL,
    TOKEN_URL,
    AuthError,
    MkcertUnavailableError,
    TokenManager,
    TokenSet,
    TokenStore,
    buildAuthorizationUrl,
    ensureLocalhostCert,
    exchangeCodeForTokens,
    parseRedirectUrl,
    refreshTokenSet,
    runManualFlow,
)
from demostudent.config import AppConfig


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
    )


def tokenResponse(accessToken="access-1", refreshToken="refresh-1", expiresIn=3600):
    return httpx.Response(
        200,
        json={
            "access_token": accessToken,
            "refresh_token": refreshToken,
            "expires_in": expiresIn,
            "token_type": "Bearer",
        },
    )


# --- authorization URL and redirect parsing ---


def test_buildAuthorizationUrl(config):
    url = buildAuthorizationUrl(config, state="abc123")
    parsed = urlparse(url)
    assert url.startswith(AUTHORIZATION_URL)
    query = parse_qs(parsed.query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://localhost:8443/callback"]
    assert query["scope"] == ["core:*:*"]
    assert query["state"] == ["abc123"]


def test_parseRedirectUrlExtractsCode():
    code = parseRedirectUrl(
        "https://localhost:8443/callback?code=the-code&state=abc123", expectedState="abc123"
    )
    assert code == "the-code"


def test_parseRedirectUrlRejectsBadState():
    with pytest.raises(AuthError, match="state"):
        parseRedirectUrl(
            "https://localhost:8443/callback?code=the-code&state=WRONG", expectedState="abc123"
        )


def test_parseRedirectUrlRejectsMissingCode():
    with pytest.raises(AuthError, match="code"):
        parseRedirectUrl("https://localhost:8443/callback?state=abc123", expectedState="abc123")


def test_parseRedirectUrlReportsProviderError():
    with pytest.raises(AuthError, match="access_denied"):
        parseRedirectUrl(
            "https://localhost:8443/callback?error=access_denied&state=abc123",
            expectedState="abc123",
        )


# --- token endpoint calls ---


@respx.mock
def test_exchangeCodeForTokens(config):
    route = respx.post(TOKEN_URL).mock(return_value=tokenResponse())
    tokens = exchangeCodeForTokens(config, "the-code")
    assert tokens.accessToken == "access-1"
    assert tokens.refreshToken == "refresh-1"
    assert tokens.expiresAt > time.time()
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["authorization_code"]
    assert sent["code"] == ["the-code"]
    assert sent["redirect_uri"] == ["https://localhost:8443/callback"]
    assert sent["client_id"] == ["client-id"]
    assert sent["client_secret"] == ["client-secret"]


@respx.mock
def test_refreshTokenSet(config):
    route = respx.post(TOKEN_URL).mock(
        return_value=tokenResponse(accessToken="access-2", refreshToken="refresh-2")
    )
    tokens = refreshTokenSet(config, "refresh-1")
    assert tokens.accessToken == "access-2"
    assert tokens.refreshToken == "refresh-2"
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["grant_type"] == ["refresh_token"]
    assert sent["refresh_token"] == ["refresh-1"]


@respx.mock
def test_tokenEndpointErrorRaisesAuthError(config):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(AuthError, match="invalid_grant"):
        refreshTokenSet(config, "stale-refresh")


# --- token store ---


def test_tokenStoreRoundTripAndPermissions(tmp_path):
    store = TokenStore(tmp_path / "working" / "token.json")
    assert store.load() is None
    tokens = TokenSet(accessToken="a", refreshToken="r", expiresAt=123.0)
    store.save(tokens)
    assert store.load() == tokens
    if sys.platform != "win32":  # Windows has no POSIX mode bits to assert on
        mode = os.stat(tmp_path / "working" / "token.json").st_mode & 0o777
        assert mode == 0o600


def test_tokenStoreRejectsCorruptFile(tmp_path):
    path = tmp_path / "token.json"
    path.write_text("not json")
    store = TokenStore(path)
    with pytest.raises(AuthError, match="token"):
        store.load()


def test_tokenStoreLeavesNoTemporaryFileBehind(tmp_path):
    store = TokenStore(tmp_path / "working" / "token.json")
    store.save(TokenSet(accessToken="a", refreshToken="r", expiresAt=123.0))
    assert [p.name for p in (tmp_path / "working").iterdir() if p.suffix == ".tmp"] == []


def test_exclusiveLockIsReentrantAcrossCalls(tmp_path):
    """Portable check: acquire and release repeatedly without error."""
    store = TokenStore(tmp_path / "working" / "token.json")
    for _ in range(3):
        with store.exclusiveLock():
            pass
    assert (tmp_path / "working" / "token.json.lock").is_file()


def test_fileLockUsesMsvcrtWhenFlockIsAbsent(tmp_path, monkeypatch):
    """The Windows branch: retry until granted, then unlock. Verified here
    because CI and development are POSIX-only."""
    calls = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def locking(self, fileno, mode, nbytes):
            calls.append(mode)
            if mode == self.LK_NBLCK and calls.count(self.LK_NBLCK) == 1:
                raise OSError("held by another process")

    monkeypatch.setattr(auth, "fcntl", None)
    monkeypatch.setattr(auth, "msvcrt", FakeMsvcrt())
    monkeypatch.setattr(auth, "LOCK_POLL_SECONDS", 0)

    store = TokenStore(tmp_path / "working" / "token.json")
    with store.exclusiveLock():
        pass

    assert calls == [FakeMsvcrt.LK_NBLCK, FakeMsvcrt.LK_NBLCK, FakeMsvcrt.LK_UNLCK]


def test_fileLockDegradesWhenNoPrimitiveExists(tmp_path, monkeypatch):
    """An exotic platform loses the lock, not the ability to run: the
    invalid_grant recovery still covers correctness."""
    monkeypatch.setattr(auth, "fcntl", None)
    monkeypatch.setattr(auth, "msvcrt", None)

    store = TokenStore(tmp_path / "working" / "token.json")
    with store.exclusiveLock():
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="flock specifics; Windows uses msvcrt")
def test_exclusiveLockExcludesOtherHolders(tmp_path):
    import fcntl

    store = TokenStore(tmp_path / "working" / "token.json")
    lockPath = tmp_path / "working" / "token.json.lock"

    with store.exclusiveLock():
        with open(lockPath) as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    with open(lockPath) as contender:  # released on exit
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender, fcntl.LOCK_UN)


# --- token manager ---


@respx.mock
def test_tokenManagerReturnsValidTokenWithoutRefresh(config, tmp_path):
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="valid", refreshToken="r", expiresAt=time.time() + 3600))
    manager = TokenManager(config, store)
    assert manager.getAccessToken() == "valid"
    assert not respx.calls  # no network traffic


@respx.mock
def test_tokenManagerRefreshesExpiredTokenAndPersists(config):
    respx.post(TOKEN_URL).mock(
        return_value=tokenResponse(accessToken="fresh", refreshToken="rotated")
    )
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="stale", refreshToken="old", expiresAt=time.time() - 10))
    manager = TokenManager(config, store)
    assert manager.getAccessToken() == "fresh"
    persisted = store.load()
    assert persisted is not None
    assert persisted.refreshToken == "rotated"  # rotated refresh token saved immediately


@respx.mock
def test_tokenManagerRefreshesOnlyOnceUnderConcurrency(config):
    route = respx.post(TOKEN_URL).mock(
        return_value=tokenResponse(accessToken="fresh", refreshToken="rotated")
    )
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="stale", refreshToken="old", expiresAt=time.time() - 10))
    manager = TokenManager(config, store)

    results = []
    barrier = threading.Barrier(8)

    def fetch():
        barrier.wait()
        results.append(manager.getAccessToken())

    threads = [threading.Thread(target=fetch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["fresh"] * 8
    assert route.call_count == 1


@respx.mock
def test_tokenManagerHandleUnauthorizedRefreshesOncePerToken(config):
    route = respx.post(TOKEN_URL).mock(
        return_value=tokenResponse(accessToken="fresh", refreshToken="rotated")
    )
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="revoked", refreshToken="old", expiresAt=time.time() + 3600))
    manager = TokenManager(config, store)
    # Several threads report the same dead token; only one refresh should happen
    assert manager.handleUnauthorized("revoked") == "fresh"
    assert manager.handleUnauthorized("revoked") == "fresh"
    assert route.call_count == 1


@respx.mock
def test_refreshAdoptsPairWrittenByAnotherProcess(config):
    """Another demo-student process refreshed first; use what it left on disk."""
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="old", refreshToken="r1", expiresAt=time.time() - 1))
    manager = TokenManager(config, store)
    store.save(TokenSet(accessToken="theirs", refreshToken="r2",
                        expiresAt=time.time() + 3600))

    assert manager.getAccessToken() == "theirs"
    assert not respx.calls  # no refresh needed, the disk already had a fresh pair


@respx.mock
def test_refreshRecoversWhenOurRefreshTokenWasInvalidated(config):
    """D2L rotates refresh tokens, so a racing process kills the one held here."""
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="old", refreshToken="r1", expiresAt=time.time() - 1))
    manager = TokenManager(config, store)

    def rejectAfterOtherProcessRotated(request):
        store.save(TokenSet(accessToken="theirs", refreshToken="r2",
                            expiresAt=time.time() + 3600))
        return httpx.Response(400, json={"error": "invalid_grant"})

    respx.post(TOKEN_URL).mock(side_effect=rejectAfterOtherProcessRotated)
    assert manager.getAccessToken() == "theirs"


@respx.mock
def test_refreshRaisesWhenNoNewerPairOnDisk(config):
    """A genuine invalid_grant still surfaces rather than being swallowed."""
    store = TokenStore(config.tokenPath)
    store.save(TokenSet(accessToken="old", refreshToken="r1", expiresAt=time.time() - 1))
    manager = TokenManager(config, store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(AuthError, match="invalid_grant"):
        manager.getAccessToken()


def test_tokenManagerWithoutStoredTokensRaises(config):
    manager = TokenManager(config, TokenStore(config.tokenPath))
    with pytest.raises(AuthError, match="demo-student auth"):
        manager.getAccessToken()


# --- manual flow ---


@respx.mock
def test_runManualFlow(config):
    respx.post(TOKEN_URL).mock(return_value=tokenResponse())
    printed = []

    def fakeInput(prompt):
        authUrl = next(line for line in printed if line.startswith(AUTHORIZATION_URL))
        state = parse_qs(urlparse(authUrl).query)["state"][0]
        return f"https://localhost:8443/callback?code=manual-code&state={state}"

    tokens = runManualFlow(config, inputFunc=fakeInput, printFunc=printed.append)
    assert tokens.accessToken == "access-1"


# --- mkcert ---


def test_ensureLocalhostCertWithoutMkcertRaises(config, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(MkcertUnavailableError):
        ensureLocalhostCert(config.workingDir)
