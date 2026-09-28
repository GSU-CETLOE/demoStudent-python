"""Orphaned demo student detection.

A demo student is orphaned when the course offering it was created for
no longer exists in D2L. Those accounts have nothing left to do, but
removing the wrong one is not recoverable, so every step here is built
to fail toward doing nothing.

Detection runs in two passes. scanDemoStudents walks D2L's user list and
records every demo.{orgUnitId} account into the inventory.
confirmCourseStates then looks up each distinct course once and marks it
live, deleted, or unconfirmable.

The accounts come from D2L rather than from the completedCourses table
for a specific reason. That table is a skip list pruned on a one-year
clock, so a course deleted after its row ages out leaves nothing behind,
and the oldest demo students are the ones most likely to have lost their
course. The population that matters most is the one that table is worst
at holding. A demo student's username carries the org unit id it belongs
to, so reading from D2L needs nothing the database remembers.

Only an explicit 404 marks a course deleted. A 403 means the service
account cannot see the course, which is a different thing, and any other
error means the lookup simply failed. Both leave the state unknown, and
nothing acts on unknown.

Cleanup deactivates rather than deletes. D2L reverses a user deletion
only through a support request, only within 30 days, and only while no
new account has taken the same username. This tool reuses
demo.{orgUnitId} on every run, so a recreated demo student closes that
window for good. Deactivation blocks the account, keeps its enrollments,
and can be undone whenever.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

from .config import AppConfig
from .d2lApi import D2lApiError, D2lUnavailableError
from .db import MetricsDb
from .logs import LOGGER_NAME
from .output import emit

fileLogger = logging.getLogger(LOGGER_NAME)




class DemoStudentScanApi(Protocol):
    """The slice of D2lApiClient that the inventory scan needs."""

    def iterUsers(self) -> Iterator[dict[str, Any]]: ...


class OrphanScanApi(Protocol):
    """The slice of D2lApiClient that orphan detection needs."""

    def iterDescendants(self, orgUnitId: int, ouTypeId: int) -> Iterator[dict[str, Any]]: ...
    def getOrgUnit(self, orgUnitId: int) -> dict[str, Any]: ...
    def getUserByUsername(self, userName: str) -> dict[str, Any] | None: ...


class OrphanDeleteApi(Protocol):
    """The slice of D2lApiClient that retiring an orphan needs."""

    def deleteUser(self, userId: int) -> None: ...
    def setUserActivation(self, userId: int, isActive: bool) -> None: ...


class DemoStudentScanAborted(Exception):
    """The user walk could not be trusted to see what it needed to see."""


@dataclass(frozen=True)
class OrphanedDemoStudent:
    """A demo student whose course offering is confirmed deleted."""

    orgUnitId: int
    userName: str
    userId: int
    courseName: str | None
    courseCode: str | None


@dataclass(frozen=True)
class DemoStudentScanResult:
    """What one walk of the D2L user list turned up."""

    usersWalked: int = 0
    demoStudentsFound: int = 0
    newlyRecorded: int = 0
    unparsed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CourseStateResult:
    """How many courses each verdict applied to in one confirmation pass."""

    live: int = 0
    deleted: int = 0
    unknown: int = 0


@dataclass(frozen=True)
class OrphanDeleteOutcome:
    """Result of retiring the orphans in a report."""

    deleted: int = 0
    failed: int = 0
    hardDeleted: bool = False

    @property
    def actionName(self) -> str:
        """What was done to the orphans, for display."""
        return "deleted" if self.hardDeleted else "deactivated"


def parseDemoStudentUsername(userName: str, config: AppConfig) -> int | None:
    """The org unit id encoded in a demo student username, or None.

    Demo students are named {usernamePrefix}.{orgUnitId}, so the username
    points back at its own course. That is what makes an inventory scan
    possible: no database row is needed to know where an account belongs.

    Returns None for anything this tool would not have created. A name
    starting with the prefix but carrying no usable org unit id, such as
    a demo.legacy left by some other process, is rejected rather than
    guessed at. D2L usernames are not case sensitive, so neither is the
    prefix match.
    """
    if not userName:
        return None
    prefix = f"{config.usernamePrefix}."
    if not userName.lower().startswith(prefix.lower()):
        return None

    orgUnitPart = userName[len(prefix):]
    # isdigit() rejects signs, whitespace, and embedded separators, which
    # int() alone would accept or misread.
    if not orgUnitPart.isdigit():
        return None
    return int(orgUnitPart)


def confirmCourseStates(
    api: OrphanScanApi,
    db: MetricsDb,
    logFunc: Callable[[str], None] = emit,
) -> CourseStateResult:
    """Look up each inventoried demo student's course and record what it finds.

    Read-only against D2L. The only call made here is an org unit lookup,
    and the counts it returns describe what the courses already were, not
    anything this function did to them.

    One lookup per distinct org unit rather than per demo student, since
    several accounts can name the same course.

    Only an explicit 404 sets a course to 'deleted'. A 403 means the
    service account cannot see it, and anything else means the lookup
    failed; both leave the state 'unknown', which no cleanup touches.
    Same rule as everywhere else here: a course is presumed alive unless
    D2L says otherwise.
    """
    students = db.demoStudents()
    orgUnitIds = sorted({
        student["orgUnitId"] for student in students
        if student["orgUnitId"] is not None
    })

    if not orgUnitIds:
        return CourseStateResult()

    logFunc(f"Checking {len(orgUnitIds)} courses, one lookup each. "
            f"Read-only; nothing is changed.")

    live = 0
    missing = 0
    unknown = 0
    checked = 0

    for orgUnitId in orgUnitIds:
        checked += 1
        if checked % CONFIRM_PROGRESS_INTERVAL == 0:
            logFunc(f"  checked {checked} of {len(orgUnitIds)} courses, "
                    f"{missing} already missing from D2L")
        try:
            api.getOrgUnit(orgUnitId)
        except D2lUnavailableError:
            # Says nothing about this course, and every remaining lookup
            # would fail the same way.
            raise
        except D2lApiError as error:
            if error.httpStatus == 404:
                db.setCourseState(orgUnitId, "deleted")
                missing += 1
                logFunc(f"  {orgUnitId}: course no longer exists in D2L")
                continue
            db.setCourseState(orgUnitId, "unknown")
            unknown += 1
            logFunc(f"  {orgUnitId}: cannot confirm (HTTP {error.httpStatus})")
            fileLogger.warning(
                f"Course state unknown for org unit {orgUnitId}: "
                f"HTTP {error.httpStatus}: {error}")
            continue
        except Exception as error:
            db.setCourseState(orgUnitId, "unknown")
            unknown += 1
            logFunc(f"  {orgUnitId}: cannot confirm ({type(error).__name__})")
            fileLogger.warning(
                f"Course state unknown for org unit {orgUnitId}: "
                f"{type(error).__name__}: {error}")
            continue

        db.setCourseState(orgUnitId, "live")
        live += 1

    return CourseStateResult(live=live, deleted=missing, unknown=unknown)


# Report progress every this many users. The walk is one long HTTP
# conversation with nothing to show for it in between, so this is set low
# enough that a tenant of any size shows movement within a few seconds.
SCAN_PROGRESS_INTERVAL = 500

# Same idea for the course confirmation pass, which spends one API call
# per course. The count is lower because courses are far fewer than users
# and each call is slower.
CONFIRM_PROGRESS_INTERVAL = 100


def scanDemoStudents(
    api: DemoStudentScanApi,
    config: AppConfig,
    db: MetricsDb,
    logFunc: Callable[[str], None] = emit,
) -> DemoStudentScanResult:
    """Walk D2L's user list and record every demo student into the inventory.

    This is the half that makes orphan detection work on old courses.
    Retention prunes completedCourses after a year, so it cannot answer
    the question for exactly the courses most likely to have been
    deleted. Reading the accounts from D2L avoids that: a demo student's
    username carries the org unit id it belongs to.

    Nothing is judged here. The scan records what exists, and deciding
    which courses are gone is confirmCourseStates' job.

    Raises DemoStudentScanAborted when usernames are not visible. D2L
    filters this listing by what the calling role may see and withholds
    individual fields the same way, saying nothing about either in the
    response, so a role that cannot read usernames matches nothing and
    looks exactly like a tenant with no demo students.
    """
    logFunc(f"Reading every user from {config.host}. This is the slow part; "
            f"a large tenant takes a while.")

    usersWalked = 0
    demoStudents: list[dict] = []
    unparsed: list[str] = []
    sawAnyUsername = False

    for user in api.iterUsers():
        usersWalked += 1
        userName = user.get("UserName")
        if userName:
            sawAnyUsername = True
        elif usersWalked <= 1:
            # Fail on the first user rather than walking a whole tenant
            # to arrive at an empty answer.
            raise DemoStudentScanAborted(
                "The first user came back with no UserName. D2L withholds fields "
                "the calling role cannot see, so a role without username visibility "
                "matches no demo students and reports a clean empty result. Grant "
                "See Usernames under User Information Privacy, then re-run.")

        if not userName:
            continue

        if usersWalked % SCAN_PROGRESS_INTERVAL == 0:
            logFunc(f"  walked {usersWalked} users, "
                    f"{len(demoStudents)} demo students so far")

        orgUnitId = parseDemoStudentUsername(userName, config)
        if orgUnitId is None:
            if userName.lower().startswith(f"{config.usernamePrefix}.".lower()):
                # Looks like one of ours but carries no usable org unit id.
                unparsed.append(userName)
            continue

        demoStudents.append({
            "userId": user["UserId"],
            "userName": userName,
            "orgUnitId": orgUnitId,
            "isActive": bool(user.get("Activation", {}).get("IsActive", True)),
        })

    if usersWalked and not sawAnyUsername:
        raise DemoStudentScanAborted(
            f"Walked {usersWalked} users and not one carried a UserName. D2L "
            f"withholds fields the calling role cannot see. Grant See Usernames "
            f"under User Information Privacy, then re-run.")

    newlyRecorded = db.recordDemoStudents(demoStudents)

    logFunc(f"Walked {usersWalked} users; found {len(demoStudents)} demo students "
            f"({newlyRecorded} new to the inventory).")
    if unparsed:
        noun = "account" if len(unparsed) == 1 else "accounts"
        logFunc(f"  Left alone: {len(unparsed)} {noun} matching the "
                f"'{config.usernamePrefix}.' prefix with no usable org unit id.")
    fileLogger.info(
        f"Demo student scan: walked {usersWalked} users, found {len(demoStudents)}, "
        f"{newlyRecorded} new, {len(unparsed)} unparsed")

    return DemoStudentScanResult(
        usersWalked=usersWalked, demoStudentsFound=len(demoStudents),
        newlyRecorded=newlyRecorded, unparsed=unparsed,
    )


def findOrphansFromInventory(
    db: MetricsDb,
    includeInactive: bool = False,
) -> list[OrphanedDemoStudent]:
    """Demo students in the inventory whose course is confirmed deleted.

    Reads only what confirmCourseStates already established. A course
    still 'unknown' is never an orphan, which is what stops a permissions
    gap becoming a mass cleanup.

    Demo students already deactivated are left out by default, since
    re-reporting a retired account on every run is just noise. Pass
    includeInactive=True to list them anyway, which helps when the plan
    is to hard-delete what was deactivated earlier.
    """
    orphans = []
    for student in db.demoStudents(courseState="deleted"):
        if not includeInactive and not student["isActive"]:
            continue
        orphans.append(OrphanedDemoStudent(
            orgUnitId=student["orgUnitId"],
            userName=student["userName"],
            userId=student["userId"],
            courseName=None,
            courseCode=None,
        ))
    return orphans


def deleteOrphanedDemoStudents(
    api: OrphanDeleteApi,
    db: MetricsDb,
    orphans: list[OrphanedDemoStudent],
    runId: int | None = None,
    hardDelete: bool = False,
    logFunc: Callable[[str], None] = emit,
) -> OrphanDeleteOutcome:
    """Retire the demo students in a list and update the inventory.

    Deactivates by default, which blocks the account, keeps its
    enrollments, and can be undone whenever. Pass hardDelete=True to
    delete instead. D2L reverses a deletion only through a support
    request, only within 30 days, and only while no new account has taken
    the same username, and this tool reuses demo.{orgUnitId} on every
    run.

    Works only from orphans whose course was already confirmed deleted;
    nothing is judged here. A user that fails keeps its inventory row and
    its recorded course state, so the next run offers it again, with the
    failure recorded in the errors table. Every exception is caught, not
    just D2lApiError, so one bad user cannot abandon the rest.
    """
    deleted = 0
    failed = 0
    actionName = "delete" if hardDelete else "deactivate"
    pastTense = "Deleted" if hardDelete else "Deactivated"

    for orphan in orphans:
        try:
            if hardDelete:
                api.deleteUser(orphan.userId)
            else:
                api.setUserActivation(orphan.userId, isActive=False)
        except Exception as error:
            failed += 1
            if isinstance(error, D2lApiError):
                httpStatus = error.httpStatus
                detail = str(error)
            else:
                httpStatus = None
                detail = f"{type(error).__name__}: {error}"
            message = (f"Could not {actionName} {orphan.userName} "
                       f"(user {orphan.userId}): {detail}")
            logFunc(f"  ERROR {orphan.orgUnitId}: {message}")
            fileLogger.error(message)
            db.recordError(runId, orgUnitId=orphan.orgUnitId, phase="deleteOrphan",
                           httpStatus=httpStatus, message=message)
            continue

        deleted += 1
        db.clearCompleted([orphan.orgUnitId])
        if hardDelete:
            # The account is gone from D2L, so the inventory row would
            # only be re-scanned into nothing.
            db.forgetDemoStudent(orphan.userId)
        else:
            # Still in D2L, just inactive. Record that so the next run
            # does not offer it again.
            db.recordDemoStudents([{
                "userId": orphan.userId, "userName": orphan.userName,
                "orgUnitId": orphan.orgUnitId, "isActive": False,
            }])
            db.setCourseState(orphan.orgUnitId, "deleted")
        logFunc(f"  {pastTense.lower()} {orphan.userName} "
                f"(course {orphan.orgUnitId} no longer exists)")
        fileLogger.info(
            f"{pastTense} orphaned demo student {orphan.userName} "
            f"(user {orphan.userId}); course offering {orphan.orgUnitId} no longer exists")

    return OrphanDeleteOutcome(deleted=deleted, failed=failed, hardDeleted=hardDelete)
