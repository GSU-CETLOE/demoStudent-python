"""Run orchestration: discover courses, fan out workers, record results.

Courses already recorded in the database are filtered out before any API
call is made, so repeat runs only touch courses that still need a demo
student. Pass recheck=True to drop the in-scope courses back to pending
first, so already-completed courses get verified live against D2L again
(ensureDemoStudent is idempotent, so this only recreates anything that's
actually missing). Worker failures are recorded in the errors table and
never stop the rest of the run.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from .config import AppConfig
from .d2lApi import D2lApiError
from .db import MetricsDb
from .discovery import CourseOffering, discoverCourseOfferings
from .enrollment import ensureDemoStudent
from .logs import LOGGER_NAME

fileLogger = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class RunSummary:
    """Aggregate outcome of one run, mirroring the runs table."""

    runId: int
    status: str
    coursesDiscovered: int
    coursesSkipped: int
    coursesChecked: int
    usersCreated: int
    enrollmentsCreated: int
    errorCount: int


def runEnrollment(
    config: AppConfig,
    api,
    db: MetricsDb,
    ouList: list[int] | None = None,
    recentSemesterCount: int | None = None,
    maxWorkers: int | None = None,
    dryRun: bool = False,
    recheck: bool = False,
    logFunc: Callable[[str], None] = print,
) -> RunSummary:
    """Execute one full pass and persist metrics; returns the summary."""
    pruned = db.pruneOldActivity()
    runId = db.startRun()
    fileLogger.info(f"Run {runId} started (dryRun={dryRun})")
    if pruned:
        fileLogger.info(f"Run {runId}: pruned {pruned} database rows older than a year")

    try:
        courses = discoverCourseOfferings(api, config, ouList=ouList,
                                          recentSemesterCount=recentSemesterCount)
    except Exception as error:
        httpStatus = error.httpStatus if isinstance(error, D2lApiError) else None
        db.recordError(runId, orgUnitId=None, phase="discovery",
                       httpStatus=httpStatus, message=str(error))
        db.finishRun(runId, status="failed", coursesDiscovered=0, coursesSkipped=0,
                     coursesChecked=0, usersCreated=0, enrollmentsCreated=0, errorCount=1)
        fileLogger.error(f"Run {runId} discovery failed: {error}")
        raise

    if recheck:
        recheckedCount = db.clearCompleted([course.orgUnitId for course in courses])
        fileLogger.info(f"Run {runId}: recheck cleared {recheckedCount} completed courses "
                        f"back to pending")

    completedIds = db.completedOrgUnitIds()
    pending = [course for course in courses if course.orgUnitId not in completedIds]
    skipped = len(courses) - len(pending)
    discoveredMessage = (f"Discovered {len(courses)} course offerings; "
                         f"{skipped} already completed, {len(pending)} to check.")
    logFunc(discoveredMessage)
    fileLogger.info(f"Run {runId}: {discoveredMessage}")

    usersCreated = 0
    enrollmentsCreated = 0
    errorCount = 0
    checked = 0

    def processCourse(course: CourseOffering):
        return ensureDemoStudent(api, config, course, dryRun=dryRun)

    workerCount = maxWorkers if maxWorkers is not None else config.maxWorkers
    interrupted = False
    if pending:
        pool = ThreadPoolExecutor(max_workers=workerCount)
        futures = {pool.submit(processCourse, course): course for course in pending}
        try:
            for future in as_completed(futures):
                course = futures[future]
                checked += 1
                try:
                    result = future.result()
                except Exception as error:
                    errorCount += 1
                    if isinstance(error, D2lApiError):
                        httpStatus = error.httpStatus
                        message = str(error)
                    else:
                        httpStatus = None
                        message = f"{type(error).__name__}: {error}"
                    db.recordError(runId, orgUnitId=course.orgUnitId, phase="course",
                                   httpStatus=httpStatus, message=message)
                    logFunc(f"ERROR {course.orgUnitId} ({course.code}): {message}")
                    fileLogger.error(
                        f"Run {runId} course {course.orgUnitId} ({course.code}): {message}")
                    continue

                usersCreated += result.createdUser
                enrollmentsCreated += result.enrolled
                if not dryRun:
                    db.markCourseCompleted(
                        orgUnitId=course.orgUnitId, courseName=course.name,
                        courseCode=course.code, userName=result.userName,
                        userId=result.userId, runId=runId,
                    )
                logFunc(f"{result.action}: {result.userName} in {course.orgUnitId} ({course.code})")
        except KeyboardInterrupt:
            interrupted = True
            logFunc("Interrupted; cancelling remaining courses "
                    "(completed ones stay recorded)...")
            fileLogger.warning(f"Run {runId} interrupted by user; pending courses cancelled")
        finally:
            pool.shutdown(wait=True, cancel_futures=interrupted)

    if interrupted:
        status = "interrupted"
    elif dryRun:
        status = "dryRun"
    elif errorCount:
        status = "completedWithErrors"
    else:
        status = "completed"

    db.finishRun(
        runId, status=status, coursesDiscovered=len(courses), coursesSkipped=skipped,
        coursesChecked=checked, usersCreated=usersCreated,
        enrollmentsCreated=enrollmentsCreated, errorCount=errorCount,
    )
    fileLogger.info(
        f"Run {runId} finished: status={status}, discovered={len(courses)}, "
        f"skipped={skipped}, checked={checked}, usersCreated={usersCreated}, "
        f"enrollmentsCreated={enrollmentsCreated}, errors={errorCount}")
    return RunSummary(
        runId=runId, status=status, coursesDiscovered=len(courses),
        coursesSkipped=skipped, coursesChecked=checked, usersCreated=usersCreated,
        enrollmentsCreated=enrollmentsCreated, errorCount=errorCount,
    )
