# demoStudent

Adds a unique **Demo Student** user to every D2L Brightspace course offering.
Each course gets its own user named `demo.{orgUnitId}`, created with the Demo
Student role and enrolled in that course. Courses that already have their demo
student are recorded in a local SQLite database and skipped on future runs,
unless re-checked with `--recheck` (e.g. to confirm a demo student wasn't
removed from a current-semester course). Errors and run metrics are stored in
the same database so a future dashboard can report on them.

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
  db/demoStudent.db     SQLite database (completed courses, runs, errors)
  token/token.json      OAuth2 token store
  logs/                 monthly log files (dir name via `logDir`)
  localhost.pem(-key)   mkcert certificate for the browser auth flow
```

## Brightspace access requirements

Two independent things must both be granted, and they fail in different ways:
**OAuth2 scopes** decide which routes the application may call at all, while
the **signed-in account's role** decides what it may do in a given org unit.
A correct scope with an inadequate role still returns 403 on every course.

### OAuth2 scopes

`scope` defaults to `core:*:*`, D2L's catch-all. The tool only ever calls these
nine routes, so the OAuth2 application can be registered with just the scopes
below:

| Route | Scope |
| --- | --- |
| `GET /d2l/api/versions/` | *(none — version negotiation is unauthenticated)* |
| `GET /lp/{v}/users/whoami` | `users:own_profile:read` |
| `GET /lp/{v}/orgstructure/{ou}` | `organizations:organization:read` |
| `GET /lp/{v}/orgstructure/{ou}/children/` | `organizations:organization:read` |
| `GET /lp/{v}/orgstructure/{ou}/descendants/paged/` | `organizations:organization:read` |
| `GET /lp/{v}/users/?userName=` | `users:userdata:read` |
| `GET /lp/{v}/enrollments/users/{userId}/orgUnits/` | `enrollment:orgunit:read` |
| `POST /lp/{v}/users/` | `users:userdata:create` |
| `POST /lp/{v}/enrollments/` | `enrollment:orgunit:create` |

As one scope string (spaces separate scopes, commas separate permissions on the
same resource):

```
enrollment:orgunit:create,read organizations:organization:read users:own_profile:read users:userdata:create,read
```

Register that on the OAuth2 application, then set `scope` in `config.toml` to
the same value — a requested scope that isn't registered fails authorization.
Scopes are fixed at authorization time and **do not change on refresh**: after
editing `scope`, re-run `demo-student auth` or the stored token keeps the old
set.

### Brightspace role and permissions

Two different roles are involved. Only the first one should ever cascade:

| Role | Who holds it | Requirement |
| --- | --- | --- |
| The service account's role | The account signed in via `demo-student auth` | Needs the permissions below **in every course offering** the tool touches |
| The demo student role (`roleId`, default 783) | The `demo.{orgUnitId}` users the tool creates | Just needs to be assignable in course offerings. **Must not cascade** — see below |

The service account's role must permit it to:

- read the org structure (children and descendants) from `orgOu` downward,
  to discover course offerings;
- look up users by username, and create users, at the organization level;
- read a user's enrollments;
- **enroll users into each course offering.**

Configure these under **Admin Tools > Roles and Permissions**. The exact
permission names vary by Brightspace version, so locate them by tool (Users,
Enrollment, Classlist) rather than by copying labels from here.

#### Access must reach every course offering

This is the failure mode worth understanding, because it does not look like a
permission problem in the logs — it looks like the tool is broken.

Permissions in Brightspace are granted per role **per org unit type**. A role
can be enrolled at the Organization or Semester level and still have no
authority in the course offerings beneath it, because its permissions were
never enabled for the Course Offering type. When that happens the service
account is denied *everything* in those courses — reading the classlist,
reading an enrollment, and creating one — while working normally in courses it
does have access to. Discovery still succeeds, so the run proceeds and then
fails per-course with `403 Not Authorized`.

Either grant the role genuine cascading authority over Course Offering org
units, or enrol the service account directly in every course. Enrolling
course-by-course works but silently leaves gaps: any course added later is
inaccessible until someone remembers to enroll the account in it too.

To check whether the account can reach a specific course, request its
classlist — `200` means it has access there, `403` means it does not:

```
GET /d2l/api/le/{leVersion}/{orgUnitId}/classlist/
```

To list every course the account can currently reach, page through its own
enrollments (`{callerUserId}` comes from `whoami`):

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
| `{"Errors":[{"Message":"Not Authorized"}]}` | Role permission — the account lacks authority in that org unit |
| `Insufficient scope to call API. Required: <scope>` | OAuth2 scope — D2L names the missing scope; add it and re-run `demo-student auth` |
| `404 {"Errors":[{"Message":"Resource Not Found"}]}` | The account *does* have access; the user simply is not enrolled there. Handled normally, not an error |

Note that the tool checks enrollment from the user side
(`/enrollments/users/{userId}/orgUnits/`) rather than the org unit side
(`/enrollments/orgUnits/{ou}/users/{userId}`). Both need only
`enrollment:orgunit:read`, but the org unit side additionally requires
authority inside that course offering, so it returns 403 in exactly the courses
where the role has not been granted. Reading from the user side means the tool
needs course-level authority only to *enroll*, not to check.

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

### `demo-student run`

Discovers course offerings in the most recent semesters, then checks each one
in parallel and creates/enrolls the demo student where missing.

```sh
uv run demo-student run                          # default: 2 most recent semesters
uv run demo-student run --semesters 3            # 3 most recent semesters
uv run demo-student run --ou-list 2469900,2469901  # specific org units only
uv run demo-student run --workers 16             # more worker threads
uv run demo-student run --dry-run                # report only; change nothing
uv run demo-student run --semesters 1 --recheck  # re-verify current semester
uv run demo-student run --ou-list 2469900 --recheck  # re-verify one course
```

Exit code is 0 when every course succeeded, 1 when any course errored (errors
are stored in the database and the run continues past them).

`--recheck` drops the in-scope courses (per `--semesters` or `--ou-list`)
back to pending before the run, so already-completed courses are verified
live against D2L again instead of being skipped. It's safe to run any time:
each check is idempotent, so a course whose demo student is still in place
is left untouched (`alreadyEnrolled`) and only a course whose demo student
went missing gets recreated. Without `--recheck`, completed courses stay
skipped until they age out of the one-year retention window (see
[Database](#database)).

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
