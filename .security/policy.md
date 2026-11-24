# Application security boundaries

Review these rules before changing routes, identities, integrations, or sensitive data. Record each change's affected users, records, inputs, permissions, and trust boundaries in the pull request. The private audit register contains findings and verification gaps; this document describes intended application behavior.

## Identities and permissions

- `accounts` are sign-in identities; `users` are scheduling profiles. Their IDs may differ. Preserve linked and unlinked legacy profiles and existing group sharing.
- Every route is classified in `routes.json`. Public classification requires a product reason; hiding a button never grants or denies server permission.
- Group Owners control their group. Admins receive individual named permissions. Members receive the shared group view and their own permitted actions. Recheck membership and permissions on each request.
- Account credentials, recovery tokens, provider tokens, subscription URLs, and private calendar names/details remain private to the account even when scheduling availability is shared.
- Private group notifications require current membership when created and when listed, counted, or summarized. Stale plan participant lists cannot grant access after removal. Explicit pending invitations remain visible to their recipients, and private account-security alerts stay available independently of group membership.
- Analytics runs only on the public home and privacy pages. Never load analytics on sign-in, account, recovery, OAuth callback, or private group/calendar pages.
- MongoDB does not provide SQL-style row-level security here. Scope queries and mutations by the authenticated account, resource ownership, or an explicitly checked group permission. Validate every ID in a mixed batch before applying changes.

## Authentication and recovery

- Google identities use a validated provider subject, never email alone. An email collision requires proof of the existing account before linking.
- Passwords are accepted as supplied, including Unicode and spaces. New passwords have 8–1024 characters and are checked offline against the pinned common-password list plus whole-value username, email, and product-name matches. Existing ordinary passwords remain usable for sign-in.
- Password-account email changes and Google disconnection require the current password. Google-only changes and adding a sign-in method require authentication within five minutes. Google-only accounts add a password after provider sign-in; email recovery cannot create one.
- Recovery links are random, purpose-bound, expiring, single-use, and bound to the current verified email and account security version. Later account-security changes invalidate old links.
- New migration credentials are distinct, supplied through hidden operator prompts, expire after 24 hours, and require a different replacement before group access. Migration never silently overwrites existing account credentials or IDs.
- Authenticator verification is optional by explicit product policy. After enrollment, both native password and Google sign-in require a time-based code or an unused recovery code before account access. An email reset cannot remove or bypass the factor. Optional enrollment does not satisfy an assurance requirement that mandates MFA for every account; keep that conformance gap visible.
- Setup is short-lived and bound to the authenticated account, session, and security version. Generate QR images locally. Encrypt authenticator seeds with a purpose-specific key derived from the stable signing secret; store only recovery-code hashes. Never log or put seeds, provisioning URLs, or recovery codes in cookies or links. Recovery codes appear only in the immediate enrollment/replacement response.
- Consume authenticator time steps and recovery codes atomically. Limit attempts across all challenges for the account. Changing or removing the factor requires primary proof plus the existing second factor, and revokes other sessions and pending sign-ins. Losing both the authenticator and all saved codes must not trigger an email-only bypass.
- Authenticator codes use only the server's current 30-second time step; client-supplied clocks and adjacent steps are not accepted. Keep the hosting time source synchronized and verify that operationally. A code already used for enrollment or another action must be replaced by a new code.
- Notify the account's previously verified email and owned in-app inbox after sign-in settings change. Queue retries durably; payloads contain only the kind and time of change, never credentials, recovery links, or MFA data.

## Sessions and requests

- MongoDB stores only a hash of the random session ID. Logout revokes the matching record. Account-security versions revoke other sessions after credential or recovery changes.
- A 30-minute inactivity limit reduces exposure on unattended devices; a 12-hour absolute lifetime prevents indefinite renewal. Notification polling and static assets do not extend activity. Sensitive changes use a five-minute authentication window or current-password proof.
- Production cookies use `__Host-`, Secure, HttpOnly, Path=/, no Domain, and SameSite=Lax for the OIDC redirect flow. Expiry and ownership checks apply independently of MongoDB TTL cleanup.
- CSRF tokens stay enabled on forms and same-origin mutations. Enforce bounded request bodies, form fields, usernames, passwords, search queries, and batch sizes. Database-backed quotas cover password authentication and expensive scheduling operations; deployment proxy/IP behavior still requires environment validation.
- Production requires HTTPS configuration, verified database TLS, a stable random signing key, real email delivery, trusted hosts, and required identity indexes. Do not activate authentication/debug bypasses in production.

## Integrations and development gates

- Outbound iCal requests require public HTTPS destinations, checked/pinned addresses, verified TLS, bounded redirects, timeouts, response size, and recurrence expansion. Never expose another account's provider secrets or calendar details.
- Security tests use synthetic accounts with login and CSRF enabled. Browser and real-MongoDB checks run against disposable infrastructure; hosted consent/configuration checks require separate evidence.
- Commit gates inspect the index; push/CI gates inspect an exact commit. Scanner failure, incomplete checks, exposed credentials, serious findings, and unauthorized access block progression. Keep lower risks visible with an owner and deadline; never hide serious findings in a baseline.
- Fresh scanner hits reopen historical closed/false-positive annotations. Private triage alone cannot suppress a new result at the same source location. Any scanner-rule refinement must be narrow, reviewed, and backed by regression coverage that still detects the unsafe case.
- Required checks are bound to the official GitHub Actions app, with approval after the last push. Audit helpers reject provider redirects and verify live read-back. Existing branch rules are preserved; strengthen missing safeguards without overwriting stricter settings. The ASVS worksheet uses a size-bounded, checksum-verified standard source and rejects changed input before writing evidence.
- Keep detailed reports outside Git and sync folders. CI reports redacted status/counts and publishes no detailed artifacts. Production releases require the exact successfully checked commit, trusted credentials, branch protection, and an isolated preview environment.
- The trusted release job uses the official HTTPS API with an explicit checked Git SHA. Verify project ownership, connected repository and main production branch before requesting deployment; verify the resulting commit, project and configured production alias before reporting success. Never follow provider redirects or log provider bodies or credentials. Install only the pinned release HTTP dependencies, include every root Python dependency manifest in advisory scans, and keep deployment credentials out of installation steps.
- Hosted release verification must use an isolated project first. A source-level or mocked check cannot establish actual Git integration access, provider build-tool patching, environment separation, or production routing.

## Security alert delivery

`scripts/security_dispatch_alerts.py` retries up to 50 queued alerts per invocation. It reads only explicit environment configuration, requires HTTPS and verified MongoDB TLS, and does not import the application or load `.env`. Immediate delivery also occurs after a confirmed change. Interrupted changes reconcile through an account marker committed atomically with the change.

The `Retry security alerts` workflow is disabled until `SECURITY_ALERT_RETRIES_ENABLED=true` is configured. Before activation, restrict the `security-alerts` environment to trusted `main` deployments and provide a separately scoped database account with read/update access to `accounts`, `security_alerts`, and `notifications`, plus the delivery credential. Never make these secrets available to pull requests. The workflow uses no Google or application signing secrets. Actual provider delivery and remote environment restrictions require direct verification.

Provider idempotency deduplicates identical alert retries for 24 hours. A longer outage can produce another copy; delivery is at least once. Queued alerts expire after 30 days, and only bounded batches run, so monitor failures and backlog before enabling this schedule. No background worker is required in Vercel.
