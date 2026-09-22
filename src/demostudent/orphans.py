"""Orphaned demo student detection.

A demo student is orphaned when the course offering it was created for
no longer exists in D2L. Those accounts have nothing left to do, but
removing the wrong one is not recoverable, so every step here is built
to fail toward doing nothing.

Candidates come from diffing the completedCourses table against live
discovery, which costs one discovery pass instead of a call per course.
The diff by itself is not evidence. Brightspace leaves org units the
service account cannot reach out of listings (see "Course Access
Options" in README.md), so a permissions gap would make thousands of
live courses look deleted. Each candidate is confirmed with its own org
unit lookup, and only an explicit 404 counts. A 403 means the account
cannot see the course, which is a different thing; any other error means
nothing at all. Both get reported and left alone.

There is a second guard for the case the first one misses. A scan whose
candidate set is too large a share of all completed courses gives up
before confirming anything, since a sudden mass orphaning is usually a
broken service account rather than real deletions.

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
from .d2lApi import D2lApiError
from .db import MetricsDb
from .discovery import discoverCourseOfferings
from .logs import LOGGER_NAME

fileLogger = logging.getLogger(LOGGER_NAME)

# Give up on a scan when more than this share of completed courses look
# orphaned. That pattern is usually a broken service account rather than
# real deletions.
DEFAULT_MAX_CANDIDATE_RATIO = 0.05

# The ratio on its own is too strict on small course sets, where a few
# real deletions are a large percentage. Candidate sets this size or
# smaller always get confirmed one by one, which is cheap anyway.
MIN_CANDIDATES_BEFORE_ABORT = 25


class OrphanScanApi(Protocol):
    """The slice of D2lApiClient that orphan detection needs."""

    def iterDescendants(self, orgUnitId: int, ouTypeId: int) -> Iterator[dict[str, Any]]: ...
    def getOrgUnit(self, orgUnitId: int) -> dict[str, Any]: ...
    def getUserByUsername(self, userName: str) -> dict[str, Any] | None: ...


class OrphanDeleteApi(Protocol):
    """The slice of D2lApiClient that retiring an orphan needs."""

    def deleteUser(self, userId: int) -> None: ...
    def setUserActivation(self, userId: int, isActive: bool) -> None: ...


class OrphanScanAborted(Exception):
    """The candidate set was too large to trust, so nothing was confirmed."""


@dataclass(frozen=True)
class OrphanedDemoStudent:
    """A demo student whose course offering is confirmed deleted."""

    orgUnitId: int
    userName: str
    userId: int
    courseName: str | None
    courseCode: str | None


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


@dataclass(frozen=True)
class OrphanReport:
    """Everything one scan found, including what it deliberately left alone."""

    orphans: list[OrphanedDemoStudent] = field(default_factory=list)
    unreachable: list[int] = field(default_factory=list)
    alreadyGone: list[int] = field(default_factory=list)
    completedCourseCount: int = 0
    candidatesChecked: int = 0


def parseDemoStudentUsername(userName: str, config: AppConfig) -> int | None:
    """The org unit id encoded in a demo student username, or None.

    Demo students are named {usernamePrefix}.{orgUnitId}, so the username
    carries its own pointer back to the course. That is what makes an
    inventory scan possible at all: it needs no database row to know
    which course an account belongs to.

    Returns None for anything that is not a demo student this tool would
    have created. A name that starts with the prefix but carries no
    usable org unit id, such as demo.legacy from some other process, is
    rejected rather than guessed at. D2L usernames are not case
    sensitive, so the prefix match is not either.
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


def _resolveDemoStudent(
    api: OrphanScanApi,
    config: AppConfig,
    row: dict[str, Any],
) -> tuple[str, int | None]:
    """The demo student for a completed course row.

    Rows written by a run carry the username and user id. Rows seeded by
    import-legacy carry only an org unit id, so the user is looked up
    live from its derived username.
    """
    userName = row["userName"] or config.demoUsername(row["orgUnitId"])
    userId = row["userId"]
    if userId is None:
        user = api.getUserByUsername(userName)
        userId = user["UserId"] if user else None
    return userName, userId


