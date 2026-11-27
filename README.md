# Group Planner

## Required security checks

Activate the project virtual environment, install `requirements-security.txt`, run
`python -m playwright install chromium`, and start Docker. Install the local hooks
with `python scripts/security_install_hooks.py`. The hooks use the installing
Python interpreter, including from Git interfaces. Security tests keep
authentication and CSRF enabled; older functional fixtures explicitly disable
only the protections they do not exercise.

During development run `python scripts/security_check.py audit --worktree`.
Commit hooks check the staged snapshot; push and CI gates check the committed
snapshot. Missing scanners, failed tests, and incomplete scans fail the gate.
High/Critical candidates, credential exposure, account takeover, and unauthorized
cross-user access block progression. Other findings appear before commits/pushes.
The private register and reports live outside Git and sync folders at
`%LOCALAPPDATA%/GroupPlanner/Security/`. Never attach these reports to public issues,
pull requests, or workflow artifacts. Generate the private ASVS worksheet and route
matrix with `python scripts/security_report.py`.
Manual observations are read from the private `asvs-evidence.json` file and are
bound to the reviewed branch commits. Missing or stale evidence stays unverified.

The Security workflow runs on all pushes, pull requests to `main`,
daily advisory checks, and a weekly full scan. Administrators must apply and verify
`.security/branch-protection.json` on `main`; `security_admin.py --apply`
uses an administrator `GITHUB_TOKEN`. Required check: `security (current)`.
The check must come from the official GitHub Actions app, and the latest push
requires a fresh approval. The helper reads settings back before reporting
success. `--apply` protects unprotected branches; existing rules are preserved
and missing safeguards are reported for an administrator to strengthen.

The private checklist generator verifies the reviewed ASVS 5.0.0 CSV checksum
before writing reports. Updating the standard requires an explicit source review
and checksum update; redirects or changed content do not overwrite evidence.

The checked production workflow requires a production environment containing
`VERCEL_TOKEN`, `VERCEL_ORG_ID`, and `VERCEL_PROJECT_ID`, plus a `PRODUCTION_URL`
environment variable in GitHub's **Variables** settings. It deploys only the exact
main commit whose Security workflow succeeded. Git-triggered main deployments are
disabled in `vercel.json`; configure the release credentials before merging this
configuration. Preview deployments must use separate credentials and databases.

The release job uses `scripts/security_release.py` and the official HTTPS API.
It verifies the configured project owner, connected GitHub repository, `main`
production branch, checked commit, and assigned production hostname. It uses the
pinned HTTP dependencies in `requirements-release.txt`; all root Python dependency
manifests are included in security scans. It does not install a deployment CLI.
Verify this flow first with an isolated Vercel project and its test hostname.
Project Git integration, permissions, and actual release behavior remain unverified
until that hosted check is completed.

Production also requires a random signing key of at least 32 characters, verified
MongoDB TLS, HTTPS base/callback URLs on the same hostname, secure session cookies,
and Resend delivery. Hosted settings, real Google consent, and HTTP/2 edge protection
remain unverified until checked in the provider consoles. Never run active scanners
or resource-exhaustion tests against production.

Set a stable local `SECRET_KEY` when keeping calendar connections between restarts.
An unconfigured development instance generates a new random key on each start.
The same stable key protects enrolled authenticator seeds; back it up securely and
plan recovery before rotating it. Recovery codes remain usable if seed decryption
is unavailable.

Users can enable optional authenticator verification from **Account**. Once on,
both password and Google sign-in require an authenticator code or one of ten
single-use recovery codes. Save the recovery codes when shown; they cannot be
retrieved later. Password resets preserve this protection. Turning verification
off or replacing recovery codes requires existing identity proofs. Optional
enrollment leaves any assurance requirement for mandatory MFA unresolved.

Sign-in setting changes create generic in-app alerts and email the previously
verified contact. Configure the separate trusted retry job as described in
`.security/policy.md`; it stays disabled until its environment and restricted
credentials are ready. Hosted delivery and retry activation need direct testing.

Group Planner is the CS4800 Software Engineering with AI MVP for coordinating recurring availability across small groups. Members save weekly availability, groups compare those windows, and plans can find and schedule matching meeting times.

Live application: **replace this text with the deployed Vercel URL** (for example, `https://your-project.vercel.app`). The production URL is intentionally not guessed or committed as a secret.

