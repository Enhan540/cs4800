# CS4800 Availability App

A small Flask + MongoDB Atlas application for collecting and viewing participant availability.

## Run locally

1. Install Python 3.11+ and make sure MongoDB Atlas allows your IP address.
2. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   # macOS/Linux
   source .venv/bin/activate
   # Windows PowerShell
   .venv\Scripts\Activate.ps1
   ```

3. Install dependencies: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and set `MONGO_URI` to your MongoDB Atlas connection string. Set a random `FLASK_SECRET_KEY` too.
5. Start the development server: `python app.py`
6. Open http://localhost:5000.

The form validates required fields and ensures the end time is later than the start time. Submissions are stored in the configured MongoDB collection, and the results page sorts them by weekday and start time.

## Production serving

Gunicorn can serve the application with:

```bash
gunicorn --workers 2 --bind 0.0.0.0:5000 app:app
```

For EC2, place the repository in a directory, create a `.venv`, configure the same environment variables in the service environment, and create a systemd service named `cs4800-availability` that runs the Gunicorn command. The included GitHub Actions workflow expects these repository secrets:

- `EC2_HOST`: EC2 public DNS name or IP address
- `EC2_USER`: SSH user, such as `ubuntu`
- `EC2_SSH_KEY`: private SSH key for the deployment user
- `EC2_APP_PATH`: absolute path to the checked-out app on EC2

The workflow runs on pushes to `main`, pulls the latest code, installs requirements, and restarts the systemd service. It does not contain database or AWS credentials.

## Project structure

```text
app.py
requirements.txt
.env.example
.gitignore
templates/
static/
.github/workflows/deploy.yml
```
