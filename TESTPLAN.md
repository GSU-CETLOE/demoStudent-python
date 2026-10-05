# Test Plan

Manual end-to-end verification checklist. Run against a **test** D2L instance
first. Keep this document current whenever commands or functionality change.

Automated coverage: `uv run pytest` must pass with pristine output before any
manual testing.

## Setup

- [ ] `uv sync` completes without errors
- [ ] `cp config.example.toml config.toml`, fill in test-instance values
- [ ] With `proxyUrl = "127.0.0.1:8080"` (no scheme), any command exits 2
      and says it must start with `http://` or `https://`
- [ ] With `proxyKey` set and `proxyUrl` commented out, any command exits 2
      and says `proxyKey is set without proxyUrl`
- [ ] With `proxyUser` set and `proxyKey` commented out, any command exits 2
      and says `proxyUser is set without proxyKey`
- [ ] With `proxyUrl` and `proxyKey` set and `clientId`, `clientSecret`, and
      `redirectUri` removed, `uv run demo-student status` runs. Without
      `proxyKey`, the same config exits 2 naming the three missing values
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
- [ ] With `proxyKey` set, `uv run demo-student auth` exits 2, opens no
      browser, and points at `apiproxy users authorize`

## whoami

- [ ] `uv run demo-student whoami` prints the host, proxy URL, name, username,
      user id, and token file path of the account signed in via `auth` (matches the
      account shown in the D2L UI)
- [ ] After deleting `working/token/token.json`, `whoami` exits 2 with a
      message that says to run `demo-student auth`

## Route to D2L

The rest of this plan runs in any of the three modes below. Work through it
once in the mode production will use before relying on it there.

Direct, with no `proxyUrl` in `config.toml`:

- [ ] `whoami` shows "Proxy: none, calling D2L directly" and succeeds with
      apiProxy2 stopped
- [ ] With `host` temporarily set to `nonexistent.invalid`, `whoami` exits 1
      with "Could not reach D2L at https://nonexistent.invalid/d2l/api." and
      no mention of apiProxy2. Put `host` back afterwards

Through the proxy, with apiProxy2 running in passthrough mode and
`proxyUrl` in `config.toml` pointing at it. In apiProxy2's `.env`,
`D2L_PROXY_AUTH_ENABLED` is off and `D2L_TENANT_HOST` is the test instance:

- [ ] While `whoami` runs, the proxy's log shows each request with
      `"cache":"BYPASS"`: the tool never reads from or fills the cache
- [ ] Stop the proxy. `whoami` exits 1 with "Could not reach the API proxy
      at <proxyUrl>. Is apiProxy2 running?" and no traceback
- [ ] With the proxy still stopped, `run --dry-run` exits 1 with the same
      message
- [ ] With the proxy still stopped and an inventory already built,
      `find-orphans` exits 1 with the same message, rather than listing
      every course as "cannot confirm"
- [ ] Start the proxy again and set `proxyUrl` to
      `http://127.0.0.1:8080/d2l/api` (a wrong base). `find-orphans` exits 1
      with "Could not negotiate API versions" and reports no orphans. Put
      the URL back afterwards
- [ ] `find-orphans --rescan` with Data Sets available: the proxy's log
      shows the `datasets/bds` listing, the extracts listing, and the
      download request. The download from storage itself does not appear
      there, because the tool follows D2L's redirect to storage directly

Through the proxy with proxy authentication. On apiProxy2,
`D2L_PROXY_AUTH_ENABLED=true`, the service account has been authorized with
`apiproxy users authorize`, and this tool is registered with
`apiproxy apps create` and granted that account. In `config.toml`,
`proxyUrl` and `proxyKey` are set and `clientId`, `clientSecret`, and
`redirectUri` are removed:

- [ ] With `working/token/token.json` deleted, `whoami` succeeds, shows the
      service account, and ends with "Token: held by apiProxy2, requested
      with proxyKey". The key itself is not printed
- [ ] While `whoami` runs, the proxy's log names the application and the
      account on each request and still shows `"cache":"BYPASS"`
- [ ] `run --ou-list <test-course-ou> --dry-run` completes, and no
      `working/token/` directory is created
- [ ] Change one character of `proxyKey`. `whoami` exits 1 with HTTP 401
      and `proxyAuthorizationRequired`. Put the key back afterwards
- [ ] Set `proxyUser` to the granted account: `whoami` still succeeds. Set
      it to an account the application was not granted: `whoami` exits 1
      with HTTP 403 and `usernameNotAllowed`. Remove `proxyUser` afterwards
- [ ] Grant the application a second account on the proxy and leave
      `proxyUser` unset: `whoami` exits 1 with HTTP 400 and
      `iCollegeUserRequired`. Set `proxyUser` and it succeeds. Revoke the
      second grant afterwards
- [ ] With `D2L_PROXY_AUTH_ENABLED` turned off on the proxy and `proxyKey`
      still set here, `whoami` exits 1 with HTTP 401 and
      `authorizationRequired`
- [ ] With `D2L_PROXY_AUTH_ENABLED=true` on the proxy and `proxyKey`
      commented out here (OAuth settings restored, tokens stored), `whoami`
      exits 1 with HTTP 401 and `proxyAuthorizationRequired`

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

Read-only against D2L throughout. Note that several steps below ask you
to delete a course in the Brightspace UI. That is you setting up a test
case by hand. **The tool itself never deletes a course**, and has no
code path that could: every org unit route it calls is a read.

