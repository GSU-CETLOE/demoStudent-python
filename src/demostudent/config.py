"""Configuration loading for the demo student tool.

Reads a TOML config file (see config.example.toml), applies defaults,
and allows the client secret to be overridden via the
DEMO_STUDENT_CLIENT_SECRET environment variable.

D2L API calls go through apiProxy2 when proxyUrl is set, and straight
to https://{host}/d2l/api when it is not. host names the tenant either
way: OAuth is issued for it, and D2L's own absolute links point at it.

proxyKey turns on apiProxy2's proxy authentication. The proxy then
holds the D2L token and this tool sends the key in its place, so the
OAuth settings (clientId, clientSecret, redirectUri) are not needed.
"""

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

CLIENT_SECRET_ENV_VAR = "DEMO_STUDENT_CLIENT_SECRET"

DB_SUBDIR = "db"
DB_FILENAME = "demoStudent.db"
TOKEN_SUBDIR = "token"
TOKEN_FILENAME = "token.json"


class ConfigError(Exception):
    """Raised when the config file is missing, malformed, or incomplete."""


@dataclass(frozen=True)
class AppConfig:
    """All runtime settings for the application."""

    host: str
    workingDir: Path
    # Empty when proxyKey is set: apiProxy2 signs in to D2L then, and this
    # tool has no OAuth2 application of its own.
    clientId: str = ""
    clientSecret: str = ""
    redirectUri: str = ""
    proxyUrl: str | None = None
    proxyKey: str | None = None
    proxyUser: str | None = None
    scope: str = "core:*:*"
    roleId: int = 783
    orgOu: int = 1070555
    courseOfferingOuType: int = 3
    usernamePrefix: str = "demo"
    firstName: str = "Demo"
    lastName: str = "Student"
    externalEmailDomain: str = "student.gsu.edu"
    maxWorkers: int = 8
    logDir: str = "logs"
    logFilePrefix: str = "demoStudent"

    @property
    def dbPath(self) -> Path:
        """Path of the SQLite database inside the working directory."""
        return self.workingDir / DB_SUBDIR / DB_FILENAME

    @property
    def tokenPath(self) -> Path:
        """Path of the OAuth token store inside the working directory."""
        return self.workingDir / TOKEN_SUBDIR / TOKEN_FILENAME

    def demoUsername(self, orgUnitId: int) -> str:
        """Unique demo student username for a course offering."""
        return f"{self.usernamePrefix}.{orgUnitId}"

    def demoEmail(self, orgUnitId: int) -> str:
        """External email address for a course's demo student."""
        return f"{self.demoUsername(orgUnitId)}@{self.externalEmailDomain}"


def loadConfig(configPath: Path | str) -> AppConfig:
    """Load and validate the application config from a TOML file.

    A relative workingDir is resolved against the config file's directory.
    Raises ConfigError with a clear message on any problem.
    """
    configPath = Path(configPath)
    if not configPath.is_file():
        raise ConfigError(f"Config file not found: {configPath}")

    try:
        raw = tomllib.loads(configPath.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid TOML in {configPath}: {error}") from error

    knownKeys = {field.name for field in fields(AppConfig)}
    unknownKeys = set(raw) - knownKeys
    if unknownKeys:
        raise ConfigError(f"Unknown config keys in {configPath}: {', '.join(sorted(unknownKeys))}")

    envSecret = os.environ.get(CLIENT_SECRET_ENV_VAR)
    if envSecret:
        raw["clientSecret"] = envSecret

    raw["proxyKey"] = raw.get("proxyKey") or None
    raw["proxyUser"] = raw.get("proxyUser") or None

    requiredKeys = ["host"]
    if raw["proxyKey"] is None:
        requiredKeys += ["clientId", "clientSecret", "redirectUri"]
    missing = [key for key in requiredKeys if not raw.get(key)]
    if missing:
        secretHint = (f" (clientSecret may also be set via {CLIENT_SECRET_ENV_VAR})"
                      if "clientSecret" in missing else "")
        raise ConfigError(f"Missing required config values: {', '.join(missing)}{secretHint}")

    if raw.get("proxyUrl"):
        if not raw["proxyUrl"].startswith(("http://", "https://")):
            raise ConfigError(
                f"proxyUrl must start with http:// or https://, got {raw['proxyUrl']!r}")
        raw["proxyUrl"] = raw["proxyUrl"].rstrip("/")
    else:
        raw["proxyUrl"] = None

    if raw["proxyKey"] and raw["proxyUrl"] is None:
        raise ConfigError(
            "proxyKey is set without proxyUrl. The key is only for apiProxy2, "
            "so set proxyUrl to the proxy that issued it")
    if raw["proxyUser"] and raw["proxyKey"] is None:
        raise ConfigError(
            "proxyUser is set without proxyKey. It names the account for "
            "apiProxy2's proxy authentication, which needs the key")

    workingDir = Path(raw.pop("workingDir", "working"))
    if not workingDir.is_absolute():
        workingDir = configPath.parent / workingDir

    return AppConfig(workingDir=workingDir, **raw)
