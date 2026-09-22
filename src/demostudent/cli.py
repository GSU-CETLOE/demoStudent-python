"""Command-line interface for the demo student tool.

Commands:
    demo-student auth [--manual]        obtain and store OAuth2 tokens
    demo-student whoami                 show the D2L account the stored tokens act as
    demo-student run [options]          add demo students to every course offering
                                         in the organization (--recheck re-verifies
                                         already-completed courses instead of
                                         skipping them)
    demo-student status [--errors]      show recent runs and errors
    demo-student find-orphans [--out]   report demo students whose course
                                         offering has been deleted
    demo-student delete-orphans --confirm [--hard-delete]
                                        deactivate (or delete) the demo students
                                         find-orphans reports; reports only
                                         without --confirm
    demo-student import-legacy FILE     seed completed courses from the legacy list
"""

import argparse
import csv
import logging
import sys
from pathlib import Path
from typing import Any

from .auth import AuthError, TokenManager, TokenStore, obtainTokensInteractively
from .config import AppConfig, ConfigError, loadConfig
from .d2lApi import D2lApiClient, D2lApiError
from .db import MetricsDb
from .logs import LOGGER_NAME, setupFileLogging
from .orphans import (DEFAULT_MAX_CANDIDATE_RATIO, OrphanReport, OrphanScanAborted,
                      deleteOrphanedDemoStudents, findOrphanedDemoStudents)
from .runner import runEnrollment

fileLogger = logging.getLogger(LOGGER_NAME)


def _logFatal(message: str) -> None:
    """Record a fatal error in the log file when logging is set up."""
    if fileLogger.handlers:
        fileLogger.error(message)


def buildParser() -> argparse.ArgumentParser:
    """Define the demo-student command line."""
    parser = argparse.ArgumentParser(
        prog="demo-student",
        description="Adds a unique Demo Student user to each D2L Brightspace course offering.",
    )
    parser.add_argument("--config", default="config.toml",
                        help="Path to the TOML config file (default: config.toml)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    authParser = subparsers.add_parser(
        "auth", help="Sign in to D2L and store OAuth2 tokens")
    authParser.add_argument("--manual", action="store_true",
                            help="Paste-the-URL flow for machines without mkcert or a browser")

    subparsers.add_parser(
        "whoami", help="Show which D2L account the stored tokens authenticate as")

    runParser = subparsers.add_parser(
        "run", help="Check courses and add missing demo students")
    runParser.add_argument("--ou-list", metavar="ID,ID",
                           help="Comma-separated org unit ids to process instead of "
                                "the whole organization")
    runParser.add_argument("--workers", type=int, metavar="N",
                           help="Worker thread count (default from config)")
    runParser.add_argument("--dry-run", action="store_true",
                           help="Report what would change without creating or enrolling anyone")
    runParser.add_argument("--recheck", action="store_true",
                           help="Re-verify in-scope courses even if already marked complete "
                                "(use with --ou-list to limit scope)")

    orphanParser = subparsers.add_parser(
        "find-orphans",
        help="Report demo students whose course offering no longer exists (changes nothing)")
    orphanParser.add_argument("--ou-list", metavar="ID,ID",
                              help="Comma-separated org unit ids to scan instead of "
                                   "the whole organization")
    orphanParser.add_argument("--out", metavar="PATH",
                              help="Write the orphan list to a CSV file as well")
    orphanParser.add_argument("--max-candidate-ratio", type=float,
                              default=DEFAULT_MAX_CANDIDATE_RATIO, metavar="RATIO",
                              help="Abort if more than this share of completed courses "
                                   f"look orphaned (default: {DEFAULT_MAX_CANDIDATE_RATIO}); "
                                   "1.0 disables the check")

    deleteOrphanParser = subparsers.add_parser(
        "delete-orphans",
        help="Deactivate demo students whose course offering no longer exists "
             "(reports only without --confirm)")
    deleteOrphanParser.add_argument("--confirm", action="store_true",
                                    help="Actually act; without this the command only reports")
    deleteOrphanParser.add_argument("--hard-delete", action="store_true",
                                    help="Permanently delete instead of deactivating. D2L "
                                         "reverses this only by support request, within 30 "
                                         "days, and only while the username is unused")
    deleteOrphanParser.add_argument("--ou-list", metavar="ID,ID",
                                    help="Comma-separated org unit ids to scan instead of "
                                         "the whole organization")
    deleteOrphanParser.add_argument("--out", metavar="PATH",
                                    help="Write the orphan list to a CSV file as well")
    deleteOrphanParser.add_argument("--max-candidate-ratio", type=float,
                                    default=DEFAULT_MAX_CANDIDATE_RATIO, metavar="RATIO",
                                    help="Abort if more than this share of completed courses "
                                         f"look orphaned (default: "
                                         f"{DEFAULT_MAX_CANDIDATE_RATIO}); 1.0 disables the check")

    statusParser = subparsers.add_parser(
        "status", help="Show recent runs and metrics from the database")
    statusParser.add_argument("--errors", action="store_true", help="Show recent errors instead")
    statusParser.add_argument("--limit", type=int, default=10, help="Rows to show (default: 10)")

    importParser = subparsers.add_parser(
        "import-legacy", help="Seed completed courses from the legacy OU-list text file")
    importParser.add_argument("file", help="Path to the legacy file (one org unit id per line)")

    return parser


