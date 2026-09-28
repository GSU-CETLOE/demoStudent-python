"""Configuration loading for the demo student tool.

Reads a TOML config file (see config.example.toml), applies defaults,
and allows the client secret to be overridden via the
DEMO_STUDENT_CLIENT_SECRET environment variable.

Every D2L API call goes through apiProxy2 at proxyUrl. host still names
the tenant: OAuth is issued for it, and D2L's own absolute links point
at it.
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
    proxyUrl: str
    clientId: str
    clientSecret: str
    redirectUri: str
    workingDir: Path
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

    missing = [key for key in ("host", "proxyUrl", "clientId", "clientSecret", "redirectUri")
               if not raw.get(key)]
    if missing:
        raise ConfigError(
            f"Missing required config values: {', '.join(missing)} "
            f"(clientSecret may also be set via {CLIENT_SECRET_ENV_VAR})"
        )

    if not raw["proxyUrl"].startswith(("http://", "https://")):
        raise ConfigError(
            f"proxyUrl must start with http:// or https://, got {raw['proxyUrl']!r}")
    raw["proxyUrl"] = raw["proxyUrl"].rstrip("/")

    workingDir = Path(raw.pop("workingDir", "working"))
    if not workingDir.is_absolute():
        workingDir = configPath.parent / workingDir

    return AppConfig(workingDir=workingDir, **raw)
