"""Small transactional-email abstraction for Flask and serverless deployment."""

import os

import requests
from flask import has_request_context, request


def _base_url(app):
    configured = app.config.get("APP_BASE_URL") or os.getenv("APP_BASE_URL")
    if configured:
        return configured.rstrip("/")
    if has_request_context():
        return request.url_root.rstrip("/")
    return ""


def send_email(app, *, recipient, subject, text, idempotency_key=None):
    mode = (app.config.get("EMAIL_DELIVERY_MODE") or os.getenv("EMAIL_DELIVERY_MODE", "console")).casefold()
    if mode == "console":
        app.logger.info("Development email subject=%s recipient=%s body=%s", subject, recipient, text)
        return True
    if mode != "resend":
        app.logger.error("Unsupported email delivery mode")
        return False
    api_key = app.config.get("RESEND_API_KEY") or os.getenv("RESEND_API_KEY")
    from_email = app.config.get("FROM_EMAIL") or os.getenv("FROM_EMAIL")
    if not api_key or not from_email:
        app.logger.error("Email provider is not configured")
        return False
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    response = requests.post(
        "https://api.resend.com/emails",
        headers=headers,
        json={"from": from_email, "to": [recipient], "subject": subject, "text": text},
        timeout=(3.05, 15),
    )
    if not response.ok:
        app.logger.error("Email provider request failed with status=%s", response.status_code)
        return False
    return True


def send_security_change_email(app, *, recipient, message, changed_at, idempotency_key):
    return send_email(app, recipient=recipient, subject="Group Planner security update", text=f"{message}.\n\nTime: {changed_at}\n\nIf you did not make this change, open Group Planner using your usual address and review your account.\n\n{_base_url(app)}/account\n\nThis message contains no password, verification code, or sign-in link.", idempotency_key=idempotency_key)


def send_verification_email(app, *, recipient, token):
    url = f"{_base_url(app)}/verify-email/{token}"
    return send_email(
        app,
        recipient=recipient,
        subject="Verify your Group Planner email",
        text=f"Verify your Group Planner email by opening this link:\n\n{url}\n\nThis link expires soon.",
    )


def send_password_reset_email(app, *, recipient, token):
    url = f"{_base_url(app)}/reset-password/{token}"
    return send_email(
        app,
        recipient=recipient,
        subject="Reset your Group Planner password",
        text=f"A password reset was requested for your Group Planner account.\n\nReset your password here:\n{url}\n\nIf you did not request this, you can ignore this email.",
    )
