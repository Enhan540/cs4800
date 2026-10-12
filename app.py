"""Small CS4800 availability collection application."""

import os
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError

load_dotenv()

DAY_ORDER = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}
VALID_DAYS = list(DAY_ORDER)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

    mongo_uri = os.getenv("MONGO_URI")
    database_name = os.getenv("MONGO_DB_NAME", "cs4800_availability")
    collection_name = os.getenv("MONGO_COLLECTION", "availability")
    collection = None

    if mongo_uri:
        try:
            client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
            collection = client[database_name][collection_name]
            collection.create_index([("day_order", ASCENDING), ("start_time", ASCENDING)])
        except PyMongoError:
            app.logger.exception("Unable to connect to MongoDB Atlas during startup")

    def availability_records():
        if collection is None:
            raise RuntimeError("MongoDB is not configured or is unavailable.")
        return list(collection.find({}, {"_id": 0}).sort([("day_order", ASCENDING), ("start_time", ASCENDING)]))

    @app.get("/")
    def index():
        return render_template("index.html", days=VALID_DAYS)

    @app.post("/submit")
    def submit():
        name = request.form.get("name", "").strip()
        day = request.form.get("day", "").strip()
        start_time = request.form.get("start_time", "").strip()
        end_time = request.form.get("end_time", "").strip()

        if not all([name, day, start_time, end_time]):
            flash("Please fill in every field.", "error")
            return render_template("index.html", days=VALID_DAYS, form=request.form), 400

        if day not in DAY_ORDER:
            flash("Please choose a valid day of the week.", "error")
            return render_template("index.html", days=VALID_DAYS, form=request.form), 400

        try:
            start = datetime.strptime(start_time, "%H:%M")
            end = datetime.strptime(end_time, "%H:%M")
        except ValueError:
            flash("Please enter valid start and end times.", "error")
            return render_template("index.html", days=VALID_DAYS, form=request.form), 400

        if end <= start:
            flash("End time must be later than start time.", "error")
            return render_template("index.html", days=VALID_DAYS, form=request.form), 400

        if collection is None:
            flash("The database is currently unavailable. Please try again later.", "error")
            return render_template("index.html", days=VALID_DAYS, form=request.form), 503

        try:
            collection.insert_one(
                {
                    "name": name,
                    "day": day,
                    "day_order": DAY_ORDER[day],
                    "start_time": start_time,
                    "end_time": end_time,
                    "created_at": datetime.utcnow(),
                }
            )
        except PyMongoError:
            app.logger.exception("Could not save availability submission")
            flash("We could not save your availability. Please try again.", "error")
            return render_template("index.html", days=VALID_DAYS, form=request.form), 503

        flash("Availability submitted successfully.", "success")
        return redirect(url_for("results"))

    @app.get("/results")
    def results():
        try:
            entries = availability_records()
        except (PyMongoError, RuntimeError):
            app.logger.exception("Could not load availability submissions")
            flash("We could not load availability right now. Please try again later.", "error")
            return render_template("results.html", entries=[]), 503
        return render_template("results.html", entries=entries)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