## Public Google integration setup

The app has separate Google flows. Sign-in requests `openid email profile`; Calendar connection is optional after account creation and requests `https://www.googleapis.com/auth/calendar.events.freebusy` plus `https://www.googleapis.com/auth/calendar.calendarlist.readonly` to let each user select calendars. A new Google user chooses a Group Planner username, then sees a Calendar connection prompt that can be skipped. The Google name and verified email are saved with the account.

In Google Cloud Console, enable Google Calendar API, configure OAuth consent with **External** audience, and create a Web application OAuth client. Set the app name to **Group Planner**, add the app's logo, a monitored support email, and developer contact email. Use the public Vercel homepage as the application home page and `https://<production-host>/privacy` as the privacy policy URL. Declare only the scopes above. Add the exact local and production redirect URIs for both `/auth/google/callback` and `/auth/google-calendar/callback` to the client. Publish the app when ready for external sign-in. Google may restrict unverified Calendar consent; an owned, verified domain and Google's scope review are needed before promising unrestricted public Calendar access.

Set these Vercel production environment variables using the actual deployment host, and redeploy after changing them:

```env
GOOGLE_CLIENT_ID=<web OAuth client ID>
GOOGLE_CLIENT_SECRET=<web OAuth client secret>
GOOGLE_REDIRECT_URI=https://<production-host>/auth/google/callback
GOOGLE_CALENDAR_REDIRECT_URI=https://<production-host>/auth/google-calendar/callback
APP_BASE_URL=https://<production-host>
SESSION_COOKIE_SECURE=true
SUPPORT_EMAIL=<the support email on the Google consent screen>
```

Keep the localhost redirect values in `.env` for local development. The Vercel host and OAuth client credentials are account-specific and cannot be safely filled in from this repository. The production homepage and privacy policy must be publicly reachable before entering them in Google Cloud.

## Problem

Finding a meeting time for a group is often an awkward exchange of messages. People may have different weekly schedules, and a simple list of individual availability does not make the overlap obvious. Group Planner keeps reusable member records, recurring availability, and group plans in one place so a group can identify realistic meeting windows quickly.

## Current MVP features

- Create private accounts with a username and securely hashed password.
- Sign in with Google using OpenID Connect identity scopes (`openid email profile`) without requesting Google Calendar access.
- Connect Google Calendar separately, with free/busy and read-only calendar-list permissions so users choose which calendars affect availability; Google sign-in alone never grants calendar access.
- Connect multiple named HTTPS iCalendar/iCal feeds per account alongside Google Calendar. Re-adding a subscription URL updates that feed. Calendar URLs and OAuth tokens are encrypted before storage, feeds are fetched server-side with redirect/size/private-network protections, and only normalized busy intervals are used for scheduling.
- Keep dashboards, groups, members, availability, and plans scoped to the authenticated account.
- Support an explicit, idempotent migration that links legacy `guest#` member profiles to authenticated accounts while preserving schedules, memberships, plans, and RSVPs.
- Create, edit, view, and delete groups with optional descriptions.
- Manage reusable members from each group; there is no standalone Members navigation page.
- Assign group Owner, Admin, or Member roles. Owners retain full control; Admin permissions can independently manage members, edit the group, edit availability, edit RSVP responses, and manage plans.
- Save multiple recurring weekly availability windows per member and day.
- Use a unified typed/time-picker editor with five-minute availability precision.
- Edit or remove availability individually or in a batch.
- View a group's availability in a member-by-day matrix.
- Create plans with a date range, meeting duration, allowed time range, and minimum attendance.
- Find matching times through a separate scheduling service. Candidates are searched in 30-minute increments, ranked by attendance, and shown with member availability details.
- Support arbitrary positive whole-minute meeting durations and clearly label shorter alternatives when no full-duration match exists.
- Schedule a selected result and keep the confirmed time in MongoDB.
- Invite group members to scheduled plans and record attending/not-attending RSVP responses, including optional late-arrival and early-departure times.
- Let each member update their own RSVP while limiting edits to another member's RSVP to authorized group roles.
- Show planning and upcoming scheduled plans separately; the dashboard shows only scheduled plans that have not ended.
- Provide validation, flash messages, and 404/500/503 error pages.
- Run automated tests locally and in GitHub Actions.

