"""Request protections shared by local and serverless deployments."""

import ipaddress
import hashlib
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

from flask import abort, g, request, session
from flask_login import current_user, user_logged_in
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from services.password_policy import legacy_password_setup_pending, legacy_setup_proof


def recently_authenticated():
    proved_at = session.get("reauthenticated_at")
    return isinstance(proved_at, (int, float)) and 0 <= time.time() - proved_at <= 300


def session_digest():
    value = session.get("security_session_id")
    return hashlib.sha256(value.encode()).hexdigest() if isinstance(value, str) else None


def session_active(database, account_id):
    digest = session_digest()
    if not digest:
        return False
    now = datetime.now(timezone.utc)
    query = {"_id": digest, "account_id": account_id, "expires_at": {"$gt": now}, "last_seen_at": {"$gt": now - timedelta(minutes=30)}}
    # Static assets and background notification polls do not extend activity.
    if request.endpoint == "static" or request.path.startswith("/api/notifications"):
        return database.security_sessions.find_one(query) is not None
    return database.security_sessions.find_one_and_update(query, {"$set": {"last_seen_at": now}}) is not None


def revoke_session(database, account_id):
    digest = session_digest()
    if digest:
        database.security_sessions.delete_one({"_id": digest, "account_id": account_id})


def client_address(app):
    """The visitor's address for rate limiting; never stored."""
    if app.config.get("TRUST_PLATFORM_CLIENT_IP"):
        candidates = [request.headers.get("X-Real-IP", "")]
        candidates += [part for part in request.headers.get("X-Forwarded-For", "").split(",")[:1]]
        for value in candidates:
            try:
                return str(ipaddress.ip_address(value.strip()))
            except ValueError:
                continue
    return request.remote_addr or "unknown"


def configure_security(app):
    production = bool(os.getenv("VERCEL")) or os.getenv("APP_ENV") == "production"
    app.config.update(
        CSRF_ENABLED=True,
        SECURITY_PRODUCTION=production,
        MAX_CONTENT_LENGTH=512 * 1024,
        MAX_FORM_MEMORY_SIZE=64 * 1024,
        MAX_FORM_PARTS=200,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        # On Vercel the app only ever sees 127.0.0.1; the visitor's address arrives in
        # headers that Vercel's edge sets and overwrites, so clients can't spoof them.
        TRUST_PLATFORM_CLIENT_IP=bool(os.getenv("VERCEL")),
    )
    if production:
        secret = app.config.get("SECRET_KEY", "")
        base = urlsplit(app.config.get("APP_BASE_URL", ""))
        if len(secret) < 32 or "replace-with" in secret or secret == "dev-only-change-me":
            raise RuntimeError("Production requires a random SECRET_KEY of at least 32 characters")
        if base.scheme != "https" or not base.hostname or base.username or base.password:
            raise RuntimeError("Production requires an HTTPS APP_BASE_URL")
        if not app.config.get("SESSION_COOKIE_SECURE"):
            raise RuntimeError("Production requires SESSION_COOKIE_SECURE=true")
        mongo_uri = os.getenv("MONGO_URI", "")
        parsed_mongo = urlsplit(mongo_uri)
        tls_options = {key.casefold(): value.casefold() for key, value in parse_qsl(parsed_mongo.query)}
        if parsed_mongo.scheme not in {"mongodb", "mongodb+srv"} or (parsed_mongo.scheme != "mongodb+srv" and tls_options.get("tls") != "true"):
            raise RuntimeError("Production MongoDB requires verified TLS")
        if tls_options.get("tls") == "false" or tls_options.get("ssl") == "false" or any(tls_options.get(key) == "true" for key in ("tlsallowinvalidcertificates", "tlsallowinvalidhostnames", "tlsinsecure")):
            raise RuntimeError("Production MongoDB must verify certificates")
        if app.config.get("EMAIL_DELIVERY_MODE") != "resend":
            raise RuntimeError("Production email delivery must not log recovery links")
        app.config["TRUSTED_HOSTS"] = [base.hostname]
        app.config.update(SESSION_COOKIE_NAME="__Host-group_planner", SESSION_COOKIE_PATH="/", SESSION_COOKIE_DOMAIN=None)
        for name in ("GOOGLE_REDIRECT_URI", "GOOGLE_CALENDAR_REDIRECT_URI"):
            callback = urlsplit(app.config.get(name, ""))
            if callback.scheme != "https" or callback.hostname != base.hostname:
                raise RuntimeError("Production OAuth callbacks must match APP_BASE_URL")