- [ ] **Verify the recycle bin behavior first.** *You* delete the course
      here, by hand in the Brightspace UI; the tool never deletes courses.
      Pick a throwaway course offering that has a demo student, delete it
      yourself, then run `uv run demo-student find-orphans --rescan`. If the demo student is
      reported, the org unit lookup returns 404 for recycled courses and
      detection works. If it is not reported, the lookup returns 200 and
      detection only finds courses purged from the recycle bin. Note that
      in README.md before relying on the cleanup
- [ ] `uv run demo-student find-orphans --rescan` uses Brightspace Data
      Sets when the tenant has it: the output names the extract's
      generation date and reports "Users read" rather than "Users walked"
- [ ] The generation date is within about a week. A much older date means
      extract generation is failing on the tenant
- [ ] On an account without `Can Access Brightspace Data Sets`, the same
      command says Data Sets is unavailable, falls back to the live API,
      and still finds the same orphans
- [ ] `--api-walk` skips Data Sets entirely and reports "Users walked"
- [ ] The downloaded extract lands in `working/datasets/` named for its
      generation date
- [ ] A second `--rescan` in the same week reuses that file rather than
      downloading again ("Using the Users extract already downloaded")
- [ ] **Role gate.** Create an account named `demo.999999` by hand with
      an ordinary student role, delete nothing, and run
      `find-orphans --rescan`. It is counted as left alone, not reported
      as an orphan. Repeat with `--api-walk` and confirm the live path
      cannot make that distinction (it has no role to check)
- [ ] The first `find-orphans --rescan` prints progress while it runs:
      a line naming the host before the walk, periodic "walked N users"
      lines, then "Checking N courses" and periodic "checked N of M"
      lines. It should never sit silent for more than a few seconds
- [ ] Every progress and result line starts with a timestamp matching the
      log file format (`2026-09-22 18:45:12`)
- [ ] Summary blocks and the `status` table keep their column alignment,
      with the timestamp on the block header rather than each row
- [ ] `uv run demo-student whoami`, `status`, and `import-legacy` all
      carry timestamps too
- [ ] An error (e.g. a missing config file) is timestamped on stderr
- [ ] Progress still appears when the output is redirected
      (`uv run demo-student find-orphans --rescan > run.log &` then
      `tail -f run.log`), rather than arriving all at once at the end
- [ ] **Check the scan sees the whole tenant.** The first
      `find-orphans --rescan` prints "Users walked" and "Demo students
      found". Compare the demo student count against what you expect for
      the tenant. A count far below expectation means the service account
      is not seeing everything, which D2L does not report as an error
- [ ] On a service account without **See Usernames**, the command exits 2
      and names the permission rather than reporting zero orphans
- [ ] `uv run demo-student find-orphans` with nothing deleted reports
      "No orphaned demo students found"
- [ ] After you delete a course that has a demo student (by hand, in the
      Brightspace UI), `find-orphans --rescan` reports that `demo.{ou}` user
- [ ] The demo student still exists and is still active in the D2L UI
      afterwards (the command changed nothing)
- [ ] A second `find-orphans` without `--rescan` reuses the cached
      inventory and does not re-walk the user list (noticeably faster)
- [ ] `--rescan` walks again and prints "Users walked"
- [ ] `uv run demo-student find-orphans --out working/orphans.csv` writes a
      CSV with the header `orgUnitId,userName,userId` and one row per orphan
- [ ] A course the service account cannot see is counted under "Courses
      unconfirmed" and **not** reported as orphaned
- [ ] An account matching the prefix with no usable org unit id (create
      `demo.legacy` by hand) is counted as unparsed and never reported as
      an orphan
- [ ] `sqlite3 working/db/demoStudent.db "SELECT COUNT(*) FROM demoStudents"`
      matches the reported demo student count

## delete-orphans

Run against a **test** instance. Deactivation is reversible; `--hard-delete`
is not.

- [ ] `uv run demo-student delete-orphans` (no `--confirm`) lists the
      orphans, says nothing was changed, and names `--confirm`
- [ ] The demo student is still active in the D2L UI afterwards
- [ ] `uv run demo-student delete-orphans --confirm` reports
      "Deactivated: N" and the demo student shows as **inactive** in the
      D2L UI, still present, with its enrollments intact
- [ ] A following `find-orphans` does not report that account again
- [ ] `sqlite3 working/db/demoStudent.db "SELECT isActive FROM demoStudents WHERE userId=<id>"`
      returns 0
- [ ] Reactivating the user in the D2L UI restores it fully (confirming
      the default action is reversible)
- [ ] `uv run demo-student delete-orphans --confirm --hard-delete` prints
      the support-request warning **before** acting, then removes the user
      from D2L entirely, and its `demoStudents` row is gone
- [ ] With a course the account cannot see, `delete-orphans --confirm`
      touches nothing and counts it under "Courses unconfirmed"
- [ ] Force a failure (e.g. remove the `Can Activate or Deactivate Users`
      permission): the command exits 1, reports it under "Failed", leaves
      the inventory row in place, and records the failure under the
      `deleteOrphan` phase in `status --errors`
- [ ] The failed account is offered again on the next `find-orphans`
- [ ] With two orphans where only one fails, the other still succeeds
- [ ] Log file records each deactivation or deletion with its org unit id

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
- [ ] Retention exemption: backdate a `demoStudents` row the same way,
      run again, and confirm it is **still there**. Ageing out the
      inventory would hide the oldest orphans
