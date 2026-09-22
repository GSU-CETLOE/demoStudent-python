# Test Plan

Manual end-to-end verification checklist. Run against a **test** D2L instance
first. Keep this document current whenever commands or functionality change.

Automated coverage: `uv run pytest` must pass with pristine output before any
manual testing.

## Setup

- [ ] `uv sync` completes without errors
- [ ] `cp config.example.toml config.toml`, fill in test-instance values
- [ ] `uv run demo-student --help` shows the seven subcommands
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

## whoami

- [ ] `uv run demo-student whoami` prints the host, name, username, user id,
      and token file path of the account signed in via `auth` (matches the
      account shown in the D2L UI)
- [ ] After deleting `working/token/token.json`, `whoami` exits 2 with a
      message that says to run `demo-student auth`

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
- [ ] `uv run demo-student run --dry-run` (no `--ou-list`) discovers every
      course offering in the organization — the discovered count matches the
      course offering total in the D2L UI, not just the current semesters
- [ ] The discovered set includes courses with no semester parent: pick a
      building section or sandbox course (parented to a course template) and
      confirm it appears in the run output or `completedCourses`
- [ ] `uv run demo-student run --workers 16` completes noticeably faster on a
      large pending set
- [ ] `uv run demo-student run --recheck` re-processes every course already
      marked complete (summary shows `0 already completed`, all courses
      `to check`) and reports `alreadyEnrolled` for each with no API changes
      (verify in D2L UI)
- [ ] `uv run demo-student run --ou-list <test-course-ou> --recheck` re-checks
      only that one course, leaving other completed courses' rows in
      `completedCourses` untouched
- [ ] After unenrolling the demo user in the D2L UI (without touching the
      local database), `run --ou-list <test-course-ou> --recheck` notices it's
      missing and reports `enrolledExistingUser` or `createdAndEnrolled`,
      re-enrolling it
- [ ] Exit code is 0 on a clean run (`echo $?`)
- [ ] Ctrl+C during a run cancels pending courses without a traceback, records
      the run as `interrupted` (visible in `status`), exits 130, and courses
      completed before the interrupt are skipped on the next run
- [ ] Force an error (e.g. temporarily set a bogus `roleId`): run exits 1,
      continues past the failing course, and the error appears in
      `status --errors`; the failed course is retried on the next run

## find-orphans

Read-only throughout: no step here should change anything in D2L.

- [ ] **Verify the recycle bin behavior first.** Delete a throwaway course
      offering that has a demo student, then run
      `uv run demo-student find-orphans --ou-list <that-ou>`. If the demo
      student is reported, the org unit lookup returns 404 for recycled
      courses and detection works. If it is not reported, the lookup is
      returning 200 and detection only finds courses purged from the
      recycle bin — note this in README.md before relying on the cleanup
- [ ] `uv run demo-student find-orphans` with no deleted courses reports
      "No orphaned demo students found"
- [ ] After deleting a course that has a demo student, `find-orphans`
      reports that course's `demo.{ou}` user as orphaned
- [ ] The demo student still exists and is still active in the D2L UI
      afterwards (the command changed nothing)
- [ ] `uv run demo-student find-orphans --out working/orphans.csv` writes a
      CSV with the header `orgUnitId,userName,userId,courseName,courseCode`
      and one row per orphan
- [ ] Running `find-orphans` twice reports the same orphans both times
      (no row is consumed by reporting)
- [ ] Revoke the service account's access to one course (or use a course it
      cannot see), confirm that course is counted under "Unconfirmable"
      and **not** reported as orphaned
- [ ] The summary's "Unconfirmable" line is followed by the warning about
      courses the account cannot see
- [ ] Temporarily clear most of `completedCourses`' matching live courses
      (or point `--ou-list` at a narrow set) so more than 25 courses and
      more than 5% look orphaned: the command exits 2 and explains the cap
- [ ] The same scan with `--max-candidate-ratio 1.0` proceeds instead
- [ ] `uv run demo-student find-orphans --ou-list <ou>` limits the scan to
      that org unit
- [ ] With an empty `completedCourses` table, the command reports nothing
      to scan and exits 0

## delete-orphans

Run against a **test** instance. Deactivation is reversible; `--hard-delete`
is not.

- [ ] `uv run demo-student delete-orphans` (no `--confirm`) lists the
      orphans, says nothing was changed, and names `--confirm`
- [ ] The demo student is still active in the D2L UI afterwards
- [ ] `uv run demo-student delete-orphans --confirm` reports
      "Deactivated: N" and the demo student shows as **inactive** in the
      D2L UI, still present, with its enrollments intact
- [ ] The deactivated user's `completedCourses` row is gone
      (`sqlite3 working/db/demoStudent.db "SELECT * FROM completedCourses WHERE orgUnitId=<ou>"`
      returns nothing)
- [ ] A following `find-orphans` reports nothing for that course
- [ ] Reactivating the user in the D2L UI restores it fully (confirming
      the default action is reversible)
- [ ] `uv run demo-student delete-orphans --confirm --hard-delete` prints
      the support-request warning **before** acting, then removes the user
      from D2L entirely
- [ ] With a course the account cannot see, `delete-orphans --confirm`
      touches nothing and counts it under "Unconfirmable"
- [ ] Force a failure (e.g. remove the `Can Activate or Deactivate Users`
      permission): the command exits 1, reports it under "Failed", leaves
      the `completedCourses` row in place, and records the failure under
      the `deleteOrphan` phase in `status --errors`
- [ ] With two orphans where only one fails, the other still succeeds
- [ ] `--out` writes the CSV even when `--confirm` is given
- [ ] Log file records the deactivation or deletion of each user with its
      org unit id

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