## Planned features

These are intentionally outside the current MVP:

- Plan voting or preference collection.
- Apple or Outlook calendar integration.
- Notifications, chat, maps, mobile clients, and fairness scoring.

## Technology stack

- Python 3.11+ and Flask 3.1.
- Flask-Login for authenticated sessions, Authlib for Google OpenID Connect and optional Calendar consent, and Fernet encryption for saved calendar secrets.
- Jinja2 templates with HTML, CSS, and vanilla JavaScript.
- MongoDB Atlas with PyMongo.
- `mongomock` and `pytest` for isolated tests.
- Vercel for the hosted application, with production releases gated by GitHub Actions after environment setup.
- GitHub Actions for security checks on every push and pull requests to `main`.
- Gunicorn and AWS EC2 support are retained for a later/manual deployment path; EC2 is not the current automatic production deployment.

## Architecture

```text
User browser
    |
    v
Vercel-hosted Flask application
    |
    +--> app.py routes and validation
    +--> services/scheduling_service.py
    +--> Jinja templates + static CSS/JavaScript
    |
    v
MongoDB Atlas
    +--> accounts
    +--> users
    +--> groups
    +--> availability
    +--> plans
    +--> calendar_connections

GitHub push / pull request
    |
    v
GitHub Actions --> pytest -q
```

The application currently uses a single Flask entry point. Authentication identities live in the `accounts` collection; reusable scheduling members remain in `users` and are scoped by `account_id`. A migrated member also stores `linked_account_id`, while its account stores `legacy_member_id`, `migration_source`, and `must_change_password`. Groups record `owner_account_id`, associated account IDs, and account roles. Legacy records are not automatically assigned to ordinary newly registered accounts. The scheduling algorithm is kept in `services/scheduling_service.py` instead of inside a route handler.

## Repository structure

```text
app.py
services/
  __init__.py
  scheduling_service.py
  calendar_service.py
scripts/
  migrate_guest_accounts.py
templates/
  base.html
  landing.html
  login.html
  register.html
  account.html
  index.html
  users.html
  groups.html
  group_create.html
  group_edit.html
  group_detail.html
  member_availability.html
  plan_create.html
  plan_edit.html
  plan_detail.html
  error.html
static/
  css/style.css
  js/app.js
tests/
  __init__.py
  test_routes.py
  test_scheduling_service.py
  test_calendar_service.py
.github/workflows/security.yml
.github/workflows/release.yml
.env.example
requirements.txt
requirements-dev.txt
pytest.ini
```

## Local development

### 1. Clone the repository

```bash
git clone https://github.com/Enhan540/cs4800.git
cd cs4800
```

### 2. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If Windows does not recognize `python`, install Python from python.org with **Add Python to PATH** enabled, then reopen VS Code. The `py` launcher may also be used when it is available.

### 3. Install dependencies

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

The development file includes the test tools and references the base requirements.

### 4. Configure environment variables

Copy `.env.example` to `.env` and replace the placeholder values:

```env
MONGO_URI=mongodb+srv://USERNAME:PASSWORD@CLUSTER.mongodb.net/?retryWrites=true&w=majority
MONGO_DB_NAME=cs4800_database
SECRET_KEY=replace-with-a-long-random-value
GOOGLE_CLIENT_ID=replace-with-google-oauth-client-id
GOOGLE_CLIENT_SECRET=replace-with-google-oauth-client-secret
GOOGLE_REDIRECT_URI=http://127.0.0.1:5000/auth/google/callback
GOOGLE_CALENDAR_REDIRECT_URI=http://127.0.0.1:5000/auth/google-calendar/callback
SESSION_COOKIE_SECURE=false
PORT=5000
```

`FLASK_SECRET_KEY` is accepted as a backwards-compatible alternative to `SECRET_KEY`. Use the same stable `SECRET_KEY` for every instance of one deployment so signed login sessions and encrypted calendar connection secrets remain valid. Different local and production environments may use different keys, but changing a deployed key invalidates existing sessions and requires calendar connections to be reconnected. Set `SESSION_COOKIE_SECURE=true` for HTTPS production deployments; leave it false for plain-HTTP local development. `MONGO_URI`, `SECRET_KEY`, `GOOGLE_CLIENT_SECRET`, OAuth refresh tokens, and calendar-feed URLs must never be committed. `.env` is ignored by Git; `.env.example` contains placeholders only.

