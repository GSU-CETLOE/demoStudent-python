"""D2L Brightspace OAuth2 authentication.

Two interactive flows obtain the initial token pair:

- Browser flow: mkcert provides a locally-trusted HTTPS cert, a one-shot
  local HTTPS server receives the authorization code, and the system
  browser drives the login.
- Manual flow: for machines without mkcert or a browser. The user opens
  the printed authorization URL anywhere, then pastes the resulting
  redirect URL (which carries the code) back into the CLI.

After the first authorization, TokenManager keeps the access token fresh
using D2L's rotating refresh tokens; no browser is needed again unless
the refresh token is revoked or expires.
"""

import fcntl
import http.server
import json
import os
import secrets
import shutil
import ssl
import subprocess
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import AppConfig

AUTHORIZATION_URL = "https://auth.brightspace.com/oauth2/auth"
TOKEN_URL = "https://auth.brightspace.com/core/connect/token"

# Refresh this many seconds before the access token actually expires.
REFRESH_MARGIN_SECONDS = 60

CALLBACK_TIMEOUT_SECONDS = 300

CERT_FILENAME = "localhost.pem"
KEY_FILENAME = "localhost-key.pem"


class AuthError(Exception):
    """Raised when authentication fails or stored tokens are unusable."""


class MkcertUnavailableError(AuthError):
    """Raised when the mkcert binary cannot be found on PATH."""


@dataclass(frozen=True)
class TokenSet:
    """An access/refresh token pair with the access token's expiry time."""

    accessToken: str
    refreshToken: str
    expiresAt: float

    def isExpired(self) -> bool:
        """True when the access token is expired or about to expire."""
        return time.time() >= self.expiresAt - REFRESH_MARGIN_SECONDS