def findOrphanedDemoStudents(
    api: OrphanScanApi,
    config: AppConfig,
    db: MetricsDb,
    ouList: list[int] | None = None,
    maxCandidateRatio: float = DEFAULT_MAX_CANDIDATE_RATIO,
    logFunc: Callable[[str], None] = print,
) -> OrphanReport:
    """Find demo students whose course offering no longer exists.

    Reports only; nothing in D2L or the database is changed. Raises
    OrphanScanAborted when the candidate set is both larger than
    MIN_CANDIDATES_BEFORE_ABORT and above maxCandidateRatio of all
    completed courses, which signals a permissions problem rather than
    real deletions. Pass maxCandidateRatio=1.0 to scan anyway.
    """
    completedRows = db.completedCourses()
    if not completedRows:
        logFunc("No completed courses recorded; nothing to scan.")
        return OrphanReport()

    liveCourses = discoverCourseOfferings(api, config, ouList=ouList)
    liveOrgUnitIds = {course.orgUnitId for course in liveCourses}
    candidates = [row for row in completedRows if row["orgUnitId"] not in liveOrgUnitIds]

    logFunc(f"{len(completedRows)} completed courses recorded; {len(liveCourses)} found live; "
            f"{len(candidates)} to confirm.")

    overRatio = len(candidates) > len(completedRows) * maxCandidateRatio
    if len(candidates) > MIN_CANDIDATES_BEFORE_ABORT and overRatio:
        message = (
            f"Aborting: {len(candidates)} of {len(completedRows)} completed courses are "
            f"missing from discovery, above the {maxCandidateRatio:.0%} threshold. "
            f"Brightspace hides courses the service account cannot reach, so this is far "
            f"more likely a permissions problem than real deletions. Check Course Access "
            f"Options and role permissions (see README.md), then re-run with "
            f"--max-candidate-ratio to override."
        )
        fileLogger.error(message)
        raise OrphanScanAborted(message)

    orphans: list[OrphanedDemoStudent] = []
    unreachable: list[int] = []
    alreadyGone: list[int] = []

    for row in candidates:
        orgUnitId = row["orgUnitId"]

        def skipUnconfirmable(reason: str) -> None:
            """Leave a course alone because its state could not be established.

            Anything short of an explicit 404 lands here.
            """
            unreachable.append(orgUnitId)
            logFunc(f"  {orgUnitId}: skipped - cannot confirm ({reason})")
            fileLogger.warning(
                f"Orphan scan could not confirm org unit {orgUnitId}: {reason}")

        try:
            api.getOrgUnit(orgUnitId)
        except D2lApiError as error:
            if error.httpStatus != 404:
                skipUnconfirmable(f"HTTP {error.httpStatus}: {error}")
                continue
        except Exception as error:
            # A timeout or malformed response proves nothing either way.
            skipUnconfirmable(f"{type(error).__name__}: {error}")
            continue
        else:
            continue  # 200: the course is alive, so nothing is orphaned here

        # Only a 404 reaches this point: the course offering is gone.
        try:
            userName, userId = _resolveDemoStudent(api, config, row)
        except Exception as error:
            skipUnconfirmable(f"user lookup failed - {type(error).__name__}: {error}")
            continue

        if userId is None:
            alreadyGone.append(orgUnitId)
            logFunc(f"  {orgUnitId}: course deleted, demo student already gone")
            continue

        orphans.append(OrphanedDemoStudent(
            orgUnitId=orgUnitId, userName=userName, userId=userId,
            courseName=row["courseName"], courseCode=row["courseCode"],
        ))
        logFunc(f"  {orgUnitId}: ORPHANED - {userName} (course deleted)")

    return OrphanReport(
        orphans=orphans, unreachable=unreachable, alreadyGone=alreadyGone,
        completedCourseCount=len(completedRows), candidatesChecked=len(candidates),
    )


def deleteOrphanedDemoStudents(
    api: OrphanDeleteApi,
    db: MetricsDb,
    report: OrphanReport,
    runId: int | None = None,
    hardDelete: bool = False,
    logFunc: Callable[[str], None] = print,
) -> OrphanDeleteOutcome:
    """Retire the demo students in a report and clear their database rows.

    Deactivates by default, which blocks the account, keeps its
    enrollments, and can be undone whenever. Pass hardDelete=True to
    delete instead. D2L reverses a deletion only through a support
    request, only within 30 days, and only while no new account has taken
    the same username, and this tool reuses demo.{orgUnitId} on every
    run.

    Works only from orphans findOrphanedDemoStudents already confirmed;
    nothing is judged deleted here. A user that fails keeps its
    completedCourses row and stays on the books for a retry, with the
    failure recorded in the errors table. Every exception is caught, not
    just D2lApiError, so one bad user cannot abandon the rest.
    """
    deleted = 0
    failed = 0
    actionName = "delete" if hardDelete else "deactivate"
    pastTense = "Deleted" if hardDelete else "Deactivated"

    for orphan in report.orphans:
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
        logFunc(f"  {pastTense.lower()} {orphan.userName} "
                f"(course {orphan.orgUnitId} no longer exists)")
        fileLogger.info(
            f"{pastTense} orphaned demo student {orphan.userName} "
            f"(user {orphan.userId}); course offering {orphan.orgUnitId} no longer exists")

    return OrphanDeleteOutcome(deleted=deleted, failed=failed, hardDeleted=hardDelete)


def confirmCourseStates(
    api: OrphanScanApi,
    db: MetricsDb,
    logFunc: Callable[[str], None] = print,
) -> CourseStateResult:
    """Look up each inventoried demo student's course and record what it finds.

    One lookup per distinct org unit, not per demo student, since several
    accounts can in principle name the same course.

    Only an explicit 404 sets a course to 'deleted'. A 403 means the
    service account cannot see the course, and anything else means the
    lookup failed; both leave the state 'unknown', which no cleanup will
    act on. That is the same fail-safe rule the rest of this module uses:
    a course is presumed alive unless D2L says otherwise.
    """
    students = db.demoStudents()
    orgUnitIds = sorted({
        student["orgUnitId"] for student in students
        if student["orgUnitId"] is not None
    })

    live = 0
    deleted = 0
    unknown = 0

    for orgUnitId in orgUnitIds:
        try:
            api.getOrgUnit(orgUnitId)
        except D2lApiError as error:
            if error.httpStatus == 404:
                db.setCourseState(orgUnitId, "deleted")
                deleted += 1
                logFunc(f"  {orgUnitId}: course deleted")
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

    return CourseStateResult(live=live, deleted=deleted, unknown=unknown)