For Google Calendar, add the calendar callback above to the same Google OAuth client used for sign-in. The separate Calendar connection requests only free/busy and calendar-list read access. After connecting, choose one or more calendars; the primary calendar is selected by default. Existing connections must reconnect once to grant calendar-list access before choosing additional calendars. Configure matching local and deployed redirect URIs in Google Cloud Console before attempting a connection.

### Check a calendar connection locally

1. Sign in, open **Account**, connect Google Calendar, and confirm its status is **Connected**. Add two named iCal subscription links and confirm both appear separately.
2. Select the Google calendars to use. Put a busy event on a selected secondary calendar and a different busy event in each iCal feed. Use **Sync Now** on each connection.
3. Give the linked group member weekly availability spanning those events. Create a plan for their dates and confirm the preview and saved results omit all three busy periods.
4. Disconnect one feed and repeat the preview; only that feed's busy period should become available. Re-add the same URL twice and confirm it creates one connection.
5. Temporarily make a feed unavailable. If it has a successful cache for the requested dates, planning should show a stale-data warning; if it has never synced those dates, scheduling should be blocked until it syncs or is disconnected.

The automated suite mocks provider responses and cannot verify OAuth credentials, published feed permissions, or a live Google account. Calendar sync stores only busy intervals, never event titles. Google FreeBusy requests are split into short date windows covering the requested plan dates.

For an iCal connection, paste a direct HTTPS subscription feed URL. If the provider returns HTTP 429, the Account page now says it is limiting requests; automatic refresh waits 15 minutes before trying again, while a covered successful cache remains usable. HTTP 401, 403, and 404 each get a specific message, as does invalid iCal data. Check the deployed app with an owned public feed and, separately, a secret feed that works in a browser. Server logs record only a failure category and HTTP status, never the feed URL or event data. A provider that continues returning 429 cannot be made to serve a feed by the app.

A Google `public/basic.ics` address requires **Make available to public** for that calendar; the owner can choose **See only free/busy (hide details)**. If Google returns HTTP 404, check that setting and copy the public iCal address again. Google may take time to apply a sharing change. Keep a Google secret iCal address private and enter it only in the Account form.

In MongoDB Atlas, create a database user with `readWrite` access to the value used for `MONGO_DB_NAME`, and allow the development machine's IP address in Network Access. Avoid using a broad `0.0.0.0/0` rule unless it is temporary and understood.

### 5. Start the application

```bash
python app.py
```

Open <http://localhost:5000>. Data persists in MongoDB Atlas after refreshes and application restarts.

### 6. Configure Google Sign-In

In Google Cloud Console, create an OAuth 2.0 Client ID with application type **Web application**. Add the exact callback URLs used by each environment under **Authorized redirect URIs**, for example:

- `http://127.0.0.1:5000/auth/google/callback` for local development
- `https://your-production-domain.example/auth/google/callback` for production

Copy the client ID and client secret into the corresponding environment variables, and set `GOOGLE_REDIRECT_URI` to the exact authorized callback for that environment. Configure the OAuth consent screen as required by Google. Group Planner requests only basic OpenID Connect identity scopes during sign-in; do not add Google Calendar scopes for this authentication flow.

### 7. Migrate legacy guest accounts

The migration detects member names matching `guest#` (for example, `guest1`) without assuming a fixed count. It creates linked accounts with temporary migration credentials, preserves the existing member IDs and all records that reference them, and assigns `guest1` as owner of legacy groups. It does not run during Flask startup.

Always review the dry run first:

```bash
python scripts/migrate_guest_accounts.py --dry-run
```

Then run the controlled migration once:

```bash
python scripts/migrate_guest_accounts.py
```

The command is idempotent and can safely resume after a completed run. Before any writes, it asks for a distinct temporary password for each new account using hidden terminal prompts. Every password must pass the password policy. Automated callers must supply a `password_provider` callback; there is no shared default. It reports usernames and record counts and never prints credentials. Existing accounts and IDs are preserved. Migrated accounts must change their temporary password before accessing groups. Accounts still using the retired bootstrap credential need an administrator-assisted reset; old setup sessions are rejected.

## Typical demo workflow

