# AGENTS.md — Group Planner handoff

## Purpose

This file is the continuation guide for AI agents working on Group Planner. Read it before changing code. The repository is a Flask/Jinja application, not a Next.js or React application.

## Current repository

- Repository: `C:\Users\lazer\OneDrive\Documents\GitHub\cs4800`
- Stack: Python Flask, Jinja templates, vanilla JavaScript/CSS, MongoDB Atlas, Flask-Login, Authlib, Vercel.
- Main entry point: `app.py`.
- Database collections include `accounts`, `users`, `groups`, `availability`, `plans`, `calendar_connections`, `group_invites`, `notifications`, `account_tokens`, `google_oauth_states`, and `google_link_states`.
- The application uses the `accounts` collection for authenticated Group Planner accounts. Scheduling/member records remain in `users` and may reference an account through `account_id` or `linked_account_id`.
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

The Google redirect URLs must exactly match the Authorized redirect URIs in Google Cloud. Keep local and production values separate. Google sign-in and Google Calendar authorization are separate OAuth flows. Google Calendar is currently deferred/broken and must not be repaired as part of unrelated work.

## Authentication state

### Password accounts

- Passwords use Werkzeug `generate_password_hash` and `check_password_hash`.
- New signup accepts username, email, password, and confirmation. Email is normalized and starts unverified.
- Legacy accounts without email remain valid; do not destructively migrate them.
- `must_change_password` is retained for migrated legacy accounts.

### Google accounts

- Authlib handles Google OIDC.
- Google `sub` is authoritative and unique through a sparse MongoDB index.
- Google-created accounts may have no password.
- A verified Google email may populate verified account email fields.
- A Google email matching an existing password account must not automatically merge. The app stores a short-lived, server-side pending-link record and requires password proof before linking.
- A Google identity may belong to only one account.
- Account → Connected Accounts contains Google login separately from Connected Calendars.
- Google-only users cannot disconnect their only login method. Password + Google users can disconnect Google.

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
- Authorization must be enforced server-side, not only in templates.
- Preserve user/account IDs and existing MongoDB data.

## Important deferred issue

Google Calendar FreeBusy synchronization is currently deferred. A local sync request has returned Google HTTP 400 `timeRangeTooLong`; the code was changed to request 364 days, but this has not been fully verified in the user's running localhost copy. Do not spend the next task repairing Google Calendar unless explicitly requested. iCal remains the active calendar integration.

## Test status at handoff

The focused authentication tests pass:

```text
22 passed
```

The full suite last reported:

```text
154 passed, 1 failed
```

The remaining failure is `tests/test_routes.py::test_sync_now_replaces_stale_shifted_ical_cache`. It is an outdated iCal test that expects the old incorrect timezone-shifted result and uses an ownerless test connection. Do not reintroduce the old timezone behavior merely to satisfy that test; update the test only when explicitly handling the deferred iCal work.

## Working rules for the next agent

1. Inspect current code and dirty changes before editing. Do not reset or discard unrelated work.
2. Use `rg` for code discovery and `apply_patch` for edits.
3. Keep authentication identities separate from application data. Never create duplicate accounts during linking or recovery.
4. Never log or commit plaintext passwords, raw production tokens, OAuth secrets, Resend keys, MongoDB credentials, or calendar URLs.
5. Add focused tests for new behavior and run them before the full suite.
6. Be explicit about known failing tests and do not claim full completion when the browser flow has not been verified.
7. For Vercel, prefer stateless requests and MongoDB-backed state. Do not introduce long-running workers or in-memory-only locks.
8. Do not modify Google Calendar while working on account/email recovery unless the user explicitly changes scope.
