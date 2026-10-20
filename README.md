# Group Planner

Group Planner is the CS4800 Software Engineering with AI MVP for coordinating recurring availability across small groups. Members save weekly availability, groups compare those windows, and plans can find and schedule matching meeting times.

Live application: **replace this text with the deployed Vercel URL** (for example, `https://your-project.vercel.app`). The production URL is intentionally not guessed or committed as a secret.

## Problem

Finding a meeting time for a group is often an awkward exchange of messages. People may have different weekly schedules, and a simple list of individual availability does not make the overlap obvious. Group Planner keeps reusable member records, recurring availability, and group plans in one place so a group can identify realistic meeting windows quickly.

## Current MVP features

- Create private accounts with a username and securely hashed password.
- Sign in with Google using OpenID Connect identity scopes (`openid email profile`) without requesting Google Calendar access.
- Keep dashboards, groups, members, availability, and plans scoped to the authenticated account.
- Support an explicit, idempotent migration that links legacy `guest#` member profiles to authenticated accounts while preserving schedules, memberships, plans, and RSVPs.
- Create, edit, view, and delete groups with optional descriptions.
- Create reusable members with optional email addresses and add them to multiple groups.
- Save multiple recurring weekly availability windows per member and day.
- Use a unified typed/time-picker editor with five-minute availability precision.
- Edit or remove availability individually or in a batch.
- View a group's availability in a member-by-day matrix.
- Create plans with a date range, meeting duration, allowed time range, and minimum attendance.
- Find matching times through a separate scheduling service. Candidates are searched in 30-minute increments, ranked by attendance, and shown with member availability details.
- Support arbitrary positive whole-minute meeting durations and clearly label shorter alternatives when no full-duration match exists.
- Schedule a selected result and keep the confirmed time in MongoDB.
- Invite group members to scheduled plans and record attending/not-attending RSVP responses, including optional late-arrival and early-departure times.
- Show planning and upcoming scheduled plans separately; the dashboard shows only scheduled plans that have not ended.
- Provide validation, flash messages, and 404/500/503 error pages.
- Run automated tests locally and in GitHub Actions.

## Planned features

These are intentionally outside the current MVP:

- Plan voting or preference collection.
- Cross-account group invitations and role-management UI.
- Google, Apple, or Outlook calendar integration.
- Notifications, chat, maps, mobile clients, and fairness scoring.

## Technology stack

- Python 3.11+ and Flask 3.1.
- Flask-Login for authenticated sessions and Authlib for Google OpenID Connect.
- Jinja2 templates with HTML, CSS, and vanilla JavaScript.
- MongoDB Atlas with PyMongo.
- `mongomock` and `pytest` for isolated tests.
- Vercel for the current hosted application, configured through the Vercel project/Git integration.
- GitHub Actions for test automation on `main` and pull requests.
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
.github/workflows/deploy.yml
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
SESSION_COOKIE_SECURE=false
PORT=5000
```

`FLASK_SECRET_KEY` is accepted as a backwards-compatible alternative to `SECRET_KEY`. Use the same stable `SECRET_KEY` for every instance of one deployment so signed login sessions remain valid. Different local and production environments may use different keys. Set `SESSION_COOKIE_SECURE=true` for HTTPS production deployments; leave it false for plain-HTTP local development. `MONGO_URI`, `SECRET_KEY`, and `GOOGLE_CLIENT_SECRET` must never be committed. `.env` is ignored by Git; `.env.example` contains placeholders only.

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

The command is idempotent and can safely resume after a completed run. It reports usernames and record counts but never prints passwords, hashes, connection strings, or OAuth credentials. Migrated accounts have `must_change_password: true`; ordinary password and Google accounts have it set to false.

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

The repository does not currently commit a `vercel.json` or a production URL, so the Vercel project settings are the source of truth for the live deployment configuration.

### GitHub Actions

`.github/workflows/deploy.yml` runs the test job on pushes to `main`, pull requests targeting `main`, and manual workflow runs. It installs the development dependencies and runs `pytest -q`.

The EC2 job remains in the workflow for later use, but it is manual-only. It has `needs: test` and runs only when the workflow is started with **Run workflow** (`workflow_dispatch`). A normal push to `main` runs tests and skips EC2 deployment.

### Optional EC2/Gunicorn path

For a later EC2 deployment, the retained production entry point is:

```bash
gunicorn --workers 2 --bind 0.0.0.0:5000 app:app
```

Create the environment file directly on the server and configure the existing `cs4800-availability` systemd service. The manual workflow expects these GitHub repository secrets:

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

## Project status

This repository contains the deployed first MVP for the CS4800 Software Engineering with AI project. It is intentionally focused on persistent group scheduling and is not the final feature set.
