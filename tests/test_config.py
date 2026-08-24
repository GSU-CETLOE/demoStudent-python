"""Tests for configuration loading and validation."""

import pytest

from demostudent.config import AppConfig, ConfigError, loadConfig

FULL_CONFIG = """
host = "example.brightspace.com"
clientId = "my-client-id"
clientSecret = "my-client-secret"
redirectUri = "https://localhost:8443/callback"
scope = "core:*:*"
roleId = 783
orgOu = 1070555
courseOfferingOuType = 3
usernamePrefix = "demo"
firstName = "Demo"
lastName = "Student"
externalEmailDomain = "student.gsu.edu"
maxWorkers = 8
workingDir = "working"
logDir = "logs"
logFilePrefix = "demoStudent"
"""

MINIMAL_CONFIG = """
host = "example.brightspace.com"
clientId = "my-client-id"
clientSecret = "my-client-secret"
redirectUri = "https://localhost:8443/callback"
"""


def writeConfig(tmp_path, content):
    configPath = tmp_path / "config.toml"
    configPath.write_text(content)
    return configPath


def test_loadFullConfig(tmp_path):
    config = loadConfig(writeConfig(tmp_path, FULL_CONFIG))
    assert config.host == "example.brightspace.com"
    assert config.clientId == "my-client-id"
    assert config.clientSecret == "my-client-secret"
    assert config.redirectUri == "https://localhost:8443/callback"
    assert config.scope == "core:*:*"
    assert config.roleId == 783
    assert config.orgOu == 1070555
    assert config.courseOfferingOuType == 3
    assert config.usernamePrefix == "demo"
    assert config.firstName == "Demo"
    assert config.lastName == "Student"
    assert config.externalEmailDomain == "student.gsu.edu"
    assert config.maxWorkers == 8
    assert config.logDir == "logs"
    assert config.logFilePrefix == "demoStudent"


def test_minimalConfigUsesDefaults(tmp_path):
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG))
    assert config.scope == "core:*:*"
    assert config.roleId == 783
    assert config.orgOu == 1070555
    assert config.courseOfferingOuType == 3
    assert config.usernamePrefix == "demo"
    assert config.firstName == "Demo"
    assert config.lastName == "Student"
    assert config.maxWorkers == 8
    assert config.logDir == "logs"
    assert config.logFilePrefix == "demoStudent"


def test_workingDirRelativeToConfigFile(tmp_path):
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG))
    assert config.workingDir == tmp_path / "working"
    assert config.dbPath == tmp_path / "working" / "db" / "demoStudent.db"
    assert config.tokenPath == tmp_path / "working" / "token" / "token.json"


def test_workingDirAbsolutePathRespected(tmp_path):
    # Single-quoted TOML literal string: in a basic string the backslashes of a
    # Windows path are escape sequences, so "C:\Users\..." fails to parse.
    content = MINIMAL_CONFIG + f"\nworkingDir = '{tmp_path}/elsewhere'\n"
    config = loadConfig(writeConfig(tmp_path, content))
    assert config.workingDir == tmp_path / "elsewhere"


def test_clientSecretEnvOverride(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_STUDENT_CLIENT_SECRET", "env-secret")
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG))
    assert config.clientSecret == "env-secret"


def test_clientSecretFromEnvOnly(tmp_path, monkeypatch):
    content = MINIMAL_CONFIG.replace('clientSecret = "my-client-secret"\n', "")
    monkeypatch.setenv("DEMO_STUDENT_CLIENT_SECRET", "env-secret")
    config = loadConfig(writeConfig(tmp_path, content))
    assert config.clientSecret == "env-secret"


def test_missingRequiredFieldRaises(tmp_path):
    content = MINIMAL_CONFIG.replace('host = "example.brightspace.com"\n', "")
    with pytest.raises(ConfigError, match="host"):
        loadConfig(writeConfig(tmp_path, content))


def test_missingClientSecretRaises(tmp_path, monkeypatch):
    monkeypatch.delenv("DEMO_STUDENT_CLIENT_SECRET", raising=False)
    content = MINIMAL_CONFIG.replace('clientSecret = "my-client-secret"\n', "")
    with pytest.raises(ConfigError, match="clientSecret"):
        loadConfig(writeConfig(tmp_path, content))


def test_missingConfigFileRaises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        loadConfig(tmp_path / "nope.toml")


def test_invalidTomlRaises(tmp_path):
    with pytest.raises(ConfigError, match="Invalid TOML"):
        loadConfig(writeConfig(tmp_path, "host = [unclosed"))


def test_unknownKeyRaises(tmp_path):
    content = MINIMAL_CONFIG + '\ntypoKey = "oops"\n'
    with pytest.raises(ConfigError, match="typoKey"):
        loadConfig(writeConfig(tmp_path, content))


def test_demoUsernameHelper(tmp_path):
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG))
    assert config.demoUsername(123456) == "demo.123456"
    assert config.demoEmail(123456) == "demo.123456@student.gsu.edu"
