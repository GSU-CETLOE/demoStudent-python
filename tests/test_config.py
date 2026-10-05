"""Tests for configuration loading and validation."""

import pytest

from demostudent.config import AppConfig, ConfigError, loadConfig

FULL_CONFIG = """
host = "example.brightspace.com"
proxyUrl = "http://127.0.0.1:8080"
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
    assert config.proxyUrl == "http://127.0.0.1:8080"
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


def test_proxyUrlIsOptional(tmp_path):
    """Without it the tool calls D2L directly."""
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG))
    assert config.proxyUrl is None


def test_emptyProxyUrlMeansDirect(tmp_path):
    """An emptied value reads as unset rather than as a URL with no host."""
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG + 'proxyUrl = ""\n'))
    assert config.proxyUrl is None


def test_proxyUrlWithoutSchemeRaises(tmp_path):
    """host takes a bare name and proxyUrl a full URL; mixing them up is easy."""
    content = MINIMAL_CONFIG + 'proxyUrl = "127.0.0.1:8080"\n'
    with pytest.raises(ConfigError, match="proxyUrl"):
        loadConfig(writeConfig(tmp_path, content))


def test_proxyUrlTrailingSlashDropped(tmp_path):
    content = MINIMAL_CONFIG + 'proxyUrl = "https://proxy.example.edu/d2l/"\n'
    config = loadConfig(writeConfig(tmp_path, content))
    assert config.proxyUrl == "https://proxy.example.edu/d2l"


PROXY_KEY_CONFIG = """
host = "example.brightspace.com"
proxyUrl = "http://127.0.0.1:8080"
proxyKey = "8cc165e1-9d71-462c-b784-081412188b18"
"""


def test_proxyKeyAndProxyUserAreOptional(tmp_path):
    """Without a key the tool signs in to D2L itself and sends its own token."""
    config = loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG))
    assert config.proxyKey is None
    assert config.proxyUser is None


def test_proxyKeyMakesTheOAuthSettingsOptional(tmp_path, monkeypatch):
    """With a key apiProxy2 signs in to D2L, so this tool has no OAuth2 application."""
    monkeypatch.delenv("DEMO_STUDENT_CLIENT_SECRET", raising=False)
    config = loadConfig(writeConfig(tmp_path, PROXY_KEY_CONFIG))
    assert config.proxyKey == "8cc165e1-9d71-462c-b784-081412188b18"
    assert config.proxyUser is None
    assert config.clientId == ""
    assert config.clientSecret == ""
    assert config.redirectUri == ""


def test_proxyKeyStillRequiresHost(tmp_path):
    """D2L's absolute links name the tenant, so host is needed in every mode."""
    content = PROXY_KEY_CONFIG.replace('host = "example.brightspace.com"\n', "")
    with pytest.raises(ConfigError, match="host"):
        loadConfig(writeConfig(tmp_path, content))


def test_proxyKeyWithoutProxyUrlRaises(tmp_path):
    """The key means nothing to D2L, so there has to be a proxy to send it to."""
    with pytest.raises(ConfigError, match="proxyKey is set without proxyUrl"):
        loadConfig(writeConfig(tmp_path, MINIMAL_CONFIG + 'proxyKey = "some-guid"\n'))


def test_emptyProxyKeyMeansTheToolSignsInItself(tmp_path, monkeypatch):
    """An emptied value reads as unset, so the OAuth settings are required again."""
    monkeypatch.delenv("DEMO_STUDENT_CLIENT_SECRET", raising=False)
    content = PROXY_KEY_CONFIG.replace("8cc165e1-9d71-462c-b784-081412188b18", "")
    with pytest.raises(ConfigError, match="clientId, clientSecret, redirectUri"):
        loadConfig(writeConfig(tmp_path, content))


def test_proxyUserNamesTheAccountToActAs(tmp_path):
    config = loadConfig(writeConfig(tmp_path, PROXY_KEY_CONFIG + 'proxyUser = "jdoe"\n'))
    assert config.proxyUser == "jdoe"


def test_emptyProxyUserMeansTheProxyPicksTheAccount(tmp_path):
    config = loadConfig(writeConfig(tmp_path, PROXY_KEY_CONFIG + 'proxyUser = ""\n'))
    assert config.proxyUser is None


def test_proxyUserWithoutProxyKeyRaises(tmp_path):
    """Only apiProxy2's proxy authentication reads the account name."""
    content = MINIMAL_CONFIG + 'proxyUrl = "http://127.0.0.1:8080"\nproxyUser = "jdoe"\n'
    with pytest.raises(ConfigError, match="proxyUser is set without proxyKey"):
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
