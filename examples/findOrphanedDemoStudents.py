"""Scan for orphaned demo students without touching D2L or a real database.

An orphaned demo student is one whose course offering has been deleted.
This example wires findOrphanedDemoStudents to a stand-in D2L that has
deleted one course and hidden another, so the safety behavior is visible
without needing credentials:

    uv run python examples/findOrphanedDemoStudents.py

Course 300 is live, 302 has been deleted, and 303 is real but invisible
to the service account. Only 302 is reported as orphaned; 303 is counted
as unconfirmable and deliberately left alone, because a course the
account cannot see is not a deleted course.

To run the same scan against a real tenant, use the CLI instead:

    uv run demo-student find-orphans --out working/orphans.csv
"""

import logging
import tempfile
from pathlib import Path

from demostudent.config import AppConfig
from demostudent.d2lApi import D2lApiError
from demostudent.db import MetricsDb
from demostudent.logs import LOGGER_NAME
from demostudent.orphans import findOrphanedDemoStudents

LIVE_COURSE = 300
DELETED_COURSE = 302
HIDDEN_COURSE = 303


class StandInD2l:
    """Enough of the D2L client for one scan, with no network calls."""

    def __init__(self):
        self.liveCourseIds = [LIVE_COURSE]

    def iterDescendants(self, orgUnitId, ouTypeId):
        """Discovery: only the live course comes back, as D2L would report it."""
        for courseId in self.liveCourseIds:
            yield {"Identifier": str(courseId), "Name": f"Course {courseId}",
                   "Code": f"C{courseId}"}

    def getOrgUnit(self, orgUnitId):
        """Confirmation: 404 for the deleted course, 403 for the hidden one."""
        if orgUnitId == DELETED_COURSE:
            raise D2lApiError("Resource Not Found", httpStatus=404)
        if orgUnitId == HIDDEN_COURSE:
            raise D2lApiError("Not Authorized", httpStatus=403)
        return {"Identifier": str(orgUnitId), "Name": f"Course {orgUnitId}",
                "Code": f"C{orgUnitId}", "Type": {"Id": 3}}

    def getUserByUsername(self, userName):
        """User lookup, used for rows that carry no stored user id."""
        return {"UserId": 9000 + int(userName.split(".")[1]), "UserName": userName}


def main() -> None:
    """Run one scan against the stand-in and print what it found."""
    # No log file is set up here, so silence the application logger rather
    # than letting it fall back to stderr and interleave with the output.
    logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())

    with tempfile.TemporaryDirectory() as tempDir:
        config = AppConfig(
            host="example.brightspace.com",
            clientId="client-id",
            clientSecret="client-secret",
            redirectUri="https://localhost:8443/callback",
            workingDir=Path(tempDir),
        )
        db = MetricsDb(config.dbPath)
        try:
            runId = db.startRun()
            for orgUnitId in (LIVE_COURSE, DELETED_COURSE, HIDDEN_COURSE):
                db.markCourseCompleted(
                    orgUnitId=orgUnitId, courseName=f"Course {orgUnitId}",
                    courseCode=f"C{orgUnitId}", userName=f"demo.{orgUnitId}",
                    userId=9000 + orgUnitId, runId=runId,
                )

            report = findOrphanedDemoStudents(StandInD2l(), config, db)

            print("\nResult")
            for orphan in report.orphans:
                print(f"  orphaned:      {orphan.userName} "
                      f"(course {orphan.orgUnitId} was deleted)")
            for orgUnitId in report.unreachable:
                print(f"  unconfirmable: course {orgUnitId} could not be checked, "
                      f"so its demo student was left alone")
            print(f"\n  {len(report.orphans)} orphaned, "
                  f"{len(report.unreachable)} unconfirmable, "
                  f"of {report.completedCourseCount} completed courses.")
            print("\n  delete-orphans would deactivate only the orphaned one.")
        finally:
            db.close()


if __name__ == "__main__":
    main()