def _fetchIdentity(config: AppConfig) -> dict[str, Any]:
    """Ask D2L which user the stored tokens act as."""
    api = D2lApiClient(config, TokenManager(config, TokenStore(config.tokenPath)))
    try:
        return api.whoAmI()
    finally:
        api.close()


def _identityName(identity: dict[str, Any]) -> str:
    """Display name from a whoami response."""
    return f"{identity.get('FirstName', '')} {identity.get('LastName', '')}".strip()


def cmdAuth(config: AppConfig, manual: bool) -> int:
    """Run an interactive OAuth flow, then verify the token with whoami."""
    setupFileLogging(config.workingDir, config.logDir, config.logFilePrefix)
    obtainTokensInteractively(config, manual=manual)
    identityName = _identityName(_fetchIdentity(config))
    print(f"Authenticated to {config.host} as {identityName}")
    print(f"Tokens stored in {config.tokenPath}")
    fileLogger.info(f"Authenticated to {config.host} as {identityName}")
    return 0


def cmdWhoami(config: AppConfig) -> int:
    """Print the D2L account the stored tokens make API calls as."""
    identity = _fetchIdentity(config)
    print(f"  Host:       {config.host}")
    print(f"  Name:       {_identityName(identity)}")
    print(f"  Username:   {identity.get('UniqueName', '-')}")
    print(f"  User id:    {identity.get('Identifier', '-')}")
    print(f"  Token file: {config.tokenPath}")
    return 0


def cmdRun(config: AppConfig, args: argparse.Namespace) -> int:
    """Execute one enrollment pass and print the summary."""
    setupFileLogging(config.workingDir, config.logDir, config.logFilePrefix)
    ouList = None
    if args.ou_list:
        ouList = [int(orgUnitId) for orgUnitId in args.ou_list.split(",") if orgUnitId.strip()]

    db = MetricsDb(config.dbPath)
    api = D2lApiClient(config, TokenManager(config, TokenStore(config.tokenPath)))
    try:
        summary = runEnrollment(
            config, api, db,
            ouList=ouList,
            maxWorkers=args.workers,
            dryRun=args.dry_run,
            recheck=args.recheck,
        )
    finally:
        api.close()
        db.close()

    print("\nRun summary")
    print(f"  Status:               {summary.status}")
    print(f"  Courses discovered:   {summary.coursesDiscovered}")
    print(f"  Courses skipped:      {summary.coursesSkipped}")
    print(f"  Courses checked:      {summary.coursesChecked}")
    print(f"  Users created:        {summary.usersCreated}")
    print(f"  Enrollments created:  {summary.enrollmentsCreated}")
    print(f"  Errors:               {summary.errorCount}")
    print(f"  Finished (UTC):       {summary.finishedAt}")
    print(f"  Elapsed:              {summary.elapsed}")
    if summary.status == "interrupted":
        return 130
    return 0 if summary.errorCount == 0 else 1


def _parseOuList(rawOuList: str | None) -> list[int] | None:
    """Comma-separated org unit ids from the command line, or None for all."""
    if not rawOuList:
        return None
    return [int(orgUnitId) for orgUnitId in rawOuList.split(",") if orgUnitId.strip()]


