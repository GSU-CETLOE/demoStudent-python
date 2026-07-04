"""Per-course demo student logic: check, create if missing, enroll.

Each course offering gets its own unique user named
{usernamePrefix}.{orgUnitId}. The check uses a direct user lookup plus a
single-enrollment fetch instead of the legacy full-classlist scan, which
keeps large courses cheap.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from .config import AppConfig
from .discovery import CourseOffering


class EnrollmentApi(Protocol):
    """The slice of D2lApiClient that enrollment needs."""

    def getUserByUsername(self, userName: str) -> dict[str, Any] | None: ...
    def getEnrollment(self, orgUnitId: int, userId: int) -> dict[str, Any] | None: ...
    def createUser(self, userName: str, firstName: str, lastName: str,
                   externalEmail: str, roleId: int) -> dict[str, Any]: ...
    def enrollUser(self, orgUnitId: int, userId: int, roleId: int) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CourseResult:
    """Outcome of processing one course offering."""

    course: CourseOffering
    action: str
    userName: str
    userId: int | None
    createdUser: bool
    enrolled: bool


def ensureDemoStudent(
    api: EnrollmentApi,
    config: AppConfig,
    course: CourseOffering,
    dryRun: bool = False,
) -> CourseResult:
    """Make sure the course has its demo student enrolled with the demo role.

    In dry-run mode nothing is created or enrolled; the action reports what
    would have happened.
    """
    userName = config.demoUsername(course.orgUnitId)
    user = api.getUserByUsername(userName)
    userId = user["UserId"] if user else None

    if userId is not None:
        enrollment = api.getEnrollment(course.orgUnitId, userId)
        if enrollment and enrollment["RoleId"] == config.roleId:
            return CourseResult(course, "alreadyEnrolled", userName, userId,
                                createdUser=False, enrolled=False)

    if dryRun:
        action = "wouldEnrollExistingUser" if userId is not None else "wouldCreateAndEnroll"
        return CourseResult(course, action, userName, userId, createdUser=False, enrolled=False)

    createdUser = False
    if userId is None:
        newUser = api.createUser(
            userName=userName,
            firstName=config.firstName,
            lastName=config.lastName,
            externalEmail=config.demoEmail(course.orgUnitId),
            roleId=config.roleId,
        )
        userId = newUser["UserId"]
        createdUser = True

    api.enrollUser(course.orgUnitId, userId, config.roleId)
    action = "createdAndEnrolled" if createdUser else "enrolledExistingUser"
    return CourseResult(course, action, userName, userId, createdUser=createdUser, enrolled=True)