1. Open **Members** and create Alex, Sam, and Chris.
2. Open each member and save recurring weekly availability.
3. Create a group named **Friday Friends** and add the three members.
4. Review the group's availability matrix.
5. Create a **Movie Night** plan with a date range, a 120-minute duration, and minimum attendance of two or three.
6. Review the ranked matching times and schedule one result.
7. Invite members and record their RSVP responses.
8. Refresh the application or restart Flask to confirm the records remain in MongoDB.

## Tests

Run the complete suite from the repository root:

```bash
pytest -q
```

The tests cover password authentication, mocked Google authentication, OAuth state validation, guest migration and idempotency, owner/member authorization, cross-account privacy, route validation, group/member/availability/plan workflows, date and duration handling, plan lifecycle and dashboard filtering, RSVP behavior, and the scheduling service. Scheduling tests include the three-person overlap case, no overlap, minimum attendance, durations that are too long, and multiple overlapping availability blocks.

## Deployment

### Vercel

The current production hosting target is Vercel. Connect the GitHub repository to a Vercel project, configure `MONGO_URI`, `MONGO_DB_NAME`, `SECRET_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, the production `GOOGLE_REDIRECT_URI`, and `SESSION_COOKIE_SECURE=true` in the Vercel project settings, and deploy from the intended production branch. Startup fails on Vercel if `SECRET_KEY` is missing. The production callback must use the deployed HTTPS domain and must exactly match a Google Cloud authorized redirect URI. Do not put secrets in the repository or frontend code. After deployment, replace the placeholder live URL near the top of this README with the actual public Vercel URL.

The committed `vercel.json` disables automatic Git deployments. Vercel project settings remain the source of truth for environment values and domains; no production URL is committed.

### GitHub Actions

`.github/workflows/security.yml` runs the required security gate on every push, pull requests targeting `main`, and manual runs. Scheduled checks cover `main`.

`.github/workflows/release.yml` releases the exact main commit after its push-triggered Security workflow succeeds. Configure the production environment and branch protections before enabling releases. No EC2 deployment job runs from this repository.

### Archived EC2/Gunicorn path

An EC2/Gunicorn deployment is no longer part of the active workflow. The old entry point, if needed for historical reference, was:

```bash
gunicorn --workers 2 --bind 0.0.0.0:5000 app:app
```

If an EC2 deployment is intentionally reintroduced later, it would require an environment file and a systemd service. The old workflow used these secrets:

- `EC2_HOST`
- `EC2_USER`
- `EC2_SSH_KEY`
- `EC2_APP_PATH`

Do not commit those values. EC2 deployment is not required for the current Vercel-hosted path.

## Security notes

- Keep `.env` out of Git and use environment variables for secrets.
- Use a dedicated MongoDB database user with only the required `readWrite` access.
- Restrict MongoDB Atlas Network Access to known IP addresses where practical.
- Keep GitHub and Vercel secrets in their secret-management settings.
- Rotate exposed credentials immediately, use HTTPS in production, and keep the OAuth callback URI list narrow.
- Google Sign-In stores the stable Google subject and verified email for identity matching; OAuth access and refresh tokens are not persisted.
- New passwords are checked offline against a pinned list of 3,000 common passwords of at least eight characters. Existing passwords remain usable for sign-in. The list's provenance and license are recorded in `.security/password-blocklist.json`.
- Changing a password or connecting/disconnecting a sign-in method ends other sessions. The session making an authenticated change stays signed in; password recovery ends all previous sessions.
- Email changes and Google disconnection require password confirmation for password accounts. Google-only accounts must have signed in within five minutes before adding a password or changing email. Email recovery applies to existing password accounts; Google-only users add a password after signing in with Google.
- New passwords cannot equal the username, email, or the app names documented in `services/password_policy.py`. Temporary migration passwords expire after 24 hours and must be replaced with a different password before group access.
- Production uses a Secure, HttpOnly cookie with the `__Host-` prefix. This changes the production cookie name, so existing browser sessions require a new sign-in after release.
- Sessions have a MongoDB record for immediate logout revocation, a 30-minute inactivity timeout, and a 12-hour absolute lifetime. Background notification polling does not renew activity. Expiry is checked on each authenticated request, independently of TTL cleanup.

## Project status

This repository contains the deployed first MVP for the CS4800 Software Engineering with AI project. It is intentionally focused on persistent group scheduling and is not the final feature set.
