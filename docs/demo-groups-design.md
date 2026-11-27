# Demo groups: design

Status: implemented on branch `demo-groups`.

## Goal

A signed-in user can create a **demo group** that comes pre-filled with fake members and their weekly availability. They can then try the plan feature end to end: create a plan, use Smart Plan Finder, schedule a time and set RSVPs. Nothing they do reaches another person, and nothing leaks into their real groups.

## How the app works today

The design builds on these behaviors:

- **Groups** hold:
  - `owner_account_id`
  - `account_ids`: the accounts that can open the group
  - `member_ids`: scheduling profiles in `users`
  - `account_roles` and `admin_permissions`

  `get_group`/`group_is_accessible` allow only accounts in `owner_account_id`/`account_ids`.
- **Scheduling profiles (`users`)** are either:
  - *linked*, with `linked_account_id`: a real person's profile
  - *unlinked*, with only `account_id`: a legacy profile owned by an account

  Unlinked profiles already behave like "people without a login": the owner edits their availability (`member_can_manage`) and RSVPs.
- **Unique index** `(account_id, normalized_name)` on `users`.
- **Six places** map a profile to an account with `linked_account_id or account_id`. For an unlinked profile, that resolves to its **owner**. As a result:
  - The owner's **other plans** count as the profile's commitments, in both directions.
  - Scheduling a plan sends a notification to the owner for each unlinked profile. They are deduplicated per recipient, so the owner sees one.
  - Calendar busy time is **not** affected: `calendar_busy_by_member` already uses only `linked_account_id`.
- **`delete_group`** deletes the group and its plans, but not profiles, availability or notifications.
- **Smart Plan Finder** treats plans the account has scheduled in *any* group as busy time.

## Rules

1. **Fake members are `users` records only.**
   - Never an `accounts` record and never `linked_account_id`, so they can't sign in, be invited, or receive anything.
2. **A demo group is private to its creator.**
   - `account_ids` is only the creator.
   - Invitations, role changes and admin permissions are refused for demo groups.
3. **No real calendar data in a demo.** Fake members never inherit the creator's Google/iCal busy time. The existing calendar code already guarantees this; a test keeps it that way.
4. **No demo data in real scheduling.** Demo plans never count as commitments in real groups, and real plans never block fake members.
5. **Fake members never leave their demo group.** They can't be added to any other group, and they don't appear in member lists or pickers outside that group.
6. **Demo data is temporary.**
   - It expires after 7 days.
   - It can be reset or deleted at any time.
   - Access checks use the expiry time; cleanup never decides who can see what.

## Data model

```text
groups (demo)
  is_demo: true
  demo_expires_at: <now + 7 days>
  owner_account_id, account_ids: [creator], account_roles: {creator: owner}
  member_ids: [creator's own profile, fake profile ids...]

users (fake member)
  is_demo: true
  demo_group_id: <group id>
  account_id: <creator>                 # ownership, for member_can_manage
  display_name: "Alex (demo)"
  normalized_name: "demo:<group id>:alex"   # avoids the unique-index clash with real names and other demos
  # never linked_account_id, never email
```

Plans, availability and notifications need no new fields. They are found through `group_id` or `user_id`.

## Code changes

### 1. One profile-to-account helper (rules 3 and 4)

Add `member_account_id(member)`, which returns `linked_account_id`, or `account_id` unless `is_demo`, and otherwise `None`. Replace the six `linked_account_id or account_id` expressions with it. The effects:

- **Commitments:** fake members have no other plans to conflict with.
- **Calendar busy time:** fake members still get none, as before.
- **Notifications:** scheduling a demo plan notifies only the creator, through their own linked profile.

Legacy unlinked profiles keep today's behavior. Changing that would be a separate decision.

### 2. Keep demo plans out of real scheduling (rule 4)

Add `is_demo: {"$ne": true}` to three lookups: the Smart Plan Finder commitment lookup (`committed_group_ids`), the plan page's conflict warnings (`get_user_plan_conflicts`), and the group page's weekly view. Also exclude fake profiles when gathering an account's own profiles. Then a scheduled demo plan never makes the creator look busy in a real group. Inside a demo group, demo plans still conflict with each other as usual.

