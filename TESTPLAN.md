# Test Plan

Manual end-to-end verification checklist. Run against a **test** D2L instance
first. Keep this document current whenever commands or functionality change.

Automated coverage: `uv run pytest` must pass with pristine output before any
manual testing.

## Setup

- [ ] `uv sync` completes without errors
- [ ] `cp config.example.toml config.toml`, fill in test-instance values
- [ ] `uv run demo-student --help` shows the four subcommands
- [ ] Running any command with a missing config file prints a clear error and exits 2

## auth

- [ ] `uv run demo-student auth` (with mkcert installed): browser opens,
      sign-in completes, "Sign-in complete." page appears, CLI prints the
      authenticated identity, `working/token/token.json` exists with mode 600
- [ ] `uv run demo-student auth --manual`: URL prints, pasting the redirect
      URL back stores tokens and prints the authenticated identity
- [ ] With mkcert **not** on PATH, `uv run demo-student auth` falls back to
      the manual flow with a notice
- [ ] Pasting a redirect URL with a wrong/stale `state` fails with a clear error
- [ ] After deleting `working/token/token.json`, `run` exits 2 with a message that
      says to run `demo-student auth`

## run

- [ ] `uv run demo-student run --ou-list <test-course-ou> --dry-run` reports
      `wouldCreateAndEnroll` and creates/enrolls nothing (verify in D2L UI)
- [ ] `uv run demo-student run --ou-list <test-course-ou>` creates user
      `demo.<ou>` with the Demo Student role and enrolls it (verify in D2L UI
      classlist)
- [ ] Re-running the same command reports the course as skipped and makes no
      API changes
- [ ] A course whose demo user exists but is unenrolled gets `enrolledExistingUser`
      (unenroll the demo user in the UI, delete its row from `completedCourses`,
      re-run)
- [ ] `uv run demo-student run --semesters 1` discovers course offerings from
      only the most recent semester
- [ ] `uv run demo-student run --workers 16` completes noticeably faster on a
      large pending set
- [ ] Exit code is 0 on a clean run (`echo $?`)
- [ ] Ctrl+C during a run cancels pending courses without a traceback, records
      the run as `interrupted` (visible in `status`), exits 130, and courses
      completed before the interrupt are skipped on the next run
- [ ] Force an error (e.g. temporarily set a bogus `roleId`): run exits 1,
      continues past the failing course, and the error appears in
      `status --errors`; the failed course is retried on the next run

## status

- [ ] `uv run demo-student status` lists recent runs with counts matching the
      run summaries
- [ ] `uv run demo-student status --errors` lists recorded errors (or
      "No errors recorded.")
- [ ] `uv run demo-student status --limit 1` shows only the newest row

## import-legacy

- [ ] `uv run demo-student import-legacy <legacy-file>` reports the number of
      ids imported
- [ ] Importing the same file again reports 0 new courses
- [ ] A file with a malformed line is rejected with the line number, exits 2
- [ ] After import, `run` skips the imported courses

## Log files

- [ ] After any `run`, `working/logs/demoStudent_YYYYMM.log` exists and contains
      timestamped "Run N started" / "Run N finished" lines
- [ ] Course errors appear in the log file with org unit id, course code, and
      the error message
- [ ] Fatal errors (e.g. `run` without stored tokens) are appended to the log file
- [ ] A second run in the same month appends to the same file (no duplicates,
      no new file)
- [ ] Setting `logDir` in config.toml to a relative path writes logs under
      `working/{logDir}/` instead of `working/logs/`
- [ ] Setting `logDir` to an absolute path writes logs there directly
- [ ] Setting `logFilePrefix` in config.toml changes the log file name to
      `{logFilePrefix}_YYYYMM.log`

## Database

- [ ] `working/db/demoStudent.db` contains `completedCourses`, `runs`, `errors`
      tables (`sqlite3 working/db/demoStudent.db .tables`)
- [ ] Timestamps are ISO-8601 UTC
- [ ] Retention: backdate a row past one year (e.g.
      `sqlite3 working/db/demoStudent.db "UPDATE runs SET startedAt='2024-01-01T00:00:00+00:00' WHERE runId=1"`),
      run again, and confirm the old row is gone from `status`
