"""Tests for recognizing demo student accounts by username."""

import pytest

from demostudent.config import AppConfig
from demostudent.orphans import parseDemoStudentUsername


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
    )


@pytest.mark.parametrize("userName, expected", [
    ("demo.2469900", 2469900),
    ("demo.1", 1),
    ("demo.0000301", 301),      # leading zeros still parse to the same course
])
def test_validDemoUsernameYieldsItsOrgUnitId(config, userName, expected):
    assert parseDemoStudentUsername(userName, config) == expected


@pytest.mark.parametrize("userName", [
    "jsmith",                   # an ordinary account
    "demoinstructor",           # prefix-like but no separator
    "demo",                     # bare prefix
    "demo.",                    # separator, no id
    "demo.legacy",              # non-numeric tail
    "demo.301.extra",           # more than one segment
    "demo.-301",                # negative is not an org unit id
    "demo.3 01",                # whitespace inside
    "xdemo.301",                # prefix not at the start
    "",
])
def test_nonDemoUsernameYieldsNothing(config, userName):
    assert parseDemoStudentUsername(userName, config) is None


def test_matchingIsCaseInsensitiveOnThePrefix(config):
    """D2L usernames are not case sensitive, so neither is the match."""
    assert parseDemoStudentUsername("DEMO.301", config) == 301
    assert parseDemoStudentUsername("Demo.301", config) == 301


def test_prefixComesFromConfig(config):
    from dataclasses import replace
    renamed = replace(config, usernamePrefix="testuser")
    assert parseDemoStudentUsername("testuser.301", renamed) == 301
    assert parseDemoStudentUsername("demo.301", renamed) is None


def test_usernameTheToolWouldGenerateAlwaysParses(config):
    """Whatever demoUsername builds must be recognized by the parser."""
    for orgUnitId in (1, 301, 2469900, 999999999):
        assert parseDemoStudentUsername(config.demoUsername(orgUnitId), config) == orgUnitId
