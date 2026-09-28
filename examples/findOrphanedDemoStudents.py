"""Find orphaned demo students without touching D2L or a real database.

An orphaned demo student is one whose course offering has been deleted.
This runs both detection passes against a stand-in D2L, so the behavior
is visible without credentials:

    uv run python examples/findOrphanedDemoStudents.py

The stand-in holds four demo accounts. demo.300 belongs to a live
course. demo.302's course has been deleted. demo.303's course exists but
the service account cannot see it. demo.legacy matches the prefix and
carries no usable org unit id.

Only demo.302 comes back orphaned. demo.303 is left alone, because a
course the account cannot see is not a deleted course. demo.legacy gets
reported separately rather than guessed at.

For the same thing against a real tenant, use the CLI:

    uv run demo-student find-orphans --rescan --out working/orphans.csv
"""

import logging
import tempfile
from pathlib import Path

from demostudent.config import AppConfig
from demostudent.d2lApi import D2lApiError
from demostudent.db import MetricsDb
from demostudent.logs import LOGGER_NAME
from demostudent.orphans import (confirmCourseStates, findOrphansFromInventory,
                                 scanDemoStudents)

LIVE_COURSE = 300
DELETED_COURSE = 302
HIDDEN_COURSE = 303


class StandInD2l:
    """Enough of the D2L client for one scan, with no network calls."""

    def __init__(self):
        self.users = [
            {"UserId": 9300, "UserName": "demo.300", "Activation": {"IsActive": True}},
            {"UserId": 9302, "UserName": "demo.302", "Activation": {"IsActive": True}},
            {"UserId": 9303, "UserName": "demo.303", "Activation": {"IsActive": True}},
            {"UserId": 42, "UserName": "demo.legacy", "Activation": {"IsActive": True}},
            {"UserId": 1, "UserName": "jsmith", "Activation": {"IsActive": True}},
        ]

    def iterUsers(self):
        """The user listing, the way D2L pages it."""
        yield from self.users

    def getOrgUnit(self, orgUnitId):
        """404 for the deleted course, 403 for the one that is hidden."""
        if orgUnitId == DELETED_COURSE:
            raise D2lApiError("Resource Not Found", httpStatus=404)
        if orgUnitId == HIDDEN_COURSE:
            raise D2lApiError("Not Authorized", httpStatus=403)
        return {"Identifier": str(orgUnitId), "Name": f"Course {orgUnitId}",
                "Code": f"C{orgUnitId}", "Type": {"Id": 3}}


def plural(count: int, noun: str) -> str:
    """Count and noun, pluralized: '1 course', '2 courses'."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def main() -> None:
    """Run both detection passes against the stand-in and print the result."""
    # No log file is set up here, so silence the application logger rather
    # than letting it fall back to stderr and interleave with the output.
    logging.getLogger(LOGGER_NAME).addHandler(logging.NullHandler())

    with tempfile.TemporaryDirectory() as tempDir:
        config = AppConfig(
            host="example.brightspace.com",
            proxyUrl="http://127.0.0.1:8080",
            clientId="client-id",
            clientSecret="client-secret",
            redirectUri="https://localhost:8443/callback",
            workingDir=Path(tempDir),
        )
        db = MetricsDb(config.dbPath)
        api = StandInD2l()
        try:
            scanResult = scanDemoStudents(api, config, db)
            stateResult = confirmCourseStates(api, db)
            orphans = findOrphansFromInventory(db)

            print("\nResult")
            for orphan in orphans:
                print(f"  orphaned:      {orphan.userName} "
                      f"(course {orphan.orgUnitId} was deleted)")
            print(f"  live:          {plural(stateResult.live, 'course')} still there")
            print(f"  unconfirmable: {plural(stateResult.unknown, 'course')} could not "
                  f"be checked, so the demo students were left alone")
            print(f"  unparsed:      {plural(len(scanResult.unparsed), 'account')} "
                  f"matching the prefix with no org unit id")
            print("\n  delete-orphans would deactivate only the orphaned one.")
        finally:
            db.close()


if __name__ == "__main__":
    main()