def buildAuthorizationUrl(config: AppConfig, state: str) -> str:
    """Authorization-code request URL the user must visit to sign in."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.clientId,
            "redirect_uri": config.redirectUri,
            "scope": config.scope,
            "state": state,
        }
    )
    return f"{AUTHORIZATION_URL}?{query}"


def parseRedirectUrl(redirectUrl: str, expectedState: str) -> str:
    """Extract the authorization code from the post-login redirect URL."""
    query = parse_qs(urlparse(redirectUrl.strip()).query)
    if "error" in query:
        raise AuthError(f"Authorization server returned error: {query['error'][0]}")
    state = query.get("state", [None])[0]
    if state != expectedState:
        raise AuthError("Redirect URL state does not match; possible tampering or stale URL.")
    code = query.get("code", [None])[0]
    if not code:
        raise AuthError("Redirect URL does not contain an authorization code.")
    return code


def _requestTokens(config: AppConfig, data: dict[str, str]) -> TokenSet:
    """POST to the token endpoint and translate the response into a TokenSet."""
    response = httpx.post(TOKEN_URL, data=data)
    if response.status_code != 200:
        raise AuthError(f"Token request failed ({response.status_code}): {response.text}")
    payload = response.json()
    return TokenSet(
        accessToken=payload["access_token"],
        refreshToken=payload["refresh_token"],
        expiresAt=time.time() + payload["expires_in"],
    )


def exchangeCodeForTokens(config: AppConfig, code: str) -> TokenSet:
    """Trade an authorization code for the initial token pair."""
    return _requestTokens(
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config.redirectUri,
            "client_id": config.clientId,
            "client_secret": config.clientSecret,
        },
    )


def refreshTokenSet(config: AppConfig, refreshToken: str) -> TokenSet:
    """Trade a refresh token for a new token pair (D2L rotates refresh tokens)."""
    return _requestTokens(
        config,
        {
            "grant_type": "refresh_token",
            "refresh_token": refreshToken,
            "client_id": config.clientId,
            "client_secret": config.clientSecret,
        },
    )


class TokenStore:
    """Persists the token pair as a user-only-readable JSON file.

    The file is shared state: any other demo-student process pointed at the
    same workingDir reads and writes it too. Writes are atomic so a reader
    never sees a half-written file, and exclusiveLock() serializes refreshes
    across processes.
    """

    def __init__(self, tokenPath: Path):
        self._tokenPath = tokenPath

    def load(self) -> TokenSet | None:
        """Return the stored tokens, or None if none have been saved yet."""
        if not self._tokenPath.is_file():
            return None
        try:
            payload = json.loads(self._tokenPath.read_text())
            return TokenSet(**payload)
        except (json.JSONDecodeError, TypeError) as error:
            raise AuthError(
                f"Stored token file {self._tokenPath} is corrupt: {error}. "
                "Delete it and run 'demo-student auth' again."
            ) from error

    def save(self, tokens: TokenSet) -> None:
        """Write tokens with 0600 permissions, creating the directory if needed.

        Writes to a private temporary file and renames it into place, so a
        concurrent reader sees either the old pair or the new one, never a
        partial write.
        """
        self._tokenPath.parent.mkdir(parents=True, exist_ok=True)
        tempPath = self._tokenPath.with_name(f"{self._tokenPath.name}.{os.getpid()}.tmp")
        tempPath.touch(mode=0o600, exist_ok=True)
        tempPath.chmod(0o600)
        tempPath.write_text(json.dumps(asdict(tokens)))
        tempPath.replace(self._tokenPath)  # atomic within the same directory

    @contextmanager
    def exclusiveLock(self) -> Iterator[None]:
        """Hold an inter-process lock covering a read-refresh-write cycle.

        D2L rotates refresh tokens, so two processes refreshing at once means
        the loser's token is invalidated mid-run. The lock lives in a sibling
        file and is released by the OS if the holder dies, so it cannot go
        stale.
        """
        lockPath = self._tokenPath.with_name(f"{self._tokenPath.name}.lock")
        lockPath.parent.mkdir(parents=True, exist_ok=True)
        with open(lockPath, "w") as lockFile:
            fcntl.flock(lockFile, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lockFile, fcntl.LOCK_UN)


class TokenManager:
    """Thread-safe provider of a valid access token for API calls.

    Refreshes ahead of expiry and after a 401, ensuring that concurrent
    workers trigger at most one refresh, since D2L refresh tokens are
    single-use. The threading lock only covers this process, so refreshes
    also take TokenStore.exclusiveLock() to serialize against any other
    demo-student process sharing the same working directory (a manual run
    alongside the cron job, say).
    """

    def __init__(self, config: AppConfig, store: TokenStore):
        self._config = config
        self._store = store
        self._lock = threading.Lock()
        self._tokens = store.load()

    def getAccessToken(self) -> str:
        """A currently-valid access token, refreshing it first if needed."""
        with self._lock:
            tokens = self._requireTokens()
            if tokens.isExpired():
                tokens = self._refreshLocked(tokens)
            return tokens.accessToken

    def handleUnauthorized(self, failedAccessToken: str) -> str:
        """Refresh after a 401; concurrent callers reusing the same dead token
        get the already-refreshed token instead of triggering another refresh."""
        with self._lock:
            tokens = self._requireTokens()
            if tokens.accessToken == failedAccessToken:
                tokens = self._refreshLocked(tokens)
            return tokens.accessToken

    def _requireTokens(self) -> TokenSet:
        if self._tokens is None:
            raise AuthError("No stored tokens. Run 'demo-student auth' first.")
        return self._tokens

    def _refreshLocked(self, tokens: TokenSet) -> TokenSet:
        """Exchange the refresh token, tolerating a concurrent refresher.

        Because D2L invalidates a refresh token the moment it is used, another
        process that refreshed first has already killed the one held here. In
        that case the pair it wrote is on disk, so adopt that instead of
        failing the run.
        """
        with self._store.exclusiveLock():
            stored = self._store.load()
            if stored is not None and stored.refreshToken != tokens.refreshToken:
                # Another process refreshed while this one waited for the lock.
                self._tokens = stored
                if not stored.isExpired():
                    return stored
                tokens = stored
            try:
                fresh = refreshTokenSet(self._config, tokens.refreshToken)
            except AuthError:
                # A process that does not take the lock can still beat us here.
                recovered = self._store.load()
                if recovered is None or recovered.refreshToken == tokens.refreshToken:
                    raise  # nothing newer on disk, so the failure is real
                self._tokens = recovered
                if not recovered.isExpired():
                    return recovered
                fresh = refreshTokenSet(self._config, recovered.refreshToken)
            self._store.save(fresh)
            self._tokens = fresh
            return fresh


def findMkcert() -> str | None:
    """Path to the mkcert binary, or None if it is not installed."""
    return shutil.which("mkcert")


def ensureLocalhostCert(workingDir: Path) -> tuple[Path, Path]:
    """Create (or reuse) a locally-trusted localhost certificate via mkcert."""
    mkcertPath = findMkcert()
    if mkcertPath is None:
        raise MkcertUnavailableError(
            "mkcert is not installed or not on PATH. "
            "Install it (https://github.com/FiloSottile/mkcert) or use 'demo-student auth --manual'."
        )
    certFile = workingDir / CERT_FILENAME
    keyFile = workingDir / KEY_FILENAME
    if not (certFile.is_file() and keyFile.is_file()):
        workingDir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [mkcertPath, "-cert-file", str(certFile), "-key-file", str(keyFile),
             "localhost", "127.0.0.1"],
            check=True,
            capture_output=True,
        )
    return certFile, keyFile


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the OAuth redirect and shows a close-this-window page."""

    receivedPath: str | None = None

    def do_GET(self) -> None:  # noqa: N802 (name fixed by BaseHTTPRequestHandler)
        type(self).receivedPath = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h2>Sign-in complete.</h2>"
                         b"<p>You can close this window and return to the terminal.</p></body></html>")

    def log_message(self, format: str, *args: object) -> None:
        pass  # keep CLI output clean


