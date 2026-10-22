import os

import mongomock
import pytest
from flask import jsonify
from werkzeug.security import check_password_hash

os.environ["MONGO_URI"] = ""
from app import create_app


@pytest.fixture
def auth_app_and_db():
    database = mongomock.MongoClient().auth_test
    app = create_app(database_override=database)
    app.config.update(
        TESTING=True,
        SECRET_KEY="auth-test-secret",
        LOGIN_DISABLED=False,
        GOOGLE_CLIENT_ID="",
        GOOGLE_CLIENT_SECRET="",
        GOOGLE_REDIRECT_URI="",
    )
    return app, database


def register(client, username="testuser", password="correct-horse"):
    return client.post(
        "/register",
        data={
            "username": username,
            "password": password,
            "confirm_password": password,
        },
    )


def test_registration_hashes_password_and_starts_empty(auth_app_and_db):
    app, database = auth_app_and_db
    client = app.test_client()

    response = register(client)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    account = database.accounts.find_one({"normalized_username": "testuser"})
    assert account
    assert "plaintext_password" not in account
    assert account["password_hash"] != "correct-horse"
    assert check_password_hash(account["password_hash"], "correct-horse")
    assert database.groups.count_documents({"account_ids": account["_id"]}) == 0
    assert database.plans.count_documents({}) == 0
    assert database.availability.count_documents({}) == 0


def test_account_links_to_dedicated_availability_page(auth_app_and_db):
    app, database = auth_app_and_db
    client = app.test_client()
    register(client, username="availability-user")
    account = database.accounts.find_one({"normalized_username": "availability-user"})
    database.users.insert_one({
        "display_name": "Availability User",
        "normalized_name": "availability user",
        "linked_account_id": account["_id"],
        "account_id": account["_id"],
    })

    page = client.get("/account").get_data(as_text=True)
    assert "data-account-availability" not in page
    assert "Current Availability" not in page
    assert 'href="/account/availability"' in page

    availability = client.get("/account/availability")
    assert availability.status_code == 302
    assert "/users/" in availability.headers["Location"]


def test_availability_edit_authorization_distinguishes_owner_admin_and_member(auth_app_and_db):
    app, database = auth_app_and_db
    client = app.test_client()
    register(client, username="owner-user")
    owner = database.accounts.find_one({"normalized_username": "owner-user"})
    client.post("/groups/create", data={"name": "Permission Group", "description": ""})
    group = database.groups.find_one({"name": "Permission Group"})
    owner_member = database.users.find_one({"linked_account_id": owner["_id"]})

    client.post("/logout")
    register(client, username="member-user")
    member_account = database.accounts.find_one({"normalized_username": "member-user"})
    member_id = database.users.insert_one({
        "display_name": "Member User",
        "normalized_name": "member user",
        "account_id": member_account["_id"],
        "linked_account_id": member_account["_id"],
    }).inserted_id
    member = database.users.find_one({"_id": member_id})
    database.groups.update_one(
        {"_id": group["_id"]},
        {"$addToSet": {"member_ids": member["_id"], "account_ids": member_account["_id"]},
         "$set": {f"account_roles.{member_account['_id']}": "member"}},
    )

    assert client.get(f"/users/{owner_member['_id']}/availability").status_code == 403
    assert client.get(f"/users/{member['_id']}/availability").status_code == 200

    database.groups.update_one(
        {"_id": group["_id"]},
        {"$set": {
            f"account_roles.{member_account['_id']}": "admin",
            f"admin_permissions.{member_account['_id']}": ["edit_member_availability"],
        }},
    )
    assert client.get(f"/users/{owner_member['_id']}/availability").status_code == 200

    database.groups.update_one(
        {"_id": group["_id"]},
        {"$set": {f"admin_permissions.{member_account['_id']}": []}},
    )
    assert client.get(f"/users/{owner_member['_id']}/availability").status_code == 403


