# AGENTS.md — Group Planner handoff

## Purpose

This file is the continuation guide for AI agents working on Group Planner. Read it before changing code. The repository is a Flask/Jinja application, not a Next.js or React application.

## Current repository

- Repository: `C:\Users\lazer\OneDrive\Documents\GitHub\cs4800`
- Stack: Python Flask, Jinja templates, vanilla JavaScript/CSS, MongoDB Atlas, Flask-Login, Authlib, Vercel.
- Main entry point: `app.py`.
- Database collections include `accounts`, `users`, `groups`, `availability`, `plans`, `calendar_connections`, `group_invites`, `notifications`, `account_tokens`, `google_oauth_states`, and `google_link_states`.
- The application uses the `accounts` collection for authenticated Group Planner accounts. Scheduling/member records remain in `users` and may reference an account through `account_id` or `linked_account_id`.
- Security state uses `security_sessions` and `security_rate_limits` in MongoDB. Session IDs are randomly generated; only their hashes are stored. Logout revokes the matching session, idle time is limited to 30 minutes, and absolute lifetime to 12 hours. Do not replace this with process-local state or rely on TTL cleanup for authorization.
- Do not assume the README's older “planned features” list is current; notifications, Google account linking, and email-account recovery have been implemented since portions of the README were written.

## Setup on a new computer

PowerShell:

