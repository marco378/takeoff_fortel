# Fortel AI Takeoff — deployment / operations guide

This is the ops reference for running the assessor portal (`approval_server.py`) on a
shared machine. For what the product does, see `README_PRODUCT.md`. For environment
variables, see `.env.example`.

## Quick start

```
cp .env.example .env        # fill in real values (see comments in the file)
./run.sh start               # starts the portal in the background
./run.sh status               # confirm it's up
```

## Start / stop / restart

`run.sh` is the single entry point. It checks the venv exists, checks the key
dependencies import cleanly, sources `.env` if present, and logs to a file instead of
a terminal that will eventually be closed.

| Command            | What it does                                                        |
|---------------------|----------------------------------------------------------------------|
| `./run.sh start`   | Start in the background (`nohup`), write `run/portal.pid`, log to `logs/portal.log` |
| `./run.sh stop`    | Stop the background process via its PID file (graceful, then SIGKILL after 10s) |
| `./run.sh restart` | `stop` then `start`                                                 |
| `./run.sh status`  | Show whether the PID is alive + curl the one-line health check      |
| `./run.sh fg`      | Run in the foreground (Ctrl-C to stop) — use this while debugging   |

If you'd rather have the OS supervise it (auto-restart on crash, auto-start on
login/reboot — recommended for the actual shared deployment machine, not just dev),
use the launchd unit instead of `run.sh start`/`stop`:

```
cp com.fortel.approval.plist.example ~/Library/LaunchAgents/com.fortel.approval.plist
# edit the copy: fill in the REPLACE_ME repo path and your username's log path
launchctl load ~/Library/LaunchAgents/com.fortel.approval.plist
launchctl start com.fortel.approval

# stop it:
launchctl stop com.fortel.approval
# uninstall:
launchctl unload ~/Library/LaunchAgents/com.fortel.approval.plist
```

Only run **one** instance against the live `approval_jobs.json` at a time — see
"Running a QA instance" below for a second, isolated instance.

## Health check

One-liner, works whether the server was started via `run.sh`, launchd, or by hand:

```
curl -s http://127.0.0.1:5001/status
```

`./run.sh status` runs this for you against whatever `APPROVAL_HOST`/`APPROVAL_PORT`
are set in your `.env` (defaults to `127.0.0.1:5001`). `/status` is always exempt from
the auth gate below, so this works even with `PORTAL_TOKEN` set.

## Authentication (`PORTAL_TOKEN`)