def test_registration_validation_and_duplicate_username(auth_app_and_db):
    app, _database = auth_app_and_db
    client = app.test_client()
    assert register(client).status_code == 302
    client.post("/logout")

    duplicate = register(client, username="TESTUSER")
    mismatch = client.post(
        "/register",
        data={"username": "another", "password": "long-enough", "confirm_password": "different"},
    )

    assert duplicate.status_code == 400
    assert b"Username is already taken." in duplicate.data
    assert mismatch.status_code == 400
    assert b"Passwords do not match." in mismatch.data


def test_password_login_failure_success_and_logout(auth_app_and_db):
    app, _database = auth_app_and_db
    client = app.test_client()
    register(client)
    client.post("/logout")

    failure = client.post("/login", data={"username": "testuser", "password": "wrong"})
    success = client.post("/login", data={"username": "testuser", "password": "correct-horse"})
    dashboard = client.get("/dashboard")
    logout = client.post("/logout")

    assert failure.status_code == 401
    assert b"Invalid username or password." in failure.data
    assert success.status_code == 302
    assert dashboard.status_code == 200
    assert b"Welcome, testuser" in dashboard.data
    assert logout.status_code == 302
    assert client.get("/dashboard").status_code == 302


@pytest.mark.parametrize(
    "path",
    ["/dashboard", "/groups", "/groups/create", "/users", "/members", "/account"],
)
def test_private_pages_redirect_anonymous_users(auth_app_and_db, path):
    app, _database = auth_app_and_db
    response = app.test_client().get(path)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_safe_next_redirect(auth_app_and_db):
    app, _database = auth_app_and_db
    client = app.test_client()
    register(client)
    client.post("/logout")

    safe = client.post(
        "/login?next=/groups",
        data={"username": "testuser", "password": "correct-horse", "next": "/groups"},
    )
    client.post("/logout")
    unsafe = client.post(
        "/login?next=https://example.com",
        data={
            "username": "testuser",
            "password": "correct-horse",
            "next": "https://example.com",
        },
    )

    assert safe.headers["Location"].endswith("/groups")
    assert unsafe.headers["Location"].endswith("/dashboard")


class FakeGoogleClient:
    def __init__(self, profile=None, token_error=None):
        self.profile = profile or {
            "sub": "google-user-123",
            "email": "person@example.com",
            "email_verified": True,
            "name": "Example Person",
        }
        self.token_error = token_error
        self.redirect_args = None
        self.token_calls = 0

    def authorize_redirect(self, redirect_uri, state):
        self.redirect_args = (redirect_uri, state)
        return jsonify({"redirect_uri": redirect_uri, "state": state}), 302

    def authorize_access_token(self):
        self.token_calls += 1
        if self.token_error:
            raise self.token_error
        return {"userinfo": self.profile, "access_token": "must-not-be-stored"}


def configured_google_client(app, profile=None, token_error=None):
    fake = FakeGoogleClient(profile=profile, token_error=token_error)
    app.config.update(
        GOOGLE_CLIENT_ID="test-client-id",
        GOOGLE_CLIENT_SECRET="test-client-secret",
        GOOGLE_REDIRECT_URI="http://localhost/auth/google/callback",
    )
    app.extensions["google_oauth_client"] = fake
    return fake


def start_google_login(client):
    response = client.get("/auth/google")
    with client.session_transaction() as oauth_session:
        state = oauth_session["google_oauth_state"]
    return response, state


def test_google_login_requires_configuration(auth_app_and_db):
    app, _database = auth_app_and_db
    response = app.test_client().get("/auth/google", follow_redirects=True)
    assert response.status_code == 200
    assert b"Google sign-in is not configured" in response.data


def test_google_login_generates_state_and_redirects(auth_app_and_db):
    app, _database = auth_app_and_db
    fake = configured_google_client(app)
    response, state = start_google_login(app.test_client())

    assert response.status_code == 302
    assert len(state) >= 32
    assert fake.redirect_args == ("http://localhost/auth/google/callback", state)


def test_google_callback_rejects_state_mismatch_before_token_exchange(auth_app_and_db):
    app, database = auth_app_and_db
    fake = configured_google_client(app)
    client = app.test_client()
    start_google_login(client)

    response = client.get("/auth/google/callback?state=wrong", follow_redirects=True)

    assert response.status_code == 200
    assert b"could not be verified" in response.data
    assert fake.token_calls == 0
    assert database.accounts.count_documents({}) == 0