```powershell
cd "C:\path\to\cs4800"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

If `python` is unavailable, install Python 3.11+ and enable PATH integration, or use the Windows `py` launcher. A copied `.venv` may point to the previous computer's Python installation; recreate it instead of trusting the copied environment.

Run the application locally:

```powershell
python app.py
```

Run tests:

```powershell
python -m pytest -q
```

## Environment variables

Copy `.env.example` to `.env`. Never commit `.env`, credentials, tokens, or real MongoDB URLs.

Required or commonly used local values:

```env
MONGO_URI=mongodb+srv://...
MONGO_DB_NAME=cs4800_database
SECRET_KEY=<stable-long-random-value>
GOOGLE_CLIENT_ID=<Google OAuth client ID>
GOOGLE_CLIENT_SECRET=<Google OAuth client secret>
GOOGLE_REDIRECT_URI=http://127.0.0.1:5000/auth/google/callback
GOOGLE_CALENDAR_REDIRECT_URI=http://127.0.0.1:5000/auth/google-calendar/callback
SESSION_COOKIE_SECURE=false
APP_TIMEZONE=America/Los_Angeles
APP_BASE_URL=http://127.0.0.1:5000
EMAIL_DELIVERY_MODE=console
```

For Vercel production, use the production HTTPS domain:

```env
SECRET_KEY=<stable production secret>
MONGO_URI=<Atlas URI>
MONGO_DB_NAME=cs4800_database
GOOGLE_CLIENT_ID=<client ID>
GOOGLE_CLIENT_SECRET=<client secret>
GOOGLE_REDIRECT_URI=https://<production-domain>/auth/google/callback
GOOGLE_CALENDAR_REDIRECT_URI=https://<production-domain>/auth/google-calendar/callback
SESSION_COOKIE_SECURE=true
APP_TIMEZONE=America/Los_Angeles
APP_BASE_URL=https://<production-domain>
EMAIL_DELIVERY_MODE=resend
RESEND_API_KEY=<Resend API key>
FROM_EMAIL=<verified sender address>
```

The Google redirect URLs must exactly match the Authorized redirect URIs in Google Cloud. Keep local and production values separate. Google sign-in and Google Calendar authorization are separate OAuth flows. Google Calendar now requests free/busy and read-only calendar-list scopes so users can select calendars; the new scope and live flow still need production consent and verification.

## Authentication state

### Password accounts

- Passwords use Werkzeug `generate_password_hash` and `check_password_hash`.
- New signup accepts username, email, password, and confirmation. Email is normalized and starts unverified.
- Legacy accounts without email remain valid; do not destructively migrate them.
- `must_change_password` is retained for migrated legacy accounts.
- Pending legacy setup requires an unexpired, individually supplied temporary password and a different replacement before group access. Temporary credentials expire after 24 hours. Do not restore a shared bootstrap credential or silently reset existing accounts during migration.

### Google accounts

- Authlib handles Google OIDC.
- Google `sub` is authoritative and unique through a sparse MongoDB index.
- Google-created accounts may have no password.
- A verified Google email may populate verified account email fields.
- A Google email matching an existing password account must not automatically merge. The app stores a short-lived, server-side pending-link record and requires password proof before linking.
- A Google identity may belong to only one account.
- Account → Connected Accounts contains Google login separately from Connected Calendars.
- Google-only users cannot disconnect their only login method. Password + Google users can disconnect Google.

### Optional authenticator verification

- Users may enable time-based authenticator codes in Account. Once enabled, password and Google sign-in both require an authenticator or one-use recovery code. Account recovery must preserve this requirement.
- `mfa_setups` and `mfa_challenges` hold short-lived, browser/session-bound state. Encrypted seeds and hashed recovery codes live in `accounts.mfa`. Only the immediate enrollment/replacement page displays recovery codes. Never log these values or send them to an external QR service.
- Security-setting changes require primary proof and, when enabled, a fresh second-factor proof. Use the shared atomic mutation/proof helpers; never call `login_user` for an enabled account before the challenge completes. Factor changes revoke other sessions.
- `security_alerts` holds durable, generic change notifications. The optional trusted retry workflow needs its own restricted environment and scoped credentials; actual provider delivery and schedule activation remain deployment verification tasks.

## Phase 2 email/recovery implementation

`services/email_service.py` is the provider abstraction. It supports:

- `EMAIL_DELIVERY_MODE=console` for local development. It logs verification/reset links but never passwords or API keys.
- `EMAIL_DELIVERY_MODE=resend` for production through the Resend HTTP API.

Implemented routes include:

- `/verify-email/<token>`
- `/account/email`
- `/account/email/resend`
- `/forgot-password`
- `/reset-password/<token>`
- `/account/password/set`

Tokens are cryptographically random, only token hashes are stored, verification tokens expire after 24 hours, reset tokens expire after 1 hour, and reset tokens are single-use. Email sending is throttled through MongoDB-backed rate-limit records. Forgot-password responses are intentionally generic to prevent account enumeration.

## Existing product systems to preserve

- Groups, roles, invitations, notifications, plans, RSVP, partial attendance, recurring availability, iCal availability, Smart Plan Finder, MongoDB persistence, Vercel compatibility, and Stitch styling.
- Availability uses five-minute input precision. Group graphs may use coarser visual intervals, but do not change scheduling precision.
- Do not expose private calendar event titles/details to other users.
- Group removal must revoke indirect access through plan notifications, counters, summaries, page markup and notification actions. Use the shared notification visibility query; recipient ownership alone does not grant continuing access to private group updates. Preserve pending invitations and private account-security alerts.
- Authorization must be enforced server-side, not only in templates.
- Preserve user/account IDs and existing MongoDB data.

## Important deferred issue

Google Calendar FreeBusy synchronization is implemented with short date chunks and selected calendar IDs. Its OAuth selection flow and selected secondary calendars still need a real Google account check after deployment; mocked tests do not prove production consent or permissions.

## Test status at handoff

The full suite last reported `204 passed, 78 mongomock deprecation warnings`. The new Google calendar-list scope and real secondary-calendar selection have not yet been verified against Google's production consent flow.

## Working rules for the next agent

### Required security cycle

- Before changes, identify affected users, records, permissions, external inputs, and trust boundaries. Preserve explicit group sharing and keep account/calendar secrets private.
- Add denial and abuse tests when changing authentication, authorization, validation, integrations, or sensitive data. Security tests must keep login and CSRF enabled.
- Account email changes and Google disconnection require current-password confirmation for password accounts. Google-only account changes require a provider sign-in within five minutes; email recovery must not add a password to a Google-only account. Production cookies use the `__Host-` prefix and production startup must fail if required database indexes cannot be verified.
- New or changed routes require a reviewed `.security/routes.json` entry. Run the inventory checker; do not classify protected routes as public to make checks pass.
- Install `requirements-security.txt`, Chromium (`python -m playwright install chromium`), Docker, and the local hooks (`python scripts/security_install_hooks.py`). Run `python scripts/security_check.py audit --worktree` during development. Commit/push gates test the index or committed snapshot, never unrelated unstaged content.
- Serious findings, credential exposure, account takeover, unauthorized cross-user access, scanner failures, and incomplete checks block progression. Never suppress a real issue to obtain a passing check.
- Keep detailed findings only in `%LOCALAPPDATA%/GroupPlanner/Security/security-risk-register.json`. Report unresolved lower risks before commits/pushes. Never put matched secrets, exploit details, reports, or production data in Git, public issues, CI logs, or uploaded artifacts.
- Local hooks can be bypassed; `main` is the only long-lived branch and requires server-side protection and security checks. Only a successfully checked commit may be released. Never use production data for active scanning.
- The checked release uses `scripts/security_release.py` with pinned `requirements-release.txt`, not a separately downloaded CLI. Keep every root Python dependency manifest in advisory scans. Require the checked SHA, project owner, connected GitHub repository/main branch, and resulting production-alias proof. Mocked release tests cannot establish hosted access or provider build-tool patching; verify an isolated project before activation.
- Do not declare staging, Google consent, edge HTTP/2 protection, Atlas access settings, or production deployment verified without direct evidence.

1. Inspect current code and dirty changes before editing. Do not reset or discard unrelated work.
2. Use `rg` for code discovery and `apply_patch` for edits.
3. Keep authentication identities separate from application data. Never create duplicate accounts during linking or recovery.
4. Never log or commit plaintext passwords, raw production tokens, OAuth secrets, Resend keys, MongoDB credentials, or calendar URLs.
5. Add focused tests for new behavior and run them before the full suite.
6. Be explicit about known failing tests and do not claim full completion when the browser flow has not been verified.
7. For Vercel, prefer stateless requests and MongoDB-backed state. Do not introduce long-running workers or in-memory-only locks.
8. Do not modify Google Calendar while working on account/email recovery unless the user explicitly changes scope.
