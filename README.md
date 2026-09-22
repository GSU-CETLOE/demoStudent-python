# demoStudent

Adds a unique **Demo Student** user to every D2L Brightspace course offering.
Each course gets its own user named `demo.{orgUnitId}`, created with the Demo
Student role and enrolled in that course. Courses that already have their demo
student are recorded in a local SQLite database and skipped on future runs,
unless re-checked with `--recheck` (e.g. to confirm a demo student wasn't
removed from a current-semester course). Errors and run metrics are stored in
the same database so a future dashboard can report on them.

When a course offering is deleted, its demo student stays behind.
`find-orphans` reports those leftover accounts and `delete-orphans`
retires them.

This tool replaces the legacy PHP cron script
[gsu-dle/add_demo_user](https://github.com/gsu-dle/add_demo_user), moving from
D2L ID/Key auth to OAuth2 and from a flat tracking file to SQLite.

## Setup

### 1. Install uv

This project uses [uv](https://docs.astral.sh/uv/) for package and script
management. Install it with:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install the project's dependencies:

```sh
uv sync
```

### 2. Install mkcert (optional, for browser sign-in)

The `auth` command starts a local HTTPS server to receive the OAuth redirect.
[mkcert](https://github.com/FiloSottile/mkcert) provides the locally-trusted
certificate:

```sh
brew install mkcert   # macOS
mkcert -install       # one-time: trust the local CA
```

No mkcert or no browser on the machine? Use `demo-student auth --manual`
instead — no local server or certificate is needed.

### 3. Configure

```sh
cp config.example.toml config.toml
```

Fill in `host`, `clientId`, `clientSecret`, and `redirectUri` from your
registered OAuth2 application (D2L Admin Tools > Manage Extensibility >
OAuth 2.0). The registered redirect URI must match `redirectUri` exactly
(e.g. `https://localhost:8443/callback`). The client secret can also be
provided via the `DEMO_STUDENT_CLIENT_SECRET` environment variable instead of
the file. All other settings have defaults — see `config.example.toml`.

Runtime state lives in the `working/` directory (configurable via
`workingDir`; gitignored):

```
working/
  db/demoStudent.db      SQLite database (completed courses, runs, errors)
  token/token.json       OAuth2 token store
  token/token.json.lock  guards token refresh between concurrent processes
  logs/                  monthly log files (dir name via `logDir`)
  localhost.pem(-key)    mkcert certificate for the browser auth flow
```

Two runs sharing a `workingDir` are safe. D2L rotates refresh tokens — using
one invalidates it — so without coordination a second process refreshing would
kill the first process's token mid-run, failing every remaining course with
`invalid_grant`. Refreshes therefore take an inter-process lock, and a process
that finds its own refresh token already spent adopts the pair the other one
wrote instead of failing. The lock is released by the OS if a process dies, so
it cannot go stale.

## Brightspace access requirements

Two independent things must both be granted, and they fail in different ways:
**OAuth2 scopes** decide which routes the application may call at all, while
the **signed-in account's role** decides what it may do in a given org unit.
A correct scope with an inadequate role still returns 403 on every course.

### OAuth2 scopes

`scope` defaults to `core:*:*`, D2L's catch-all. The tool only ever calls the
routes below, so the OAuth2 application can be registered with just these
scopes:

| Route | Scope |
| --- | --- |
| `GET /d2l/api/versions/` | *(none — version negotiation is unauthenticated)* |
| `GET /lp/{v}/users/whoami` | `users:own_profile:read` |
| `GET /lp/{v}/orgstructure/{ou}` | `organizations:organization:read` |
| `GET /lp/{v}/orgstructure/{ou}/descendants/paged/` | `organizations:organization:read` |
| `GET /lp/{v}/users/?userName=` | `users:userdata:read` |
| `GET /lp/{v}/enrollments/users/{userId}/orgUnits/` | `enrollment:orgunit:read` |
| `POST /lp/{v}/users/` | `users:userdata:create` |
| `POST /lp/{v}/enrollments/` | `enrollment:orgunit:create` |
| `PUT /lp/{v}/users/{userId}/activation` | `users:activation:update` |
| `DELETE /lp/{v}/users/{userId}` | `users:userdata:delete` |

The last two are used only by `delete-orphans`. Leave them off if you are
not running it; the other commands work the same either way.

As one scope string (spaces separate scopes, commas separate permissions on the
same resource, with no spaces around the commas):

```
enrollment:orgunit:create,read organizations:organization:read users:activation:update users:own_profile:read users:userdata:create,read,delete
```

Without the orphan cleanup routes:

```
enrollment:orgunit:create,read organizations:organization:read users:own_profile:read users:userdata:create,read
```

Register that on the OAuth2 application, then set `scope` in `config.toml` to
the same value — a requested scope that isn't registered fails authorization.
Scopes are fixed at authorization time and **do not change on refresh**: after
editing `scope`, re-run `demo-student auth` or the stored token keeps the old
set.

### Brightspace role and permissions

Two different roles are involved:

| Role | Who holds it |
| --- | --- |
| The service account's role | The account signed in via `demo-student auth` (at GSU: `CETLOE API`) |
| The demo student role (`roleId`, default 783) | The `demo.{orgUnitId}` users the tool creates |

Configure the service account's role under **Admin Tools > Roles and
Permissions > Edit Permissions**, which grids every permission against the org
unit types it can be granted at.

#### Required permissions

Names below are as they appear in Brightspace and are grouped by tool.

| Tool | Permission | Grant at | Needed for |
| --- | --- | --- | --- |
| Users | **Enroll '{demo student role}'** | Course Offering | `POST /lp/{v}/enrollments/` — enrolling the demo student |
| Users | **Search for '{demo student role}'** | Organization, Course Offering | `GET /lp/{v}/users/?userName=` — finding `demo.{orgUnitId}` |
| Users | **View User Enrollments** | Organization, Semester, Course Template, Course Offering, Department | `GET /lp/{v}/enrollments/users/{userId}/orgUnits/` — the enrollment check |
| Users | **Create a User in the Database** | Organization | `POST /lp/{v}/users/` — creating the demo student |
| Users | View the Users' Email Address, View Users' Contact Information | Organization | the `ExternalEmail` set on creation |
| User Information Privacy | **every row the tool offers** | Organization | `POST /lp/{v}/users/` — D2L only lets a role create a user if it can see every user field |
| Classlist | **Has Access to the Classlist** | Course Offering | enrollment reads and writes inside a course |
| Classlist | **Change Enrollment** | Course Offering | Brightspace's usual pairing for enrollment changes |
| Users | **Can Activate or Deactivate Users** | Organization | `PUT /lp/{v}/users/{userId}/activation`, for `delete-orphans` |
| Users | **Delete a User** | Organization | `DELETE /lp/{v}/users/{userId}`, for `delete-orphans --hard-delete` |
| Users | **See the User Management tool** | Organization | required alongside both of the above |
| Users | **Update a User** | Organization | required alongside `Can Activate or Deactivate Users` |

The last four are needed only for `delete-orphans`, and they have a
dependency that user creation does not. `Create a User in the Database`
stands alone, but `Delete a User` and `Can Activate or Deactivate Users`
both also require `See the User Management tool`, and deactivation wants
`Update a User` on top of that.

`Enroll '...'` and `Search for '...'` are **per-target-role** permissions —
Brightspace lists one of each for every role in the tenant, named after that
role. They must be enabled for the demo student role specifically. Changing
`roleId` in the config means enabling a different pair.

Both sit at the very bottom of the Users tool, one row per role in the tenant,
which leaves the general permissions a long scroll above them. `Create a User
in the Database` is near the top, between `View User Enrollments` and `See the
User Management tool`.

**User Information Privacy is its own tool**, further down the grid, and it is
easy to read as part of Users. D2L's reference for `POST /lp/{v}/users/` says
the action "is subject to user privacy controls" and that "the calling user
context must belong to a role granted permission to see all user fields via
User Information Privacy." Every row it offers at Organization has to be on:

- See First Names, See Middle Names, See Last Names
- See Usernames
- See Org Defined IDs
- See Email Addresses
- See IP Addresses
- Distinguish Preferred Names from Legal Names
- See Name for Anonymized Activities
- See Pronouns

The last two are the ones to look at first — at GSU they were the only two left
unchecked. Granting just the fields this tool sends is not enough, because the
check is on the role rather than on the request. `See External ID Values` is
offered at Course Offering only, so it cannot be granted at Organization and is
not needed.

A role missing any of these fails exactly like a role missing `Create a User in
the Database`: a bare `Not Authorized`, with nothing in the response pointing at
privacy.

#### Course Access Options (easy to miss)

On the role's own settings page, **Course Access Options** must all be enabled:

- Access inactive courses
- Access past courses
- **Access future courses**

These gate the role by the *state* of a course rather than by permission, and
they are set apart from the permissions grid, so they are easy to overlook.
Without them the service account is blind to entire categories of course —
most importantly courses in an upcoming semester, which have not started yet
and are therefore *future* courses. Since the tool's whole job is to seed demo
students into upcoming semesters, this affects exactly the courses it most
needs to reach.

The symptom is indistinguishable from a missing permission: discovery finds the
courses (org structure is readable regardless), then every call inside them
fails with `403 Not Authorized`. Every permission in the table above can be
correctly enabled and the tool will still fail on every affected course.

Worse, the failure is not always loud. The enrollment check reads from the user
side, and Brightspace **silently omits inaccessible courses from that listing
instead of returning an error** — so an existing enrollment in an unreachable
course reads back as "not enrolled", and the tool tries to enroll a user who is
already enrolled. What surfaces is a 403 on the write, which points at the
wrong thing.

To check whether the account can reach a course, request its classlist —
`200` means it has access, `403` means it does not:

```
GET /d2l/api/le/{leVersion}/{orgUnitId}/classlist/
```

Be aware when using the account's own enrollment listing to audit reach that it
is filtered the same way, and so under-reports:

```
GET /d2l/api/lp/{lpVersion}/enrollments/users/{callerUserId}/orgUnits/
```

#### Do not let the demo student role cascade

If the demo student role is made to cascade from the organization down, every
demo student appears to be enrolled in every course. The enrollment check then
reports `alreadyEnrolled` everywhere and the tool stops enrolling anyone, while
appearing to succeed. Cascading is for the service account's role only.

#### Reading a 403

The response body distinguishes the two causes, and a 404 is not a failure at
all:

| Response | Meaning |
| --- | --- |
| `{"Errors":[{"Message":"Not Authorized"}]}` | Role permission **or** Course Access Options — the account has no authority in that org unit |
| `Insufficient scope to call API. Required: <scope>` | OAuth2 scope — D2L names the missing scope; add it and re-run `demo-student auth` |
| `404 {"Errors":[{"Message":"Resource Not Found"}]}` | The account *does* have access; the user simply is not enrolled there. Handled normally, not an error |

The 403/404 split is a useful probe: ask for a user you know is *not* enrolled
in a course. A 404 means the account can see into that course, a 403 means it
cannot.

The tool checks enrollment from the user side
(`/enrollments/users/{userId}/orgUnits/`) rather than the org unit side
(`/enrollments/orgUnits/{ou}/users/{userId}`). Both need only
`enrollment:orgunit:read`, but the org unit side returns 403 outright in any
course the account cannot reach, whereas the user side returns 200 and omits
those courses — so the check degrades to a wrong answer rather than a hard
failure. Neither route escapes the access requirement; the user side just keeps
the run going.

## Commands

All commands accept `--config PATH` (default: `config.toml`).

### `demo-student auth`

Signs in to D2L and stores the OAuth2 token pair. Run this once; afterwards
the tool refreshes tokens automatically on every run.

```sh
uv run demo-student auth            # browser flow (requires mkcert)
uv run demo-student auth --manual   # headless flow: open the printed URL on
                                    # any device, then paste the redirect URL back
```

In the manual flow the browser shows a connection error after sign-in — that
is expected. Copy the full URL from the address bar (it contains the
authorization code) and paste it into the prompt.

### `demo-student whoami`

Shows which D2L account the stored tokens act as — every user and enrollment
the tool creates is attributed to this account, so check here when a run
fails with 403s or when several service accounts are in play.

```sh
uv run demo-student whoami
```

```
  Host:       yourschool.brightspace.com
  Name:       CETLOE Api
  Username:   cetloe.api
  User id:    12345
  Token file: working/token/token.json
```

Exits 2 with a message to run `demo-student auth` when no tokens are stored.

### `demo-student run`

Discovers every course offering in the organization, then checks each one
in parallel and creates/enrolls the demo student where missing.

```sh
uv run demo-student run                          # default: the whole organization
uv run demo-student run --ou-list 2469900,2469901  # specific org units only
uv run demo-student run --workers 16             # more worker threads
uv run demo-student run --dry-run                # report only; change nothing
uv run demo-student run --ou-list 2469900 --recheck  # re-verify one course
```

Exit code is 0 when every course succeeded, 1 when any course errored (errors
are stored in the database and the run continues past them).

#### What discovery covers

Discovery pages through the course-offering descendants of `orgOu`, so it
reaches every course in the organization regardless of where it hangs in the
org structure. This matters because **a course offering does not have to have
a semester parent**: building sections, sandboxes, and university-related
courses are parented to a course template instead. Walking semesters — as the
legacy tool and earlier versions of this one did — silently skips those. On the
GSU organization that is about 12,000 course offerings, roughly 6% of the
total, including current-term building sections.

The first full run therefore has a lot to do; later runs are cheap, because
completed courses are filtered out of the database before any API call is made.

`--recheck` drops the in-scope courses (the whole organization, or `--ou-list`)
back to pending before the run, so already-completed courses are verified
live against D2L again instead of being skipped. It's safe to run any time:
each check is idempotent, so a course whose demo student is still in place
is left untouched (`alreadyEnrolled`) and only a course whose demo student
went missing gets recreated. Without `--recheck`, completed courses stay
skipped until they age out of the one-year retention window (see
[Database](#database)).

### `demo-student find-orphans`

Reports demo students whose course offering no longer exists. When a
course is deleted, its `demo.{orgUnitId}` user stays behind with nothing
to belong to. Read-only: nothing in D2L or the database changes, so you
can look at the list before anything acts on it.

```sh
uv run demo-student find-orphans                       # scan the whole organization
uv run demo-student find-orphans --out orphans.csv     # also write the list to CSV
uv run demo-student find-orphans --ou-list 2469900     # scan specific org units only
```

Candidates come from diffing `completedCourses` against live discovery.
That costs one discovery pass instead of a call per course, but the diff
on its own is not evidence. Brightspace leaves org units the service
account cannot reach out of listings entirely (see
[Course Access Options](#course-access-options-easy-to-miss)), so a
permissions gap would make thousands of live courses look deleted. Every
candidate is therefore confirmed with a direct
`GET /lp/{v}/orgstructure/{ou}`, and the response decides:

| Response | Verdict |
| --- | --- |
| `404 Resource Not Found` | The course is gone. Its demo student is orphaned |
| `403 Not Authorized` | The account cannot see the course, which is not the same as the course being deleted. Reported as unconfirmable and left alone |
| Any other error | Proof of nothing. Reported as unconfirmable and left alone |
| `200` | The course is alive; discovery simply missed it. Not an orphan |

The summary counts what was left alone alongside what was found. A run
that skipped a thousand courses should not be able to pass for a clean
one.

#### Unverified: courses in the recycle bin

**Test this against your instance before relying on the cleanup.**
Deleting a course in the Brightspace UI does not remove the org unit. It
goes to a recycle bin, where it can be restored or purged for good.
D2L's API reference says `GET /lp/{v}/orgstructure/{ou}` returns 404 for
an org unit that is not found, and never says which of those two states
a recycled course counts as.

So one of two things is true, and only a test on a real tenant settles
it:

- A recycled course returns **404**, and the cleanup works as described.
- A recycled course returns **200**, in which case `find-orphans` sees it
  as alive and reports nothing until the course is purged from the
  archive.

Either way nothing gets wrongly deleted, since a course that reads as
alive is never judged orphaned. The worst case is that the command finds
nothing.

To check: delete a throwaway course that has a demo student, then run
`find-orphans`. If the demo student does not show up, the org unit
lookup is returning 200 for recycled courses, and detection would need
to consult `GET /lp/{v}/orgstructure/recyclebin/` as well.

#### The candidate cap

A scan aborts without confirming anything when the candidate set is both
larger than 25 courses and above 5% of all completed courses. Exit code
is 2, with the reason. A sudden mass orphaning is usually a broken
service account rather than real deletions, and it is cheaper to catch
that before making a few thousand confirmation calls than after.

Raise the threshold with `--max-candidate-ratio` when a genuinely large
cleanup is expected, e.g. after a semester purge:

```sh
uv run demo-student find-orphans --max-candidate-ratio 0.25   # allow up to 25%
uv run demo-student find-orphans --max-candidate-ratio 1.0    # disable the check
```

Check the access requirements above before you raise it. A permissions
gap is the failure this cap was put there for.

### `demo-student delete-orphans`

Acts on what `find-orphans` reports. It runs the same scan itself rather
than reading a saved list, so it never works from stale results, and it
**deactivates** the orphaned demo students by default.

```sh
uv run demo-student delete-orphans                        # report only; changes nothing
uv run demo-student delete-orphans --confirm              # deactivate the orphans
uv run demo-student delete-orphans --confirm --hard-delete  # permanently delete them
```

Without `--confirm` it reports and stops. Same command, same scan, so
what you see in the report is what `--confirm` will act on.

#### Why deactivation is the default

D2L restores a deleted user only through a support request, only within
30 days, and only if no account has since taken the same username. That
last condition is the problem here: this tool creates `demo.{orgUnitId}`
on every run, so a later run that recreates a wrongly deleted demo
student shuts the window for good.

Deactivation carries none of that. It blocks the account, keeps its
enrollments, and can be undone from the Brightspace UI or the API at any
point. The tool is making this call on its own, without a human looking
at each course, which is a good argument for the reversible option being
the default one. `--hard-delete` is there for when the accounts really
should be gone, and it prints the consequence before it acts.

Deactivation uses `PUT /lp/{v}/users/{userId}/activation`, which takes
only an `IsActive` flag. The general user update route would also work,
and is deliberately avoided: its request body replaces every field on
the user.

#### What happens to the database

A demo student that is successfully deactivated or deleted has its
`completedCourses` row removed, since the course that row recorded is
gone. A user that fails keeps its row and stays on the books for a
retry. Failures land in the `errors` table under the `deleteOrphan`
phase, where `status --errors` will show them. One failure never stops
the rest. The command exits 1 if any failed.

### `demo-student status`

Shows recent runs and metrics from the SQLite database.

```sh
uv run demo-student status               # recent runs
uv run demo-student status --errors      # recent errors
uv run demo-student status --limit 25    # more rows
```

### `demo-student import-legacy`

One-time migration: seeds the completed-courses table from the legacy
script's tracking file (one org unit id per line) so the first run does not
re-check thousands of courses.

```sh
uv run demo-student import-legacy /path/to/demo_783_newuser_co.txt
```

## Database

`working/db/demoStudent.db` (SQLite, WAL mode) with three tables:

- `completedCourses` — org units that have their demo student; the skip list.
- `runs` — one row per run: timestamps, status, and counts (discovered,
  skipped, checked, users created, enrollments created, errors).
- `errors` — individual failures with run id, org unit, phase, HTTP status,
  and message.

Dashboards can read this file directly; the schema is stable.

Retention: at the start of every run, rows older than one year are deleted
from all three tables. If a pruned course ever reappears in discovery it is
simply re-checked (found already enrolled) and re-recorded — nothing is
enrolled twice. To re-verify sooner than a year, use `run --recheck` instead
of waiting on retention or editing `completedCourses` by hand.

## Log files

In addition to the database, all errors, warnings, and run lifecycle events
(start, discovery counts, finish summary) are appended to monthly plain-text
log files in `working/logs/demoStudent_YYYYMM.log`. Per-course success lines
appear only on the console; the log files stay focused on errors and run
outcomes. The directory (`logDir`) and file name prefix (`logFilePrefix`)
are both configurable — see `config.example.toml`.

## Development

```sh
uv run pytest        # run the test suite
```

See `TESTPLAN.md` for the manual end-to-end checklist.

### Examples

`examples/` holds runnable scripts that drive the library against
stand-in data. They need no credentials and change nothing:

```sh
uv run python examples/findOrphanedDemoStudents.py
```

## CI/CD

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and
pull request to `master`:

- **test** — `uv sync` then `uv run pytest`.
- **deploy** (push to `master` only, after `test` passes, runs on the
  self-hosted runner) — rsyncs the checkout into
  `/apps/local/cetlocron/data/apps/demoStudent`, then runs `uv sync` there.

Notes on the deploy step:

- `rsync -av --delete` mirrors the repo into the app directory, removing
  files that no longer exist in git. The trailing slashes on `./` and
  `"$APP_DIR"/` matter — they copy contents into the folder instead of
  nesting a new folder inside it.
- `--exclude 'config.toml'` is required — without it, `--delete` would wipe
  out the live config, since it isn't part of the git checkout.
- `--exclude 'working/'` protects the SQLite database, tokens, and logs from
  being touched by a deploy.
- `uv sync` runs with `working-directory` set to `$APP_DIR`, so the virtual
  environment lives next to the deployed code, not in the runner's temporary
  checkout.

### Docker Deplayment

```sh
git clone https://github.com/GSU-CETLOE/demoStudent-Python.git
cd demoStudent-Python

docker build -t demo-student:latest .

docker run --rm \
  -v $(pwd)/config.toml:/app/config.toml:ro \
  -v $(pwd)/working:/app/working \
  demo-student:latest run
```

### One-time server setup (before the first deploy)

```sh
sudo mkdir -p /apps/local/cetlocron/data/apps/demoStudent
sudo chown -R <runner-user> /apps/local/cetlocron/data/apps/demoStudent
cd /apps/local/cetlocron/data/apps/demoStudent
cp config.example.toml config.toml   # then fill in host, clientId, clientSecret, redirectUri
```
