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

D2L API calls go straight to the tenant unless `proxyUrl` is set, in which
case every one goes through apiProxy2, a separate reverse proxy that mirrors
the tenant's `/d2l/api`. The proxy runs in passthrough mode: this tool still
signs in with its own OAuth2 application and sends its own bearer token, and
the proxy relays it. Sign-in and token refresh always go straight to
`auth.brightspace.com`, which the proxy does not cover.

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
instead. It needs no local server or certificate.

### 3. Start apiProxy2 (optional)

Skip this step to have the tool call D2L directly. To route its calls through
apiProxy2 instead, set `proxyUrl` (see [Configure](#4-configure)), and the proxy
has to be running, and reachable, wherever the tool runs: your machine, the
cron server, or a container. Set it up from its own README. Started with
`uv run apiproxy`, it listens on `http://127.0.0.1:8080`.

Three of apiProxy2's own settings matter here. They go in the proxy's `.env`
file, not in this tool's `config.toml`:

- Leave `D2L_PROXY_AUTH_ENABLED` off. This tool sends its own bearer token,
  and with proxy authentication on, the proxy refuses any request without an
  `X-Proxy-Key`.
- `D2L_TENANT_HOST` must name the same tenant as `host` in `config.toml`.
- If `D2L_RATE_LIMIT_ENABLED` is on, the budget applies to this tool too. A
  full run makes thousands of calls, and at the default of 60 a minute it
  crawls. The tool waits out each 429 using `Retry-After`, up to five times
  per request, and then treats the call as failed.

The tool asks the proxy not to cache anything, by sending
`Cache-Control: no-cache` on every request. It decides what to create,
enroll, deactivate, and delete from what it reads, and a cached read could be
minutes old. In the proxy's log, every request from this tool shows
`"cache":"BYPASS"`.

### 4. Configure

```sh
cp config.example.toml config.toml
```

Fill in `host`, `clientId`, `clientSecret`, and `redirectUri` from your
registered OAuth2 application (D2L Admin Tools > Manage Extensibility >
OAuth 2.0). The registered redirect URI must match `redirectUri` exactly
(e.g. `https://localhost:8443/callback`). The client secret can also be
provided via the `DEMO_STUDENT_CLIENT_SECRET` environment variable instead of
the file. All other settings have defaults; see `config.example.toml`.

`proxyUrl` picks the route to D2L. Leave it out and the tool calls
`https://{host}/d2l/api` directly. Set it to the proxy's base URL, such as
`http://127.0.0.1:8080`, and every API call goes through apiProxy2 instead.
`demo-student whoami` shows which one is in use. Use `https://` for a proxy on
another machine, since every request carries the tool's bearer token. Give the
proxy's root, not `/d2l/api`: the proxy maps its root to `/d2l/api` itself.
With the wrong base no path reaches a real D2L route, and commands stop with
`Could not negotiate API versions`.

Runtime state lives in the `working/` directory (configurable via
`workingDir`; gitignored):

```
working/
  db/demoStudent.db      SQLite database (completed courses, runs, errors)
  token/token.json       OAuth2 token store
  token/token.json.lock  guards token refresh between concurrent processes
  logs/                  monthly log files (dir name via `logDir`)
  datasets/              downloaded Brightspace Data Sets extracts
  localhost.pem(-key)    mkcert certificate for the browser auth flow
```

Two runs sharing a `workingDir` are safe. D2L rotates refresh tokens, and using
one invalidates it. Without coordination, a second process refreshing would
kill the first process's token mid-run, and every remaining course would fail
with `invalid_grant`. Refreshes therefore take an inter-process lock, and a
process that finds its own refresh token already spent adopts the pair the
other one wrote instead of failing. The lock is released by the OS if a
process dies, so it cannot go stale.

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
| `GET /d2l/api/versions/` | *(none; version negotiation is unauthenticated)* |
| `GET /lp/{v}/users/whoami` | `users:own_profile:read` |
| `GET /lp/{v}/orgstructure/{ou}` | `organizations:organization:read` |
| `GET /lp/{v}/orgstructure/{ou}/descendants/paged/` | `organizations:organization:read` |
| `GET /lp/{v}/users/?userName=` | `users:userdata:read` |
| `GET /lp/{v}/users/` (paged, all users) | `users:userdata:read` |
| `GET /lp/{v}/enrollments/users/{userId}/orgUnits/` | `enrollment:orgunit:read` |
| `POST /lp/{v}/users/` | `users:userdata:create` |
| `POST /lp/{v}/enrollments/` | `enrollment:orgunit:create` |
| `PUT /lp/{v}/users/{userId}/activation` | `users:activation:update` |
| `DELETE /lp/{v}/users/{userId}` | `users:userdata:delete` |
| `GET /lp/{v}/datasets/bds` and its extract routes | `datasets:bds:read` |

The delete and activation scopes are used only by `delete-orphans`, and
`datasets:bds:read` only by the orphan commands' Data Sets path. Leave
any of them off and the rest of the tool works unchanged; without the
Data Sets scope the orphan commands fall back to the live user API.

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
the same value. A requested scope that isn't registered fails authorization.
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
| Users | **Enroll '{demo student role}'** | Course Offering | `POST /lp/{v}/enrollments/`, to enroll the demo student |
| Users | **Search for '{demo student role}'** | Organization, Course Offering | `GET /lp/{v}/users/?userName=`, to find `demo.{orgUnitId}` |
| User Information Privacy | **See Usernames** | Organization | `GET /lp/{v}/users/`, without which the orphan scan matches nothing and reports a clean empty result |
| Users | **View User Enrollments** | Organization, Semester, Course Template, Course Offering, Department | `GET /lp/{v}/enrollments/users/{userId}/orgUnits/`, the enrollment check |
| Users | **Create a User in the Database** | Organization | `POST /lp/{v}/users/`, to create the demo student |
| Users | View the Users' Email Address, View Users' Contact Information | Organization | the `ExternalEmail` set on creation |
| User Information Privacy | **every row the tool offers** | Organization | `POST /lp/{v}/users/`, since D2L only lets a role create a user if it can see every user field |
| Classlist | **Has Access to the Classlist** | Course Offering | enrollment reads and writes inside a course |
| Classlist | **Change Enrollment** | Course Offering | Brightspace's usual pairing for enrollment changes |
| Users | **Can Activate or Deactivate Users** | Organization | `PUT /lp/{v}/users/{userId}/activation`, for `delete-orphans` |
| Users | **Delete a User** | Organization | `DELETE /lp/{v}/users/{userId}`, for `delete-orphans --hard-delete` |
| Users | **See the User Management tool** | Organization | required alongside both of the above |
| Users | **Update a User** | Organization | required alongside `Can Activate or Deactivate Users` |
| Brightspace Data Sets | **Can Access Brightspace Data Sets** | Organization | the Users extract; without it the orphan commands fall back to the live API |
| Custom Reporting Framework | **Can see data hub** | Organization | required alongside the above |

The last four are needed only for `delete-orphans`, and they have a
dependency that user creation does not. `Create a User in the Database`
stands alone, but `Delete a User` and `Can Activate or Deactivate Users`
both also require `See the User Management tool`, and deactivation wants
`Update a User` on top of that.

`Enroll '...'` and `Search for '...'` are **per-target-role** permissions.
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

Check the last two first. At GSU they were the only two left unchecked.
Granting just the fields this tool sends is not enough, because the check is
on the role rather than on the request. `See External ID Values` is
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
Without them the service account cannot see whole categories of course. The
one that matters most is courses in an upcoming semester, which have not
started yet and so count as *future* courses. Seeding demo students into
upcoming semesters is the tool's main job, so these are the courses it most
needs to reach.

The symptom is indistinguishable from a missing permission: discovery finds the
courses (org structure is readable regardless), then every call inside them
fails with `403 Not Authorized`. Every permission in the table above can be
correctly enabled and the tool will still fail on every affected course.

Worse, the failure is not always loud. The enrollment check reads from the user
side, and Brightspace **silently omits inaccessible courses from that listing
instead of returning an error**. An existing enrollment in an unreachable
course reads back as "not enrolled", and the tool tries to enroll a user who is
already enrolled. What surfaces is a 403 on the write, which points at the
wrong thing.

To check whether the account can reach a course, request its classlist.
`200` means it has access, and `403` means it does not:

```
GET /d2l/api/le/{leVersion}/{orgUnitId}/classlist/
```

The account's own enrollment listing is filtered the same way, so it
under-reports if you use it to audit reach:

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
| `{"Errors":[{"Message":"Not Authorized"}]}` | Role permission **or** Course Access Options: the account has no authority in that org unit |
| `Insufficient scope to call API. Required: <scope>` | OAuth2 scope. D2L names the missing scope; add it and re-run `demo-student auth` |
| `404 {"Errors":[{"Message":"Resource Not Found"}]}` | The account *does* have access; the user simply is not enrolled there. Handled normally, not an error |

The 403/404 split is a useful probe: ask for a user you know is *not* enrolled
in a course. A 404 means the account can see into that course, a 403 means it
cannot.

The tool checks enrollment from the user side
(`/enrollments/users/{userId}/orgUnits/`) rather than the org unit side
(`/enrollments/orgUnits/{ou}/users/{userId}`). Both need only
`enrollment:orgunit:read`, but the org unit side returns 403 outright in any
course the account cannot reach, whereas the user side returns 200 and omits
those courses. The check degrades to a wrong answer instead of a hard failure.
Neither route escapes the access requirement; the user side just keeps the run
going.

## Commands

All commands accept `--config PATH` (default: `config.toml`).

Every command except `status` and `import-legacy` calls D2L. With `proxyUrl`
set, apiProxy2 must be running for them, `auth` included, since it checks the
new token with `whoami`. When the proxy is down, a command exits 1 with
`Could not reach the API proxy at http://127.0.0.1:8080. Is apiProxy2 running?`
Calling D2L directly, the same failure reads
`Could not reach D2L at https://yourschool.brightspace.com/d2l/api.`

### `demo-student auth`

Signs in to D2L and stores the OAuth2 token pair. Run this once; afterwards
the tool refreshes tokens automatically on every run.

```sh
uv run demo-student auth            # browser flow (requires mkcert)
uv run demo-student auth --manual   # headless flow: open the printed URL on
                                    # any device, then paste the redirect URL back
```

In the manual flow the browser shows a connection error after sign-in. That
is expected. Copy the full URL from the address bar (it contains the
authorization code) and paste it into the prompt.

### `demo-student whoami`

Shows which D2L account the stored tokens act as. Every user and enrollment
the tool creates is attributed to this account, so check here when a run
fails with 403s or when several service accounts are in play.

```sh
uv run demo-student whoami
```

```
  Host:       yourschool.brightspace.com
  Proxy:      http://127.0.0.1:8080
  Name:       CETLOE Api
  Username:   cetloe.api
  User id:    12345
  Token file: working/token/token.json
```

Without `proxyUrl`, the Proxy line reads `none, calling D2L directly`.

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
courses are parented to a course template instead. Walking semesters, as the
legacy tool and earlier versions of this one did, silently skips those. On the
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

Reports demo students whose course offering no longer exists. Delete a
course and its `demo.{orgUnitId}` user stays behind, belonging to
nothing. Read-only against D2L.

```sh
uv run demo-student find-orphans                    # report from the cached inventory
uv run demo-student find-orphans --rescan           # rebuild the inventory first
uv run demo-student find-orphans --rescan --api-walk  # read live instead of the extract
uv run demo-student find-orphans --out orphans.csv  # also write the list to CSV
```

#### How it finds them

Two passes. The first reads every user out of D2L and records the demo
students into a local `demoStudents` table. The second looks up each
distinct course once and marks it live, deleted, or unconfirmable.

#### Where the user list comes from

Two sources, and the command picks the better one on its own.

**Brightspace Data Sets**, when the tenant has it. D2L publishes a Users
data set as a zipped CSV, downloaded to `working/datasets/` and read
locally. It carries `OrgRoleId`, so an account has to hold the demo
student role *and* match the username pattern before the tool will touch
it.

**The live user API**, when Data Sets is unavailable. This pages through
every user in the tenant, which is slower, and its listing carries no
role at all, so accounts are matched on username alone.

D2L regenerates full extracts weekly by default, so the Data Sets path
can be up to a week behind. That is fine here, since an orphaned demo
student has usually been orphaned for months, and the run prints the
extract's generation date so you can see how old it is. Use `--api-walk`
when you need the live answer.

One consequence of the weekly cycle: a demo student created since the
last extract is missing from it, so a course deleted within days of
being set up may not surface until the next extract. Neither source can
wrongly delete anything, because the course lookup that decides is
always live.

The walk is the expensive half, so it runs only when the inventory is
empty or when `--rescan` asks for it. Course states get re-confirmed
every time, since a course can be deleted long after its demo student
was first seen.

Both passes print progress as they go, so a long run shows movement
rather than sitting silent:

```
2026-09-22 18:45:12  No demo student inventory yet, so this run has to build one.
2026-09-22 18:45:12  Reading every user from yourschool.brightspace.com.
2026-09-22 18:45:31    walked 500 users, 125 demo students so far
2026-09-22 18:45:49    walked 1000 users, 250 demo students so far
2026-09-22 18:52:06  Checking 750 courses, one lookup each.
2026-09-22 18:53:20    checked 100 of 750 courses, 2 deleted so far
```

Every line carries the time it happened, in the format the log files
use, so a console session and `working/logs/demoStudent_YYYYMM.log` can
be read side by side. Summaries and tables print as one block under a
stamped header, so their columns stay aligned.

Why read the accounts from D2L instead of from `completedCourses`? That
table is a skip list and gets pruned after a year. A course deleted
after its row ages out leaves nothing behind to notice, and the oldest
demo students are the ones most likely to have lost their course.
Reading from D2L sidesteps that, because a demo student's username
carries its own org unit id.

The verdict comes from the course lookup:

| Response | Verdict |
| --- | --- |
| `404 Resource Not Found` | The course is gone. Its demo student is orphaned |
| `403 Not Authorized` | The account cannot see the course, which is not the same as the course being deleted. Left alone |
| Any other error | Proof of nothing. Left alone |
| `200` | The course is alive. Not an orphan |

Only a 404 counts. Everything else leaves the course `unknown`, and
cleanup skips those.

#### Unverified: courses in the recycle bin

**This tool never deletes a course.** It reads org units and nothing
more. The only things it ever writes to D2L are demo student accounts:
creating them, enrolling them, deactivating them, and deleting them
under `--hard-delete`. Courses are deleted by people, in the Brightspace
UI, and this tool only notices afterwards.

**Test the following against your instance before relying on the
cleanup.** When someone deletes a course in the Brightspace UI, the org
unit is not removed. It goes to a recycle bin, where it can be restored
or purged for good.
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

To check, you delete a course by hand: pick a throwaway course that has
a demo student, delete it yourself in the Brightspace UI, then run
`find-orphans --rescan`. If the demo student does not show up, the org
unit lookup is returning 200 for recycled courses, and detection would
need to consult `GET /lp/{v}/orgstructure/recyclebin/` as well.

#### The scan refuses to run blind

D2L filters its user listing by what the calling role may see. It
withholds individual fields the same way, and says nothing about either
in the response. A service account that cannot read usernames matches
zero demo students and gets back an empty result that looks exactly like
a tenant with nothing to clean up.

So the scan stops with exit code 2 on the first user that arrives
without a username, rather than walking a few hundred thousand records
to reach a wrong answer. Grant **See Usernames** under User Information
Privacy, then re-run.

That guard catches the total blackout, not a partial one. Compare the
demo student count against what you expect for the tenant; if it comes
in well under, the service account is seeing only part of the tenant.

### `demo-student delete-orphans`

Runs the same two passes as `find-orphans`, then acts on the result.
**Deactivates** by default.

```sh
uv run demo-student delete-orphans                          # report only; changes nothing
uv run demo-student delete-orphans --confirm                # deactivate the orphans
uv run demo-student delete-orphans --confirm --hard-delete  # permanently delete them
```

Without `--confirm` it reports and stops. Same command, same passes, so
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

#### What happens to the inventory

A deactivated demo student is marked inactive and stays in the table, so
later runs stop offering it. A hard-deleted one is dropped, since the
account no longer exists to track.

A user that fails keeps both its row and its course state, which puts it
back in front of you on the next run. Failures land in the `errors`
table under the `deleteOrphan` phase, and `status --errors` shows them.
One failure never stops the rest. Exit code is 1 if any failed.

#### A caveat on matching

On the Data Sets path an account must hold the configured `roleId` as
well as match the username pattern, so something named `demo.12345` by
hand is reported and left alone.

The live API path cannot make that check, because D2L's user listing
carries no role field, so `--api-walk` matches on username alone. Either
way, accounts matching the prefix with no usable org unit id,
`demo.legacy` and the like, are counted and reported rather than acted
on.

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

`working/db/demoStudent.db` (SQLite, WAL mode) with four tables:

- `completedCourses`: org units that have their demo student. This is the
  skip list.
- `demoStudents`: an inventory of the demo student accounts that exist in
  D2L, each with the org unit its username names and the verdict from the
  last course lookup (`live`, `deleted`, or `unknown`).
- `runs`: one row per run, with timestamps, status, and counts (discovered,
  skipped, checked, users created, enrollments created, errors).
- `errors`: individual failures with run id, org unit, phase, HTTP status,
  and message.

Dashboards can read this file directly; the schema is stable.

Retention: at the start of every run, rows older than one year are deleted
from `completedCourses`, `runs`, and `errors`. A pruned course that turns up
in discovery again is re-checked, found already enrolled, and recorded again,
so nothing is enrolled twice. To re-verify sooner than a year, use
`run --recheck` instead of waiting on retention or editing `completedCourses`
by hand.

**`demoStudents` is exempt.** Those rows track accounts that exist in D2L,
and the oldest of them are the ones most likely to have lost their course.
Aging them out would hide the orphans the cleanup exists to find.

## Log files

In addition to the database, all errors, warnings, and run lifecycle events
(start, discovery counts, finish summary) are appended to monthly plain-text
log files in `working/logs/demoStudent_YYYYMM.log`. Per-course success lines
appear only on the console. The directory (`logDir`) and file name prefix
(`logFilePrefix`) are both configurable; see `config.example.toml`.

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

- `test` runs `uv sync`, then `uv run pytest`.
- `deploy` runs on a push to `master` only, after `test` passes, on the
  self-hosted runner. It rsyncs the checkout into
  `/apps/local/cetlocron/data/apps/demoStudent`, then runs `uv sync` there.

Notes on the deploy step:

- `rsync -av --delete` mirrors the repo into the app directory, removing
  files that no longer exist in git. The trailing slashes on `./` and
  `"$APP_DIR"/` matter: they copy the contents into the folder instead of
  nesting a new folder inside it.
- `--exclude 'config.toml'` is required. Without it, `--delete` would wipe
  out the live config, which isn't part of the git checkout.
- `--exclude 'working/'` protects the SQLite database, tokens, and logs from
  being touched by a deploy.
- `uv sync` runs with `working-directory` set to `$APP_DIR`, so the virtual
  environment lives next to the deployed code, not in the runner's temporary
  checkout.

### Docker deployment

```sh
git clone https://github.com/GSU-CETLOE/demoStudent-Python.git
cd demoStudent-Python

docker build -t demo-student:latest .

docker run --rm \
  -v $(pwd)/config.toml:/app/config.toml:ro \
  -v $(pwd)/working:/app/working \
  demo-student:latest run
```

With `proxyUrl` set, `127.0.0.1` inside the container is the container
itself, not the host, so `http://127.0.0.1:8080` does not reach a proxy
running on the host. On Linux, `--network host` shares the host's network,
and that URL then works.

### One-time server setup (before the first deploy)

```sh
sudo mkdir -p /apps/local/cetlocron/data/apps/demoStudent
sudo chown -R <runner-user> /apps/local/cetlocron/data/apps/demoStudent
cd /apps/local/cetlocron/data/apps/demoStudent
cp config.example.toml config.toml   # then fill in host, clientId, clientSecret, redirectUri
```

If the config sets `proxyUrl`, the server also needs a running apiProxy2 that
the cron job can reach. See [Start apiProxy2](#3-start-apiproxy2-optional).
