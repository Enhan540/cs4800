"""Bounded retry of security alerts; credentials come only from the environment."""

import argparse
import logging
import os
from pathlib import Path
import sys
from urllib.parse import parse_qsl, urlsplit

from flask import Flask
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.security_alert_service import deliver_due


def configuration():
    base = os.environ.get("APP_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("HTTPS application origin required")
    uri = os.environ.get("MONGO_URI", "")
    mongo = urlsplit(uri)
    options = [(key.casefold(), value.casefold()) for key, value in parse_qsl(mongo.query)]
    if mongo.scheme not in {"mongodb", "mongodb+srv"} or not mongo.netloc:
        raise ValueError("Database configuration required")
    if mongo.scheme == "mongodb" and ("tls", "true") not in options:
        raise ValueError("Database TLS required")
    for key, value in options:
        if (key in {"tls", "ssl"} and value != "true") or (key in {"tlsinsecure", "tlsallowinvalidcertificates", "tlsallowinvalidhostnames"} and value != "false"):
            raise ValueError("Verified database TLS required")
    values = {name: os.environ.get(name, "") for name in ("MONGO_DB_NAME", "RESEND_API_KEY", "FROM_EMAIL")}
    if not all(values.values()):
        raise ValueError("Delivery configuration incomplete")
    return uri, values.pop("MONGO_DB_NAME"), {**values, "APP_BASE_URL": base, "EMAIL_DELIVERY_MODE": "resend"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Retry queued security alerts without importing the application.")
    parser.add_argument("--limit", type=int, default=50, choices=range(1, 51), metavar="1..50")
    args = parser.parse_args(argv)
    try:
        uri, database_name, config = configuration()
        app = Flask("security-alert-dispatcher")
        app.config.update(config)
        app.logger.setLevel(logging.WARNING)
        with MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000, maxPoolSize=3, tz_aware=True) as client:
            client.admin.command("ping")
            counts = deliver_due(app, client[database_name], limit=args.limit)
        print(f"Security alert retry: sent={counts['sent']}, deferred={counts['deferred']}.")
        return int(counts["deferred"] > 0)
    except (ValueError, PyMongoError, requests.RequestException):
        # Provider/database exception text may contain credentials or payloads.
        print("Security alert retry incomplete; review configuration and delivery privately.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
