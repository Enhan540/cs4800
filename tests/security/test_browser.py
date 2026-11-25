import os
import json
import threading

import pytest
from werkzeug.serving import make_server

pytestmark = [pytest.mark.security, pytest.mark.browser]


@pytest.fixture
def live_browser(secured):
    if os.environ.get("RUN_SECURITY_BROWSER") != "1":
        pytest.skip("Run through security_check.py for required browser validation")
    from playwright.sync_api import sync_playwright
    app, db, ids, password = secured
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                yield page, base, db, ids, password
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def sign_in(page, base, name, password):
    page.goto(base + "/login")
    page.locator('[name="username"]').fill("audit-" + name)
    page.locator('[name="password"]').fill(password)
    page.locator('button[type="submit"]').first.click()
    page.wait_for_url("**/dashboard")


def test_browser_sign_in_csrf_and_cross_user_isolation(live_browser):
    page, base, db, ids, password = live_browser
    sign_in(page, base, "outsider", password)
    response = page.goto(base + f"/groups/{ids['group']}")
    assert response.status == 404
    assert "Private audit group" not in page.content()
    # Use the page's fetch wrapper and real session cookie, rather than bypassing the UI.
    result = page.evaluate("""async (url) => { const response = await fetch(url, {method: 'POST'}); return response.status; }""", base + f"/availability/{ids['window']}/delete")
    assert result in (400, 404)
    assert db.availability.find_one({"_id": ids["window"]})


def test_browser_invitation_search_treats_response_values_as_text(live_browser):
    page, base, db, ids, password = live_browser
    sign_in(page, base, "owner", password)
    hostile = '<img data-security-probe src=x onerror="alert(1)">"<b>name</b>'
    page.route("**/invitations/search**", lambda route: route.fulfill(content_type="application/json", body=json.dumps({"results": [{"id": str(ids["outsider"]), "username": hostile, "state": "available"}]})))
    page.goto(base + f"/groups/{ids['group']}")
    page.locator('[data-dialog-open="invite-dialog"]').click()
    page.locator("input[data-invite-search]").fill("audit")
    row = page.locator(".invite-search-result").first
    row.wait_for()
    assert row.locator("span").inner_text() == hostile
    assert row.locator("button").get_attribute("data-invite-username") == hostile
    assert page.locator("[data-security-probe]").count() == 0
    assert row.locator("img, b").count() == 0


def test_browser_optional_authenticator_setup_and_recovery_sign_in(live_browser, monkeypatch):
    import time
    import pyotp
    from services import mfa_service

    page, base, db, ids, password = live_browser
    when = time.time()
    monkeypatch.setattr(mfa_service, "otp_timestamp", lambda: when)
    sign_in(page, base, "owner", password)
    requested = []
    page.on("request", lambda request: requested.append(request.url))
    page.goto(base + "/account/mfa")
    page.get_by_label("Current password").fill(password)
    page.get_by_role("button", name="Set up an authenticator").click()
    seed = page.locator("[data-sensitive-key]").inner_text()
    page.locator("[data-sensitive-qr]").wait_for(state="visible")
    page.get_by_label("Six-digit authenticator code").fill(pyotp.TOTP(seed).at(when))
    page.get_by_role("button", name="Turn on verification").click()
    codes = page.locator("[data-mfa-recovery]").all_inner_texts()
    assert len(codes) == 10
    assert all(url.startswith(base + "/") for url in requested)
    assert all("/_vercel/insights" not in url for url in requested)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("link", name="I have saved my codes").click()
    assert page.locator("[data-mfa-recovery]").count() == 0
    # A new browser must finish the second step before any account page opens.
    other = page.context.browser.new_context()
    try:
        actor = other.new_page()
        actor.goto(base + "/login")
        actor.get_by_label("Username", exact=True).fill("audit-owner")
        actor.locator('[name="password"]').fill(password)
        actor.locator('button[type="submit"]').first.click()
        actor.wait_for_url("**/auth/mfa")
        actor.goto(base + "/account")
        assert actor.url.endswith("/login?next=%2Faccount") or "/login?" in actor.url
        actor.goto(base + "/auth/mfa")
        actor.get_by_label("Verification code").fill(codes[0])
        actor.get_by_role("button", name="Verify and sign in").click()
        actor.wait_for_url("**/dashboard")
        assert actor.goto(base + "/account").status == 200
    finally:
        other.close()
