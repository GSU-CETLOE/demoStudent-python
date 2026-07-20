"""Command-line interface for the demo student tool.

Commands:
    demo-student auth [--manual]        obtain and store OAuth2 tokens
    demo-student run [options]          add demo students to course offerings
    demo-student status [--errors]      show recent runs and errors
    demo-student import-legacy FILE     seed completed courses from the legacy list
"""

import argparse
import logging
import sys
from pathlib import Path

from .auth import AuthError, TokenManager, TokenStore, obtainTokensInteractively
from .config import AppConfig, ConfigError, loadConfig
from .d2lApi import D2lApiClient, D2lApiError
from .db import MetricsDb
from .logs import LOGGER_NAME, setupFileLogging
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

    runParser = subparsers.add_parser(
        "run", help="Check courses and add missing demo students")
    runParser.add_argument("--semesters", type=int, metavar="N",
                           help="Number of most recent semesters to process")
    runParser.add_argument("--ou-list", metavar="ID,ID",
                           help="Comma-separated org unit ids to process instead of semesters")
    runParser.add_argument("--workers", type=int, metavar="N",
                           help="Worker thread count (default from config)")
    runParser.add_argument("--dry-run", action="store_true",
                           help="Report what would change without creating or enrolling anyone")

    statusParser = subparsers.add_parser(
        "status", help="Show recent runs and metrics from the database")
    statusParser.add_argument("--errors", action="store_true", help="Show recent errors instead")
    statusParser.add_argument("--limit", type=int, default=10, help="Rows to show (default: 10)")

    importParser = subparsers.add_parser(
        "import-legacy", help="Seed completed courses from the legacy OU-list text file")
    importParser.add_argument("file", help="Path to the legacy file (one org unit id per line)")

    return parser


def cmdAuth(config: AppConfig, manual: bool) -> int:
    """Run an interactive OAuth flow, then verify the token with whoami."""
    setupFileLogging(config.workingDir, config.logDir, config.logFilePrefix)
    obtainTokensInteractively(config, manual=manual)
    api = D2lApiClient(config, TokenManager(config, TokenStore(config.tokenPath)))
    try:
        identity = api.whoAmI()
    finally:
        api.close()
    identityName = f"{identity.get('FirstName', '')} {identity.get('LastName', '')}".strip()
    print(f"Authenticated to {config.host} as {identityName}")
    print(f"Tokens stored in {config.tokenPath}")
    fileLogger.info(f"Authenticated to {config.host} as {identityName}")
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
            recentSemesterCount=args.semesters,
            maxWorkers=args.workers,
            dryRun=args.dry_run,
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
    if summary.status == "interrupted":
        return 130
    return 0 if summary.errorCount == 0 else 1


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
        if args.command == "run":
            return cmdRun(config, args)
        if args.command == "status":
            return cmdStatus(config, showErrors=args.errors, limit=args.limit)
        if args.command == "import-legacy":
            return cmdImportLegacy(config, args.file)
        raise AssertionError(f"Unhandled command: {args.command}")
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
