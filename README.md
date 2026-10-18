# Meetwise — CS4800 Group Scheduling MVP

Meetwise is a deployable first MVP for recurring group scheduling. It lets users create reusable members and groups, record weekly availability, create plans, and rank matching meeting windows by attendance.

## MVP features

- Persistent `users`, `groups`, `availability`, and `plans` MongoDB collections
- Reusable members with optional email addresses
- Groups with compact member management and a responsive weekly availability matrix
- Multiple recurring availability windows per member and day
- Add, edit, and delete availability
- Plans with any positive whole-minute duration, date range, allowed time range, and minimum attendance
- A separate scheduling service that searches start times in 30-minute increments while preserving arbitrary durations
- Ranked exact matches, or clearly labeled shorter alternatives when no full-duration match exists
- Plan editing, one-click scheduling from ranked results, and a persistent scheduled-time summary
- Safe POST-only deletion for plans and groups; group deletion preserves reusable members and availability
- Group-aware member creation that automatically adds a new reusable member to the originating group
- Editable group names and descriptions without changing members, plans, or group IDs
- Separate **Scheduled Plans** and **Planning** sections on group pages
- Dashboard sections for upcoming scheduled plans, plans needing attention, and groups
- Automatic best-time generation when a plan is created or opened
- Debounced scheduling previews while plan fields are changed, without creating temporary plan records
- Persistent per-member RSVP responses: attending, not attending, arriving late, or leaving early
- RSVP attendance summaries on scheduled plans and plan cards
- Flask validation, flash messages, and 404/500/503 pages
- Pytest scheduling tests and GitHub Actions CI before EC2 deployment

## Stack

- Python 3.11+ and Flask
- MongoDB Atlas with PyMongo
- HTML, CSS, and JavaScript
- Gunicorn for production
- AWS EC2 and GitHub Actions for deployment

## Local setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   # Windows PowerShell
   .venv\Scripts\Activate.ps1
   # macOS/Linux
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements-dev.txt
   ```

3. Copy `.env.example` to `.env` and provide your own values:

   ```env
   MONGO_URI=mongodb+srv://USERNAME:PASSWORD@CLUSTER.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB_NAME=cs4800_database
   SECRET_KEY=replace-with-a-long-random-value
   PORT=5000
   ```

   `FLASK_SECRET_KEY` is still accepted as a backwards-compatible alternative to `SECRET_KEY`. Never commit `.env`.

4. Make sure MongoDB Atlas allows your current IP and the database user has `readWrite` access to `MONGO_DB_NAME`.

5. Start the development server:

   ```bash
   python app.py
   ```

6. Open <http://localhost:5000>.

MongoDB creates the four collections when data is first inserted. Availability documents from the original demo without a `user_id` remain untouched but are not shown in the new group workflow.

## Run tests

```bash
pytest -q
```

The tests cover arbitrary 45/75/125/180-minute durations, shorter alternatives, minimum attendance, the required three-person overlap, deletion cleanup, plan lifecycle categorization, automatic previews, group editing, scheduling, RSVP validation and updates, dashboard organization, and group-aware member creation.

## Acceptance demo

1. Open **Members** and add Alex, Sam, and Chris.
2. Open each member and save recurring weekly availability.
3. Create **Friday Friends** from **Groups**.
4. Add all three members to the group.
5. Confirm the group weekly matrix lists their windows by member and weekday.
6. Create a plan called **Movie Night** inside the group.
7. Choose an inclusive date range, a 120-minute duration, and minimum attendance of two or three.
8. Confirm matching times appear automatically and update in the live preview when plan requirements change.
9. Review the ranked results and attendance details, then schedule one result.
10. Submit RSVP responses for several members, including an arrival or departure time.
11. Confirm the plan moves from **Planning** to **Scheduled Plans** and appears under **Upcoming** on the dashboard.
12. Refresh or restart Flask and confirm the scheduled time and RSVP responses remain.

## Project structure

```text
app.py
services/
  scheduling_service.py
templates/
static/
tests/
requirements.txt
requirements-dev.txt
.env.example
.github/workflows/deploy.yml
```

## Production and EC2

Gunicorn keeps the existing entry point:

```bash
gunicorn --workers 2 --bind 0.0.0.0:5000 app:app
```

Create `.env` directly on EC2 and configure the `cs4800-availability` systemd service to run that command. The GitHub Actions workflow expects:

- `EC2_HOST`
- `EC2_USER`
- `EC2_SSH_KEY`
- `EC2_APP_PATH`

Pushes and pull requests to `main` run pytest. EC2 deployment is currently manual-only through GitHub Actions; a manual run waits for tests to pass, then installs requirements and restarts the existing systemd service.
