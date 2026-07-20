# demoStudent

Adds a unique **Demo Student** user to every D2L Brightspace course offering.
Each course gets its own user named `demo.{orgUnitId}`, created with the Demo
Student role and enrolled in that course. Courses that already have their demo
student are recorded in a local SQLite database and never checked again.
Errors and run metrics are stored in the same database so a future dashboard
can report on them.

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
```

Exit code is 0 when every course succeeded, 1 when any course errored (errors
are stored in the database and the run continues past them).

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
enrolled twice.

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

CI-CD GitHub

# YAML:

name: Test and Deploy (demoStudent)

on:
push:
branches: [master]
pull_request:
branches: [master]

jobs:
test:
runs-on: ubuntu-latest
steps: - name: Checkout code
uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Install dependencies
        run: uv sync

      - name: Run tests
        run: uv run pytest

deploy:
needs: test
runs-on: self-hosted
if: github.ref == 'refs/heads/master'
env:
APP_DIR: /apps/local/cetlocron/data/apps/demoStudent
steps: - name: Checkout code
uses: actions/checkout@v4

      - name: Sync code to app directory
        run: |
          mkdir -p "$APP_DIR"
          rsync -av --delete \
            --exclude 'config.toml' \
            --exclude 'working/' \
            ./ "$APP_DIR"/

      - name: Install uv
        uses: astral-sh/setup-uv@v3

      - name: Sync dependencies in app directory
        working-directory: /apps/local/cetlocron/data/apps/demoStudent
        run: uv sync

      - name: Verify config exists
        run: |
          if [ ! -f "$APP_DIR/config.toml" ]; then
            echo "ERROR: config.toml not found in $APP_DIR"
            echo "Create it once manually: cp config.example.toml config.toml, then fill it in."
            exit 1
          fi

=================
Why each piece is there
rsync -av --delete — mirrors the checked-out code into /apps/local/cetlocron/data/apps/demoStudent, removing files that no longer exist in the repo (so old/renamed files don't linger). The trailing slashes on ./ and "$APP_DIR"/ matter — they tell rsync to copy contents into the folder, not create a nested folder inside it.
--exclude 'config.toml' — critical. Without this, rsync's --delete would wipe out your live config since it doesn't exist in the git checkout. This exclude keeps your real credentials untouched on every deploy.
--exclude 'working/' — the repo's README mentions working/ holds the SQLite database, tokens, and logs — none of that should ever be touched by a code deploy either.
uv sync runs in $APP_DIR — via working-directory, so the actual virtual environment/dependencies live alongside the deployed code, not in the runner's temporary \_work checkout.

One-time setup you'll need to do on the server, before the first deploy runs

sudo mkdir -p /apps/local/cetlocron/data/apps/demoStudent
sudo chown -R <runner-user> /apps/local/cetlocron/data/apps/demoStudent
cd /apps/local/cetlocron/data/apps/demoStudent
cp config.example.toml config.toml # then fill in host, clientId, clientSecret, redirectUri
