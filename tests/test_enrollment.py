"""Tests for the per-course demo student check/create/enroll logic."""

import pytest

from demostudent.config import AppConfig
from demostudent.discovery import CourseOffering
from demostudent.enrollment import ensureDemoStudent


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        host="example.brightspace.com",
        clientId="client-id",
        clientSecret="client-secret",
        redirectUri="https://localhost:8443/callback",
        workingDir=tmp_path / "working",
        roleId=783,
    )


COURSE = CourseOffering(orgUnitId=555, name="Bio", code="BIO101")


class FakeApi:
    """Canned user/enrollment state exposed through the client interface."""

    def __init__(self, users=None, enrollments=None):
        self.users = users or {}                # userName -> user dict
        self.enrollments = enrollments or {}    # (orgUnitId, userId) -> enrollment dict
        self.createdUsers = []
        self.enrollCalls = []
        self._nextUserId = 9000

    def getUserByUsername(self, userName):
        return self.users.get(userName)

    def getEnrollment(self, orgUnitId, userId):
        return self.enrollments.get((orgUnitId, userId))

    def createUser(self, userName, firstName, lastName, externalEmail, roleId):
        self._nextUserId += 1
        user = {"UserId": self._nextUserId, "UserName": userName}
        self.users[userName] = user
        self.createdUsers.append((userName, firstName, lastName, externalEmail, roleId))
        return user

    def enrollUser(self, orgUnitId, userId, roleId):
        self.enrollCalls.append((orgUnitId, userId, roleId))
        return {"OrgUnitId": orgUnitId, "UserId": userId, "RoleId": roleId}


def test_alreadyEnrolledDoesNothing(config):
    api = FakeApi(
        users={"demo.555": {"UserId": 9001, "UserName": "demo.555"}},
        enrollments={(555, 9001): {"OrgUnitId": 555, "UserId": 9001, "RoleId": 783}},
    )
    result = ensureDemoStudent(api, config, COURSE)
    assert result.action == "alreadyEnrolled"
    assert result.userId == 9001
    assert not api.createdUsers
    assert not api.enrollCalls


def test_missingUserIsCreatedAndEnrolled(config):
    api = FakeApi()
    result = ensureDemoStudent(api, config, COURSE)
    assert result.action == "createdAndEnrolled"
    assert result.createdUser
    assert result.enrolled
    assert api.createdUsers == [
        ("demo.555", "Demo", "Student", "demo.555@student.gsu.edu", 783)
    ]
    assert api.enrollCalls == [(555, 9001, 783)]


def test_existingUserNotEnrolledIsEnrolled(config):
    api = FakeApi(users={"demo.555": {"UserId": 9001, "UserName": "demo.555"}})
    result = ensureDemoStudent(api, config, COURSE)
    assert result.action == "enrolledExistingUser"
    assert not result.createdUser
    assert result.enrolled
    assert not api.createdUsers
    assert api.enrollCalls == [(555, 9001, 783)]


def test_enrolledWithWrongRoleIsReenrolled(config):
    api = FakeApi(
        users={"demo.555": {"UserId": 9001, "UserName": "demo.555"}},
        enrollments={(555, 9001): {"OrgUnitId": 555, "UserId": 9001, "RoleId": 999}},
    )
    result = ensureDemoStudent(api, config, COURSE)
    assert result.action == "enrolledExistingUser"
    assert api.enrollCalls == [(555, 9001, 783)]


def test_dryRunMakesNoChanges(config):
    api = FakeApi()
    result = ensureDemoStudent(api, config, COURSE, dryRun=True)
    assert result.action == "wouldCreateAndEnroll"
    assert not result.createdUser
    assert not result.enrolled
    assert not api.createdUsers
    assert not api.enrollCalls


def test_dryRunReportsExistingUser(config):
    api = FakeApi(users={"demo.555": {"UserId": 9001, "UserName": "demo.555"}})
    result = ensureDemoStudent(api, config, COURSE, dryRun=True)
    assert result.action == "wouldEnrollExistingUser"


def test_dryRunReportsAlreadyEnrolled(config):
    api = FakeApi(
        users={"demo.555": {"UserId": 9001, "UserName": "demo.555"}},
        enrollments={(555, 9001): {"OrgUnitId": 555, "UserId": 9001, "RoleId": 783}},
    )
    result = ensureDemoStudent(api, config, COURSE, dryRun=True)
    assert result.action == "alreadyEnrolled"