def _writeOrphanCsv(report: OrphanReport, outPath: Path) -> None:
    """Write the orphan list to CSV so it can be reviewed before deleting."""
    outPath.parent.mkdir(parents=True, exist_ok=True)
    with outPath.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["orgUnitId", "userName", "userId", "courseName", "courseCode"])
        for orphan in report.orphans:
            writer.writerow([orphan.orgUnitId, orphan.userName, orphan.userId,
                             orphan.courseName or "", orphan.courseCode or ""])


def _printOrphanSummary(report: OrphanReport) -> None:
    """Print what the scan found, including what it deliberately left alone."""
    print("\nOrphan scan summary")
    print(f"  Completed courses:    {report.completedCourseCount}")
    print(f"  Candidates confirmed: {report.candidatesChecked}")
    print(f"  Orphaned:             {len(report.orphans)}")
    print(f"  Already gone:         {len(report.alreadyGone)}")
    print(f"  Unconfirmable:        {len(report.unreachable)}")
    if report.unreachable:
        print(f"\n  {len(report.unreachable)} could not be confirmed and were left alone. "
              f"A course the service account cannot see is not a deleted course; "
              f"check the access requirements in README.md.")


def cmdFindOrphans(config: AppConfig, args: argparse.Namespace) -> int:
    """Report demo students whose course offering no longer exists.

    Read-only: nothing in D2L or the database is changed. Use
    delete-orphans to act on what this reports.
    """
    setupFileLogging(config.workingDir, config.logDir, config.logFilePrefix)
    db = MetricsDb(config.dbPath)
    api = D2lApiClient(config, TokenManager(config, TokenStore(config.tokenPath)))
    try:
        report = findOrphanedDemoStudents(
            api, config, db,
            ouList=_parseOuList(args.ou_list),
            maxCandidateRatio=args.max_candidate_ratio,
        )
    finally:
        api.close()
        db.close()

    if not report.orphans:
        print("\nNo orphaned demo students found.")
    _printOrphanSummary(report)

    if args.out:
        outPath = Path(args.out)
        _writeOrphanCsv(report, outPath)
        print(f"\n  Report written to {outPath}")

    fileLogger.info(
        f"find-orphans: {len(report.orphans)} orphaned, "
        f"{len(report.alreadyGone)} already gone, "
        f"{len(report.unreachable)} unconfirmable, "
        f"of {report.completedCourseCount} completed courses")
    return 0


def cmdDeleteOrphans(config: AppConfig, args: argparse.Namespace) -> int:
    """Delete demo students whose course offering no longer exists.

    Scans exactly as find-orphans does, then deletes only the orphans that
    scan confirmed. Without --confirm it reports and stops, so the same
    command shows what it would do before it does it.
    """
    setupFileLogging(config.workingDir, config.logDir, config.logFilePrefix)
    db = MetricsDb(config.dbPath)
    api = D2lApiClient(config, TokenManager(config, TokenStore(config.tokenPath)))
    try:
        report = findOrphanedDemoStudents(
            api, config, db,
            ouList=_parseOuList(args.ou_list),
            maxCandidateRatio=args.max_candidate_ratio,
        )

        if args.out:
            _writeOrphanCsv(report, Path(args.out))

        if not report.orphans:
            print("\nNo orphaned demo students found; nothing to delete.")
            _printOrphanSummary(report)
            return 0

        verb = "delete" if args.hard_delete else "deactivate"
        if not args.confirm:
            _printOrphanSummary(report)
            print(f"\n  Nothing was changed. Re-run with --confirm to {verb} these "
                  f"{len(report.orphans)} demo students.")
            return 0

        if args.hard_delete:
            print("\n  Hard delete: D2L reverses this only by support request, within 30 "
                  "days, and only while no new account has taken the same username.")

        print(f"\n{verb.capitalize()[:-1]}ing {len(report.orphans)} orphaned demo students...")
        fileLogger.info(f"delete-orphans: about to {verb} {len(report.orphans)} "
                        f"orphaned demo students")
        outcome = deleteOrphanedDemoStudents(api, db, report, hardDelete=args.hard_delete)
    finally:
        api.close()
        db.close()

    print("\nCleanup summary")
    print(f"  Completed courses:    {report.completedCourseCount}")
    print(f"  Candidates confirmed: {report.candidatesChecked}")
    print(f"  Orphaned:             {len(report.orphans)}")
    actionLabel = f"{outcome.actionName.capitalize()}:"
    print(f"  {actionLabel:<21} {outcome.deleted}")
    print(f"  Failed:               {outcome.failed}")
    print(f"  Unconfirmable:        {len(report.unreachable)}")
    if args.out:
        print(f"\n  Report written to {args.out}")
    fileLogger.info(f"delete-orphans finished: {outcome.deleted} {outcome.actionName}, "
                    f"{outcome.failed} failed")
    return 0 if outcome.failed == 0 else 1


