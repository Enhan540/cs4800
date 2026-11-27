# Members without accounts: design

Status: version 1 implemented on branch `managed-members`; version 2 not started.

## Goal

A group owner can add people who don't have a Group Planner account, for example "Grandma" or "Coach Lee". The owner, or an admin they allow, then enters those people's weekly availability and RSVPs. Plans and Smart Plan Finder treat them like any other member.

If that person later creates an account, the owner can invite them to **take over** their profile. They then join the group as a normal member, keeping their place in the group's plans.

## How it works today

- **Scheduling profiles (`users`)** are either *linked* (`linked_account_id`: a real login) or *unlinked* (only `account_id`, the account that created it). Unlinked profiles exist today only as legacy migration records. The UI no longer creates them:
  - `POST /users` with `group_id` remains as a compatibility path, with no link to it in the UI.
  - `POST /groups/<id>/members` adds an existing legacy profile.
- **Who can manage a profile** (`member_can_manage`): the account that owns it, or a group admin with `edit_member_availability`. **RSVPs** for other members need `edit_member_rsvp`.
- **Problem to fix:** `member_account_id()` maps a legacy unlinked profile to the account that created it. That account's plans would then count as the person's commitments, and the person's plans as the account's. Demo groups already fixed this for `is_demo` profiles. Members without accounts need the same fix.
- **Invitations** go to an existing account by username. Accepting one adds the account's own profile to `member_ids` and `account_ids`.
- **The group page** only shows the member settings menu (remove, role) for linked members, so unlinked members currently can't be managed from the page.

## Rules

1. **A member without an account is a `users` record only.**
   - No `accounts` record, no `linked_account_id`, no password, and no email in the first version.
   - It can't sign in or receive notifications or emails.
2. **It belongs to one group.**
   - It can't be added to other groups, can't be invited, and has no role.
   - The same person in two groups is entered twice. Version 2 may allow reuse.
3. **Only people the group already trusts can manage it:**
   - **Add, rename, remove:** the owner, or admins with `manage_members`.
   - **Edit availability:** the owner, or admins with `edit_member_availability`.
   - **Set RSVPs:** the owner, or admins with `edit_member_rsvp`.

   All checks happen on the server.
4. **It never stands in for a real account.** Its availability is only what was entered for it. It never inherits its creator's calendar, plans or notifications, and never makes the creator look busy.
5. **Taking over a profile requires the real person's consent.** The owner can't attach a profile to someone's account. The person must sign in and accept an invitation that names the profile. Taking over never merges two accounts.
6. **Removing it deletes it,** along with its availability. It disappears from the group's plans the same way a removed member does today.

## Data model

```text
users (member without an account)
  is_managed: true
  managed_group_id: <group id>
  account_id: <account that created it>     # audit trail; not used for access
  display_name: "Grandma"
  normalized_name: "managed:<group id>:grandma"   # unique within the group, no clash with real names
  created_by_account_id, created_at, updated_at
  # never linked_account_id, never email

group_invites (take-over invitation, version 2)
  ...existing fields...
  claims_member_id: <managed profile id>   # optional
```

The group keeps listing these profiles in `member_ids`. Its `account_ids` never change for them.

## Code changes: version 1, adding and managing members without accounts

1. **Helper:** `member_account_id()` returns `None` for `is_managed` profiles, as it does for `is_demo`. Rename the guard to a shared `has_no_login(member)` check. Exclude `is_managed` profiles where an account's own profiles are gathered for commitment and conflict lookups.
2. **Access:** `member_can_manage` for an `is_managed` profile checks group permissions on **its group only**, not "you created it". If an admin who created a profile is later removed from the group, they lose access to it.
3. **New routes** (login, CSRF, rate limited, and entries in `.security/routes.json`):

   | Route | Permission | Does |
   |---|---|---|
   | `POST /groups/<group_id>/managed-members` | `manage_members` | Adds a member without an account (name 1–60 characters, trimmed, unique in the group, at most 30 per group). |
   | `POST /groups/<group_id>/managed-members/<user_id>/rename` | `manage_members` | Renames it. |

   Removal uses the existing `POST /groups/<id>/members/<user_id>/remove`. For an `is_managed` profile it deletes the profile and its availability, and removes it from the group's plans, including RSVP entries.
