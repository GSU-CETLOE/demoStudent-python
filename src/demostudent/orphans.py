"""Orphaned demo student detection.

A demo student is orphaned when the course offering it was created for no
longer exists in D2L. Those users serve no purpose once their course is
gone, but deleting the wrong one is unrecoverable, so detection is built
to fail safe at every step.

Candidates come from diffing the completedCourses table against live
discovery, which costs one discovery pass rather than a call per course.
That diff alone is not trusted: Brightspace silently omits org units the
service account cannot reach from listings (see "Course Access Options"
in README.md), so a permissions gap makes thousands of live courses look
deleted. Every candidate is therefore confirmed individually with a
direct org unit lookup, and only an explicit 404 counts as proof of
deletion. A 403 means "cannot see it", which is not the same thing. Any
other error is treated as unknown. Both are reported and left alone.

As a second guard, a scan whose candidate set exceeds a share of all
completed courses aborts without confirming anything, on the theory that
a sudden mass orphaning is far more likely to be a broken service account
than a real mass deletion.
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

# Abort the scan when more than this share of completed courses look
# orphaned; that pattern means a broken service account far more often
# than it means real deletions.
DEFAULT_MAX_CANDIDATE_RATIO = 0.05

# The ratio alone is too strict on small course sets, where a handful of
# genuine deletions is a large percentage. Candidate sets at or below
# this size are always confirmed individually, which is cheap and safe.
MIN_CANDIDATES_BEFORE_ABORT = 25


class OrphanScanApi(Protocol):
    """The slice of D2lApiClient that orphan detection needs."""

    def iterDescendants(self, orgUnitId: int, ouTypeId: int) -> Iterator[dict[str, Any]]: ...
    def getOrgUnit(self, orgUnitId: int) -> dict[str, Any]: ...
    def getUserByUsername(self, userName: str) -> dict[str, Any] | None: ...


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
class OrphanReport:
    """Everything one scan found, including what it deliberately left alone."""

    orphans: list[OrphanedDemoStudent] = field(default_factory=list)
    unreachable: list[int] = field(default_factory=list)
    alreadyGone: list[int] = field(default_factory=list)
    completedCourseCount: int = 0
    candidatesChecked: int = 0


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
        try:
            api.getOrgUnit(orgUnitId)
        except D2lApiError as error:
            if error.httpStatus == 404:
                userName, userId = _resolveDemoStudent(api, config, row)
                if userId is None:
                    alreadyGone.append(orgUnitId)
                    logFunc(f"  {orgUnitId}: course deleted, demo student already gone")
                    continue
                orphans.append(OrphanedDemoStudent(
                    orgUnitId=orgUnitId, userName=userName, userId=userId,
                    courseName=row["courseName"], courseCode=row["courseCode"],
                ))
                logFunc(f"  {orgUnitId}: ORPHANED - {userName} (course deleted)")
                continue
            unreachable.append(orgUnitId)
            logFunc(f"  {orgUnitId}: skipped - cannot confirm "
                    f"(HTTP {error.httpStatus}: {error})")
            fileLogger.warning(
                f"Orphan scan could not confirm org unit {orgUnitId}: "
                f"HTTP {error.httpStatus}: {error}")
            continue

    return OrphanReport(
        orphans=orphans, unreachable=unreachable, alreadyGone=alreadyGone,
        completedCourseCount=len(completedRows), candidatesChecked=len(candidates),
    )