The portal ships with **no authentication by default** — every route is open. Set
`PORTAL_TOKEN` in `.env` to gate every route except `/status` and the static `/portal`
shell (an unauthenticated visitor to `/portal` gets served the page, but its own
`fetch()` calls 401, so it's blank/non-functional rather than a 404). `APPROVAL_TOKEN`
is accepted as an older alias for the same variable; `PORTAL_TOKEN` wins if both are set.

How it works once set:
- Bookmark the portal as `http://host:5001/portal?token=<PORTAL_TOKEN>`. That request
  sets an `httponly`, `SameSite=Lax` cookie (30-day max-age) and redirects to the clean
  `/portal` URL — the token doesn't linger in the address bar or browser history past
  the first visit.
- Every other route accepts the token via, in order: an `Authorization: Bearer <token>`
  header, the cookie above, or a `?token=` query parameter. The last of these is how
  emailed approve/reject/adjust links stay authorised — `approval_email.py` appends
  `?token=...` to every action link whenever `PORTAL_TOKEN`/`APPROVAL_TOKEN` is set.
- `APPROVAL_HOST` can only be set to something other than `127.0.0.1`/`localhost` if
  `PORTAL_TOKEN` is also set — otherwise the server logs a warning and falls back to
  `127.0.0.1` rather than exposing an unauthenticated portal to the LAN.
- The startup banner prints the token **masked** (first 4 characters + `…`) — it never
  writes the full token to `logs/portal.log`. Get the real value from `.env`.
- **werkzeug's access log line for every request still includes the full request path**,
  so a GET containing `?token=...` (an emailed link, or the `/portal?token=...`
  bookmark) will have that token appear in full in the access-log line for that request,
  even though the startup banner itself is masked. Treat `logs/portal.log` as sensitive
  for as long as any `?token=` request has been made against it, and rotate the token
  (generate a new one, update `.env`, restart) if that log is ever shared or exposed.

## CSRF: GET vs POST on mutating routes

`/approve/<id>`, `/reject/<id>` and `/adjust/<id>` all accept both GET and POST, but
**GET never mutates anything**:
- `GET /approve/<id>` and `GET /reject/<id>` render a small confirm page with a POST
  button — clicking an emailed action link lands here, and the actual approve/reject
  only happens when the button is clicked (a real POST). This is what stops a mutating
  action from firing on mere top-level navigation (an email client's link-preview
  prefetch, or a malicious page that merely links here, would otherwise silently
  approve/reject a job under a `SameSite=Lax` cookie).
- `GET /adjust/<id>` just redirects into `/portal?job=<id>` for manual polygon editing —
  it never mutated the job and still doesn't.
- The portal's own JS (`assessor_portal.html`) always POSTs with a JSON body and gets a
  JSON response back; the confirm page's `<form>` POSTs without a JSON content-type and
  gets the human-readable HTML result page back instead. Both paths mutate only on POST.

## Where things live

| What                          | Path                              | Notes |
|-------------------------------|------------------------------------|-------|
| Portal logs                  | `logs/portal.log`                 | Created by `run.sh`; append-only, rotate manually if it gets large (`mv logs/portal.log logs/portal.log.$(date +%F) && ./run.sh restart`). launchd's own copy goes to `~/Library/Logs/fortel-approval.log` per the plist. May contain a full `?token=` from an emailed-link request — see Authentication above. |
| Process PID (run.sh only)     | `run/portal.pid`                  | Deleted automatically on clean stop. |
| Job database (live)          | `approval_jobs.json`              | The system of record: every job's status, area, costing, flags. Gitignored — never commit it (client data). Atomic writes (`os.replace`) so a crash mid-write can't corrupt it, but see backups below. Overridable via `JOBS_FILE`. |
| Job database (archived)      | `<JOBS_FILE stem>_archive.json`   | e.g. `approval_jobs_archive.json` for the live default, `approval_jobs.qa_archive.json` for a `JOBS_FILE=approval_jobs.qa.json` QA instance. Where soft-deleted / archived jobs land (`/archive` endpoint). Never a hard delete of client data. Archived jobs stay reachable via `/job/<id>`, `/snapshot/<id>` and `/quotation/<id>.<fmt>` — only the default `/jobs` listing hides them (see `/jobs/archived`). Overridable via `JOBS_ARCHIVE_FILE`. |
| Rolling backups              | `backups/approval_jobs.YYYY-MM-DD.json` (live default) or `backups_<JOBS_FILE stem>/<stem>.YYYY-MM-DD.json` (any other `JOBS_FILE`) | Daily snapshot taken before the first save of a new day. Keeps the newest 14 by default (`BACKUP_KEEP`). A QA instance's backups never land in the live `backups/` directory — each distinct `JOBS_FILE` stem gets its own backup directory. Overridable via `BACKUP_DIR`. |
| Quotation PDFs/output        | `quotations/`                     | Generated quote documents. Gitignored. |
| Training log                 | `training_log.jsonl`              | Append-only log of assessor decisions/corrections. Gitignored. |
| Uploaded drawings             | `drawings/`                       | Client PDFs. **Never commit** — already gitignored. |
| Env config                   | `.env` (from `.env.example`)      | Never commit — gitignored below. |

All of the above except `logs/` and `run/` already exist as file paths the code writes
to directly; `logs/` and `run/` are created on demand by `run.sh`.

### `.gitignore` coverage

Client/operational data must never be committed. Confirm these lines exist in
`.gitignore` (add any that are missing):

```
approval_jobs.json
approval_jobs.json.bak*
approval_jobs.*.json
approval_jobs.*_archive.json
approval_jobs_archive.json
training_log.jsonl
quotations/
backups/
backups_*/
logs/
run/
.env
```

## Backups

The live job database is the only system of record for every decision made in the
portal. `approval_server.save_jobs()` takes a same-day snapshot into `backups/`
(or `backups_<stem>/` for a non-default `JOBS_FILE`) automatically before the first
write of each calendar day, keeping the newest 14 (`BACKUP_KEEP`). Take a manual backup
too before anything risky (upgrading, editing the file by hand, etc.):

```
cp approval_jobs.json "backups/approval_jobs.manual-$(date +%Y%m%dT%H%M%S).json"
```

## Updating

```
./run.sh stop
git pull
.venv/bin/pip install -r requirements.txt      # in case deps changed
.venv/bin/python ci_tests.py                     # MUST be 100% before restarting prod
.venv/bin/python robustness_tests.py             # 0 CRASH / 0 SILENT_NUMBER required
./run.sh start
./run.sh status
```

Never restart the live portal on a red test suite. If `ci_tests.py` or
`robustness_tests.py` fail after a pull, stay on the previous commit
(`git log` for the last known-good SHA, `git checkout <sha> -- .` is NOT recommended —
instead fix forward or ask Jas) rather than serving a broken build.

A server restart mid-job is safe by design: any job stuck on `processing` at startup
is swept and routed to the assessor as `UNMEASURED` rather than left stranded forever
— no manual recovery step needed after a restart.

## Running a QA / test instance alongside the live one

Never point a second instance at the live `approval_jobs.json` — writes from two
processes can silently clobber each other. Use a separate port and a separate jobs
file; the archive and backup directory both follow automatically from `JOBS_FILE`'s
name, so a QA instance never touches the live archive or backups:

```
APPROVAL_PORT=5097 JOBS_FILE=approval_jobs.qa.json .venv/bin/python approval_server.py
```

This gives the QA instance its own `approval_jobs.qa.json`, its own
`approval_jobs.qa_archive.json`, and its own `backups_approval_jobs.qa/` — all isolated
from the live files. Clean up QA jobs afterwards — don't leave test junk in any file the
team looks at (see `CLAUDE.md`).

## Troubleshooting

- **Portal won't start / exits immediately** — `./run.sh start` prints the last 20
  log lines automatically; also check `.venv/bin/python -c "import flask, fitz, numpy,
  shapely, cv2, PIL"` runs cleanly.
- **`ERROR: .venv/bin/python not found`** — set up the venv:
  `python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- **Portal up but `/status` times out** — check `APPROVAL_HOST`/`APPROVAL_PORT` in
  `.env` match what you're curling; check nothing else is bound to the same port
  (`lsof -i :5001`).
- **A job is stuck on "processing" forever** — restart the portal; the startup sweep
  routes any stranded job to the assessor instead of leaving it spinning. Check
  `logs/portal.log` for the watchdog's "PIPELINE TIMEOUT" flag, which fires
  automatically after `TAKEOFF_TIMEOUT_S`.
- **Emailed portal link points at the wrong host** — `APPROVAL_BASE_URL` in `.env` is
  still the default `http://localhost:5001`; set it to the shared machine's real
  address.
- **Emailed approve/reject link 401s** — `PORTAL_TOKEN`/`APPROVAL_TOKEN` was enabled
  (or rotated) after the email was sent, so the link's `?token=...` is stale/missing.
  Re-open the job from the portal instead, or wait for the next takeoff run to send a
  fresh email with the current token.