4. **Lockdown:** `add_group_member`, invitations, role changes and `/users` refuse `is_managed` profiles. A profile from another group returns 404.
5. **Group deletion:** deletes the group's members without accounts and their availability.
6. **Demo groups:** fake members are already a kind of member without an account. Keep demo groups as they are, but share the helper.

## Code changes: version 2, taking over a profile

1. **Invite with a profile.** The invite dialog gets an optional "This person is…" picker, listing the group's members without accounts. The invitation stores `claims_member_id`. Only `manage_members` holders can send it, and one pending take-over is allowed per profile.
2. **Accepting**, as one ordered update that can safely run twice:
   1. Check the invitation is still pending and for this account, and the profile still belongs to this group and is `is_managed`.
   2. Add the account's own profile to the group, as accepting does today.
   3. In the group's plans, replace the managed profile ID with the account's profile ID in `invited_member_ids`, `scheduled_available_member_ids` and the `rsvps` keys. The person keeps their place and existing RSVPs.
   4. Delete the managed profile and the availability someone else entered for it. The person's own account availability is what counts from now on.
   5. Notify the owner with a generic message, such as "Grandma's profile was taken over by @username".
3. **Declining** leaves the managed profile untouched.
4. **If the account is already in the group,** the invitation can't be sent. The owner removes the duplicate profile instead.

## UI

- **Group page:**
  - an "+ Add member without account" button next to "+ Invite", for people with `manage_members`
  - a "No account" label on those members
  - a ⋮ menu for rename and remove, shown even though they have no linked login
- **Availability:** the member's name opens the existing availability editor, for anyone allowed to edit availability.
- **Plans:** RSVP controls for these members appear for people with `edit_member_rsvp`, through the existing control for other members.
- **Version 2:** the invite dialog's "This person is…" picker, and on the person's side, an invitation that reads "You're invited to join <group> as Grandma."

## Tests

- **Denial:**
  - Ordinary members, admins without the right permission, removed members and outsiders can't add, rename, remove, edit availability or RSVP for a member without an account.
  - Another group's owner gets 404 for its availability.
- **Isolation:**
  - The creator's real plans don't block the member without an account, and that member's plans don't block the creator, in either Smart Plan Finder or the conflict warnings.
  - No notification or email is ever addressed to it.
- **Lockdown:**
  - It can't be invited, given a role, added to another group or reached through `/users`.
  - The 30-per-group limit and the duplicate-name check hold.
- **Lifecycle:** removing it and deleting the group delete it and its availability, and leave everything else.
- **Version 2:**
  - Taking over requires the invited account to accept.
  - A different account can't use the invitation.
  - Accepting moves plan participation and RSVPs, and running it twice changes nothing more.
  - Declining changes nothing.
  - An admin without `manage_members` can't send a take-over invitation.
- **Permission matrix:** the new routes are added to `test_group_capabilities.py`, and the route inventory and security audit pass.

## Decisions for review

| Question | Recommendation |
|---|---|
| Who can add members without accounts | Owner, plus admins with `manage_members` |
| One person in several groups | Version 1: entered separately per group. Revisit after use. |
| Store an email for them | No. That's personal data about someone who never agreed to it, and nothing would use it. |
| Limit per group | 30 |
| On take-over, keep the availability others entered | No, use the person's own account availability (they can copy it by hand) |
| Build version 2 now or later | Later. Version 1 covers the "nobody has an account yet" case on its own. |
| Existing legacy unlinked profiles | Leave as they are. Converting them is a separate migration. |