def _waitForCallback(config: AppConfig, certFile: Path, keyFile: Path) -> str:
    """Serve HTTPS on the redirect URI's port until the OAuth redirect arrives."""
    redirect = urlparse(config.redirectUri)
    port = redirect.port or 443
    sslContext = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sslContext.load_cert_chain(certfile=str(certFile), keyfile=str(keyFile))

    class Handler(_CallbackHandler):
        receivedPath = None

    server = http.server.HTTPServer(("localhost", port), Handler)
    server.socket = sslContext.wrap_socket(server.socket, server_side=True)
    server.timeout = CALLBACK_TIMEOUT_SECONDS
    try:
        deadline = time.time() + CALLBACK_TIMEOUT_SECONDS
        while Handler.receivedPath is None:
            if time.time() > deadline:
                raise AuthError("Timed out waiting for the OAuth redirect. Try again.")
            server.handle_request()
    finally:
        server.server_close()
    return f"{config.redirectUri.rstrip('/')}?{urlparse(Handler.receivedPath).query}"


def runBrowserFlow(config: AppConfig, printFunc: Callable[[str], None] = print) -> TokenSet:
    """Full browser-based authorization using mkcert and a local HTTPS server."""
    certFile, keyFile = ensureLocalhostCert(config.workingDir)
    state = secrets.token_urlsafe(24)
    authUrl = buildAuthorizationUrl(config, state)
    printFunc("Opening your browser to sign in to D2L...")
    printFunc(f"If it does not open, visit:\n{authUrl}")
    webbrowser.open(authUrl)
    redirectUrl = _waitForCallback(config, certFile, keyFile)
    code = parseRedirectUrl(redirectUrl, expectedState=state)
    return exchangeCodeForTokens(config, code)


def runManualFlow(
    config: AppConfig,
    inputFunc: Callable[[str], str] | None = None,
    printFunc: Callable[[str], None] = print,
) -> TokenSet:
    """Authorization without a local browser or mkcert.

    The user opens the printed URL on any device, signs in, and pastes the
    full redirect URL back here (the browser will show a connection error
    at the redirect step - that is expected; the code is in the URL bar).
    """
    if inputFunc is None:
        inputFunc = input  # resolved late so tests can substitute stdin
    state = secrets.token_urlsafe(24)
    printFunc("Open this URL in a browser on any device and sign in to D2L:")
    printFunc(buildAuthorizationUrl(config, state))
    printFunc(
        "\nAfter signing in, the browser will try to load the redirect URI and fail - "
        "that is expected. Copy the FULL URL from the browser's address bar."
    )
    redirectUrl = inputFunc("Paste the redirect URL here: ")
    code = parseRedirectUrl(redirectUrl, expectedState=state)
    return exchangeCodeForTokens(config, code)


def obtainTokensInteractively(config: AppConfig, manual: bool = False) -> TokenSet:
    """Run the appropriate interactive flow and persist the resulting tokens."""
    if manual or findMkcert() is None:
        if not manual:
            print("mkcert not found; falling back to the manual flow.")
        tokens = runManualFlow(config)
    else:
        tokens = runBrowserFlow(config)
    TokenStore(config.tokenPath).save(tokens)
    return tokens
