"""Validate and format bug reports and suggestions. No request or database access here."""

import re

TYPES = {
    "bug": {"label": "Report a bug", "subject": "[Group Planner] Bug report",
            "details_prompt": "What did you expect, and what happened instead?"},
    "suggestion": {"label": "Suggest an idea", "subject": "[Group Planner] Suggestion",
                   "details_prompt": "What would you like Group Planner to do?"},
}
SUMMARY_MAX = 120
DETAILS_MAX = 4000
EMAIL_MAX = 254
USER_AGENT_MAX = 200
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


def clean_text(value, limit):
    """Trim, drop control characters except newlines and tabs, and enforce a limit."""
    text = str(value or "").replace("\r\n", "\n").strip()
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    return text if len(text) <= limit else None


def validate(form, *, account_email=None):
    """Return (values, errors). `values` never contains anything not shown to the person."""
    errors = []
    kind = form.get("type", "")
    if kind not in TYPES:
        errors.append("Choose whether this is a bug or a suggestion.")
    summary = clean_text(form.get("summary"), SUMMARY_MAX)
    if not summary:
        errors.append(f"Add a short summary of up to {SUMMARY_MAX} characters.")
    elif "\n" in summary:
        summary = " ".join(summary.split())
    details = clean_text(form.get("details"), DETAILS_MAX)
    if details is None:
        errors.append(f"Keep the details to {DETAILS_MAX} characters or fewer.")
    contact = None
    if form.get("use_account_email") == "yes" and account_email:
        contact = account_email
    else:
        contact = str(form.get("contact_email") or "").strip().casefold() or None
        if contact and (len(contact) > EMAIL_MAX or not _EMAIL.fullmatch(contact)):
            errors.append("Enter a valid email address, or leave it blank.")
    return {"type": kind, "summary": summary, "details": details or "", "contact_email": contact}, errors


def email_text(values, *, username=None, page=None, created_at=None):
    lines = [
        f"Type: {TYPES[values['type']]['label']}",
        f"From: {username or 'someone who was signed out'}",
        f"Reply to: {values['contact_email'] or 'no contact email given'}",
        f"Page: {page or 'unknown'}",
        f"Sent: {created_at.strftime('%Y-%m-%d %H:%M UTC') if created_at else 'unknown'}",
        "",
        f"Summary: {values['summary']}",
        "",
        values["details"] or "(no details)",
    ]
    return "\n".join(lines)