def test_google_callback_creates_empty_account_without_storing_token(auth_app_and_db):
    app, database = auth_app_and_db
    configured_google_client(app)
    client = app.test_client()
    _response, state = start_google_login(client)

    response = client.get(f"/auth/google/callback?state={state}")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    account = database.accounts.find_one({"google_sub": "google-user-123"})
    assert account["google_email"] == "person@example.com"
    assert account["auth_methods"] == ["google"]
    assert "access_token" not in account
    assert "refresh_token" not in account
    assert database.groups.count_documents({"account_ids": account["_id"]}) == 0
    assert database.users.count_documents({"account_id": account["_id"]}) == 0


def test_google_callback_reuses_subject_and_does_not_duplicate_account(auth_app_and_db):
    app, database = auth_app_and_db
    configured_google_client(app)
    client = app.test_client()

    _response, state = start_google_login(client)
    client.get(f"/auth/google/callback?state={state}")
    client.post("/logout")
    _response, state = start_google_login(client)
    client.get(f"/auth/google/callback?state={state}")

    assert database.accounts.count_documents({"google_sub": "google-user-123"}) == 1


def test_google_callback_handles_cancellation_and_provider_failure(auth_app_and_db):
    app, database = auth_app_and_db
    configured_google_client(app)
    client = app.test_client()
    _response, state = start_google_login(client)
    cancelled = client.get(
        f"/auth/google/callback?state={state}&error=access_denied", follow_redirects=True
    )

    failing = configured_google_client(app, token_error=RuntimeError("provider unavailable"))
    _response, state = start_google_login(client)
    failed = client.get(f"/auth/google/callback?state={state}", follow_redirects=True)

    assert b"cancelled or denied" in cancelled.data
    assert b"Google sign-in failed" in failed.data
    assert failing.token_calls == 1
    assert database.accounts.count_documents({}) == 0


def test_accounts_cannot_see_or_mutate_each_others_data(auth_app_and_db):
    app, database = auth_app_and_db
    client = app.test_client()

    register(client, username="account-a")
    client.post(
        "/groups/create",
        data={"name": "Account A Secret Group", "description": "Private"},
    )
    group = database.groups.find_one({"name": "Account A Secret Group"})
    client.post(
        "/users",
        data={
            "display_name": "Private Member",
            "email": "private@example.com",
            "group_id": str(group["_id"]),
        },
    )
    member = database.users.find_one({"display_name": "Private Member"})
    plan_id = database.plans.insert_one(
        {"group_id": group["_id"], "title": "Private Plan"}
    ).inserted_id
    client.post("/logout")

    register(client, username="account-b")
    dashboard = client.get("/dashboard")
    groups = client.get("/groups")
    members = client.get("/users")

    assert b"Account A Secret Group" not in dashboard.data
    assert b"Account A Secret Group" not in groups.data
    assert b"Private Member" not in members.data
    assert b"You aren't part of any groups yet." in dashboard.data
    assert client.get(f"/groups/{group['_id']}").status_code == 404
    assert client.post(f"/groups/{group['_id']}/delete").status_code == 404
    assert client.get(f"/users/{member['_id']}/availability").status_code == 404
    assert client.get(f"/plans/{plan_id}").status_code == 404
    assert database.groups.find_one({"_id": group["_id"]}) is not None


def test_google_email_collision_is_not_automatically_merged(auth_app_and_db):
    app, database = auth_app_and_db
    first_profile = {
        "sub": "first-google-subject",
        "email": "same@example.com",
        "email_verified": True,
        "name": "First Person",
    }
    configured_google_client(app, profile=first_profile)
    client = app.test_client()
    _response, state = start_google_login(client)
    client.get(f"/auth/google/callback?state={state}")
    client.post("/logout")

    second_profile = {**first_profile, "sub": "different-google-subject"}
    configured_google_client(app, profile=second_profile)
    _response, state = start_google_login(client)
    response = client.get(
        f"/auth/google/callback?state={state}", follow_redirects=True
    )

    assert b"already connected to another account" in response.data
    assert database.accounts.count_documents({"google_email": "same@example.com"}) == 1