def cmdStatus(config: AppConfig, showErrors: bool, limit: int) -> int:
    """Print recent runs or errors from the database."""
    db = MetricsDb(config.dbPath)
    try:
        if showErrors:
            errors = db.recentErrors(limit=limit)
            if not errors:
                print("No errors recorded.")
                return 0
            for error in errors:
                orgUnit = error["orgUnitId"] if error["orgUnitId"] is not None else "-"
                status = error["httpStatus"] if error["httpStatus"] is not None else "-"
                print(f"[{error['occurredAt']}] run {error['runId']} "
                      f"ou {orgUnit} {error['phase']} http {status}: {error['message']}")
            return 0

        runs = db.recentRuns(limit=limit)
        if not runs:
            print("No runs recorded yet.")
            return 0
        header = (f"{'run':>4}  {'started':<20} {'status':<20} {'found':>6} {'skip':>6} "
                  f"{'check':>6} {'users':>6} {'enroll':>6} {'errors':>6}")
        print(header)
        for run in runs:
            print(f"{run['runId']:>4}  {run['startedAt']:<20} {run['status']:<20} "
                  f"{run['coursesDiscovered']:>6} {run['coursesSkipped']:>6} "
                  f"{run['coursesChecked']:>6} {run['usersCreated']:>6} "
                  f"{run['enrollmentsCreated']:>6} {run['errorCount']:>6}")
        return 0
    finally:
        db.close()


def cmdImportLegacy(config: AppConfig, filePath: str) -> int:
    """Load org unit ids from the legacy tracking file into completedCourses."""
    path = Path(filePath)
    if not path.is_file():
        raise ConfigError(f"Legacy file not found: {path}")
    orgUnitIds = []
    for lineNumber, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if not line.isdigit():
            raise ConfigError(f"{path}:{lineNumber}: expected an org unit id, got '{line}'")
        orgUnitIds.append(int(line))

    setupFileLogging(config.workingDir, config.logDir, config.logFilePrefix)
    db = MetricsDb(config.dbPath)
    try:
        added = db.importCompletedOrgUnits(orgUnitIds)
    finally:
        db.close()
    message = (f"Imported {added} new completed courses "
               f"({len(orgUnitIds)} ids in file, duplicates ignored).")
    print(message)
    fileLogger.info(f"import-legacy {path}: {message}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the demo-student console script."""
    args = buildParser().parse_args(argv)
    try:
        config = loadConfig(args.config)
        if args.command == "auth":
            return cmdAuth(config, manual=args.manual)
        if args.command == "whoami":
            return cmdWhoami(config)
        if args.command == "run":
            return cmdRun(config, args)
        if args.command == "find-orphans":
            return cmdFindOrphans(config, args)
        if args.command == "delete-orphans":
            return cmdDeleteOrphans(config, args)
        if args.command == "status":
            return cmdStatus(config, showErrors=args.errors, limit=args.limit)
        if args.command == "import-legacy":
            return cmdImportLegacy(config, args.file)
        raise AssertionError(f"Unhandled command: {args.command}")
    except OrphanScanAborted as error:
        print(f"Error: {error}", file=sys.stderr)
        _logFatal(str(error))
        return 2
    except (ConfigError, AuthError) as error:
        print(f"Error: {error}", file=sys.stderr)
        _logFatal(str(error))
        return 2
    except D2lApiError as error:
        print(f"D2L API error: {error}", file=sys.stderr)
        _logFatal(f"D2L API error: {error}")
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        _logFatal("Interrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