def init_security(app, database):
    production = app.config["SECURITY_PRODUCTION"]

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @user_logged_in.connect_via(app)
    def bind_session(_sender, user):
        revoke_session(database, user.document["_id"])
        session["security_session_id"] = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        database.security_sessions.insert_one({"_id": session_digest(), "account_id": user.document["_id"], "created_at": now, "last_seen_at": now, "expires_at": now + timedelta(hours=12)})
        session["auth_version"] = user.document.get("auth_version", 0)
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        session["reauthenticated_at"] = time.time()
        session.pop("mfa_verified_epoch", None)
        session.pop("mfa_verified_at", None)
        if legacy_password_setup_pending(user.document):
            session["legacy_setup_proof"] = legacy_setup_proof(user.document)
        else:
            session.pop("legacy_setup_proof", None)

    def throttle(key, limit, seconds):
        if database is None:
            abort(503)
        now = datetime.now(timezone.utc)
        bucket = int(time.time()) // seconds
        digest = hashlib.sha256(key.encode()).hexdigest()
        try:
            record = database.security_rate_limits.find_one_and_update(
                {"_id": f"{digest}:{bucket}"},
                {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": now + timedelta(seconds=seconds * 2)}},
                upsert=True, return_document=ReturnDocument.AFTER,
            )
        except PyMongoError:
            abort(503)
        if record["count"] > limit:
            abort(429)

    @app.before_request
    def protect_request():
        g.csp_nonce = secrets.token_urlsafe(18)
        if production and (app.debug or app.config.get("LOGIN_DISABLED") or not app.config["CSRF_ENABLED"]):
            abort(503)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if app.config["CSRF_ENABLED"]:
                expected = session.get("csrf_token", "")
                supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
                if not expected or not isinstance(supplied, str) or not secrets.compare_digest(expected, supplied):
                    abort(400, description="Refresh the page and try again.")
                origin = request.headers.get("Origin")
                expected_origin = app.config["APP_BASE_URL"].rstrip("/") if production else request.host_url.rstrip("/")
                # Privacy settings can serialize a legitimate form Origin as null;
                # the unpredictable session-bound token is always required.
                if origin and origin != "null" and origin != expected_origin:
                    abort(400)
            for field in ("username",):
                if len(request.form.get(field, "")) > 40:
                    abort(400)
            for field in ("password", "new_password", "current_password", "confirm_password"):
                if len(request.form.get(field, "")) > 1024:
                    abort(400)
            if len(request.form.get("email", "")) > 254:
                abort(400)
            if len(request.form.get("mfa_code", "")) > 80:
                abort(400)
        if len(request.args.get("q", "")) > 40:
            abort(400)
        if request.path in {"/api/notifications/mark-read", "/api/notifications/delete"}:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
                abort(400)
            if len(payload["ids"]) > 100 or any(not isinstance(value, str) for value in payload["ids"]):
                abort(400)
        # Test mode skips quotas only; security tests explicitly exercise quotas.
        if not app.testing or app.config.get("SECURITY_TEST_RATE_LIMITS"):
            if request.method == "POST" and request.endpoint == "mfa_challenge":
                throttle(f"auth-ip:{client_address(app)}", 120, 900)
            if request.method == "POST" and request.endpoint in {"login", "register", "forgot_password"}:
                throttle(f"auth-ip:{client_address(app)}", 120, 900)
                identity = request.form.get("username") or request.form.get("email") or ""
                throttle(f"auth-identity:{request.endpoint}:{identity.strip().casefold()}", 20, 900)
            if request.method == "POST" and request.endpoint == "feedback":
                throttle(f"feedback-ip:{client_address(app)}", 5, 3600)
                if current_user.is_authenticated:
                    throttle(f"feedback-account:{current_user.id}", 20, 86400)
            if current_user.is_authenticated and request.method == "POST" and request.endpoint == "request_google_calendar_access":
                throttle(f"calendar-access-request:{current_user.id}", 3, 86400)
            if current_user.is_authenticated and request.method == "POST" and request.endpoint in {"create_managed_member", "rename_managed_member"}:
                throttle(f"managed-members:{current_user.id}", 60, 900)
            if current_user.is_authenticated and request.method == "POST" and request.endpoint in {"create_demo_group", "reset_demo_group"}:
                throttle(f"demo:{current_user.id}", 10, 900)
            if current_user.is_authenticated and request.endpoint in {"schedule_preview", "find_plan_times", "sync_calendar", "create_plan", "edit_plan"}:
                throttle(f"expensive:{current_user.id}", 30, 60)
            if current_user.is_authenticated and request.method == "POST" and request.endpoint in {"change_username", "change_password", "set_password", "update_account_email", "disconnect_google_identity", "mfa_setup", "mfa_enable", "mfa_disable", "mfa_regenerate_recovery", "delete_account"}:
                throttle(f"account-change:{current_user.id}", 20, 900)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if (request.endpoint or "").startswith("mfa_") or request.endpoint == "reset_password":
            response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace(" https://fonts.googleapis.com", "").replace(" https://fonts.gstatic.com", "")
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        if production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    if database is not None:
        database.security_rate_limits.create_index("expires_at", expireAfterSeconds=0)