### 3. Lock demo groups down (rules 2 and 5)

Refuse with 404/403 for demo groups or fake members:

- `POST /groups/<id>/invitations` (`send` and `search`)
- `POST /groups/<id>/members` (adding a legacy member)
- `POST /groups/<id>/members/<user_id>/role`
- `POST /users` with `group_id`

Exclude `is_demo` profiles from:

- `/users` (the member list)
- `add_group_member` choices
- any member picker that isn't the demo group itself

### 4. Expiry check (rule 6)

`get_group` and `get_plan` return 404 when `is_demo` and `demo_expires_at < now`. Expiry is therefore enforced on every request without any background job, which suits Vercel.

### 5. Cleanup (rule 6)

Add `delete_demo_group(db, group)`, which deletes the group's plans, its fake members' availability, its fake members, notifications with this `group_id`, and the group. Call it from:

- `delete_group` when `is_demo`
- the reset route below (everything except the group)
- an opportunistic sweep of the current user's expired demo groups when they open `/groups` or `/dashboard`. This is bounded and only touches their own data, so no cron job is needed.

## New routes

Both need `.security/routes.json` entries (`access: authenticated`, `login_guard: true`) and CSRF.

| Route | Who | What |
|---|---|---|
| `POST /groups/demo` | any signed-in user | Creates a demo group with fake members and availability. Limited to **one active demo group per account** and to creation through `security_rate_limits`. If one already exists, redirects to it. |
| `POST /groups/<group_id>/demo/reset` | demo group owner | Restores the fake members' availability and deletes the demo plans. 404 for non-demo groups. |

Deleting uses the existing `POST /groups/<group_id>/delete`.

## Fake members

Five fake members with recurring weekly availability, chosen so the finder produces interesting results: exact matches, partial attendance, and a "shorter than requested" option.

| Name | Pattern |
|---|---|
| Alex (demo) | Weekday evenings 5–9 PM |
| Jordan (demo) | Mon/Wed/Fri 9 AM–1 PM, weekends 10 AM–6 PM |
| Sam (demo) | Tue/Thu 12–8 PM, Sat 9 AM–12 PM |
| Taylor (demo) | Weekends only, 11 AM–7 PM |
| Riley (demo) | Weekday mornings 7–10 AM and Wed 6–8 PM |

The creator's own availability and calendars are used as normal for their own profile, so the demo shows how their real schedule fits.

## UI

- **Entry point:** a "Try a demo group" button on `/groups`, plus the dashboard's empty state when the user has no groups.
- **Demo group page:**
  - a "Demo" badge and a banner: "Fake members, for practice only. Deleted on <date>."
  - **Reset demo** and **Delete demo** buttons
  - invite and role controls hidden; they're also refused on the server
- **Fake members are labelled "(demo)" everywhere.** The owner can edit their availability and RSVPs, to see how changes affect the finder.

## Tests

- **Denial:**
  - Another account gets 404 for the demo group, its plans, its fake members' availability, and reset/delete.
  - Expired demo groups return 404.
  - Invitations, role changes and adding members to a demo group are refused.
  - Fake members can't be added to a real group.
- **Isolation:**
  - Fake members get no busy time from the creator's Google/iCal cache.
  - A scheduled demo plan doesn't block the creator in a real group's finder.
  - Scheduling a demo plan creates exactly one notification, for the creator.
  - Fake members never get `linked_account_id` or an `accounts` record.
- **Lifecycle:**
  - Only one active demo group per account.
  - Reset restores the defaults.
  - Delete and expiry cleanup remove all related plans, availability, fake members and notifications, and nothing else.
- **Security:** CSRF and login stay enabled; the `routes.json` inventory check passes; `security_check.py audit --worktree` reports nothing blocking.

## Decisions for review

| Question | Recommendation |
|---|---|
| Expiry | 7 days |
| Active demo groups per account | 1 |
| Number of fake members | 5 |
| Include the creator as a member | Yes. It shows their real schedule against the fake members. |
| Change the legacy unlinked-profile behavior too | No, keep it for a separate change |
