"""Google OIDC callback regressions that run Authlib's real ID-token verification.

Only the provider's discovery document, JWKS and token endpoint are replaced with
synthetic, in-process values signed by ephemeral keys. The real `/auth/google`
redirect, `/auth/google/callback`, state handling, Authlib signature/claim checks,
and the app's own claim checks all run unchanged. Login and CSRF stay enabled and
outbound networking is blocked.
"""
import base64
import copy
import json
import socket
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from tests.security.conftest import seeded_app

pytestmark = pytest.mark.security

CLIENT_ID = "synthetic-oidc-client"
ISSUER = "https://accounts.google.com"
SUBJECT = "synthetic-oidc-sub"
KEY_ID = "synthetic-key"

TOKEN_REJECTED = "Google sign-in failed. Please try again."
CLAIMS_REJECTED = "Google did not provide a verified email address."
# Case -> the callback message that proves which layer rejected it.
REJECTED = {
    "wrong_signature": TOKEN_REJECTED,
    "unknown_key": TOKEN_REJECTED,
    "unsigned": TOKEN_REJECTED,
    "wrong_algorithm": TOKEN_REJECTED,
    "wrong_issuer": TOKEN_REJECTED,
    "wrong_audience": TOKEN_REJECTED,
    "expired": TOKEN_REJECTED,
    "future_iat": TOKEN_REJECTED,
    "future_nbf": TOKEN_REJECTED,
    "wrong_nonce": TOKEN_REJECTED,
    "missing_nonce": TOKEN_REJECTED,
    "nonce_supported_false": TOKEN_REJECTED,
    "missing_sub": TOKEN_REJECTED,
    "unverified_email": CLAIMS_REJECTED,
}


@pytest.fixture(scope="module")
def keys():
    # Same kid on both keys, so "wrong_signature" reaches signature verification.
    trusted = RSAKey.generate_key(2048, parameters={"kid": KEY_ID})
    attacker = RSAKey.generate_key(2048, parameters={"kid": KEY_ID})
    return trusted, attacker


@pytest.fixture
def no_network(monkeypatch):
    # The callback catches all exceptions, so attempts are recorded and checked
    # at teardown instead of relying on the raised error surfacing.
    attempts = []

    def blocked(*args, **_kwargs):
        attempts.append(args[1:2])
        raise AssertionError("Unexpected outbound network access in OIDC test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    yield
    assert attempts == []


@pytest.fixture
def oidc(monkeypatch, keys, no_network):
    for name, value in {
        "GOOGLE_CLIENT_ID": CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": "synthetic-oidc-secret",
        "GOOGLE_REDIRECT_URI": "http://localhost/auth/google/callback",
        "APP_BASE_URL": "http://localhost",
        "SESSION_COOKIE_SECURE": "false",
        "APP_ENV": "",
        "VERCEL": "",
    }.items():
        monkeypatch.setenv(name, value)
    app, db, ids, password = seeded_app()
    monkeypatch.setattr(app.logger, "disabled", True)
    google = app.extensions["google_oauth_client"]
    jwks = {"keys": [keys[0].as_dict(private=False)]}
    metadata = {
        "issuer": ISSUER,
        "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_endpoint": "https://oauth2.googleapis.com/token",
        "id_token_signing_alg_values_supported": ["RS256"],
        "jwks": jwks,
    }
    # Trusted discovery and JWKS substitutes; Authlib still verifies against them.
    monkeypatch.setattr(google, "load_server_metadata", lambda: metadata)
    monkeypatch.setattr(google, "fetch_jwk_set", lambda force=False: jwks)
    token_calls = []

    def use_id_token(encoded):
        def fetch_access_token(**_kwargs):
            token_calls.append(encoded)
            return {"id_token": encoded, "access_token": "synthetic-not-live", "token_type": "Bearer"}

        monkeypatch.setattr(google, "fetch_access_token", fetch_access_token)

    return app, db, use_id_token, token_calls


def begin_login(client):
    response = client.get("/auth/google")
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["Location"]).query)
    return query["state"][0], query["nonce"][0]


def b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def build_id_token(case, nonce, keys):
    trusted, attacker = keys
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": SUBJECT,
        "email": "synthetic-oidc@example.invalid",
        "email_verified": True,
        "iat": now,
        "exp": now + 600,
        "nonce": nonce,
        "name": "Synthetic OIDC user",
    }
    header, key = {"alg": "RS256", "kid": KEY_ID}, trusted
    if case == "wrong_signature":
        key = attacker
    elif case == "unknown_key":
        header["kid"] = "unknown-synthetic-key"
    elif case == "wrong_issuer":
        claims["iss"] = "https://attacker.invalid"
    elif case == "wrong_audience":
        claims["aud"] = "different-client"
    elif case == "expired":
        claims["exp"] = now - 3600
    elif case == "future_iat":
        claims["iat"] = now + 3600
    elif case == "future_nbf":
        claims["nbf"] = now + 3600
    elif case == "wrong_nonce":
        claims["nonce"] = "incorrect-nonce"
    elif case == "nonce_supported_false":
        # Regression guard: a token-supplied claim must not disable the nonce check.
        claims["nonce"] = "incorrect-nonce"
        claims["nonce_supported"] = False
    elif case == "missing_nonce":
        claims.pop("nonce")
    elif case == "missing_sub":
        claims.pop("sub")
    elif case == "unverified_email":
        claims["email_verified"] = False
    elif case == "email_collision":
        claims["email"] = "owner@example.invalid"  # seeded password account
    elif case == "wrong_algorithm":
        # HS256 keyed with the public RSA key (algorithm-confusion attempt).
        header = {"alg": "HS256", "kid": KEY_ID}
        key = OctKey.import_key(json.dumps(trusted.as_dict(private=False)).encode())
    if case == "unsigned":
        return f"{b64({'alg': 'none'})}.{b64(claims)}."
    return jwt.encode(header, claims, key)


def run_callback(client, state, code="synthetic-authorization-code"):
    response = client.get("/auth/google/callback", query_string={"state": state, "code": code})
    assert response.status_code == 302
    with client.session_transaction() as session_state:
        messages = [message for _category, message in session_state.pop("_flashes", [])]
        return "_user_id" in session_state, dict(session_state), messages


def snapshot(db):
    return copy.deepcopy(list(db.accounts.find({})))


def test_valid_id_token_signs_in_through_real_verification(oidc, keys):
    app, db, use_id_token, token_calls = oidc
    client = app.test_client()
    state, nonce = begin_login(client)
    use_id_token(build_id_token("valid", nonce, keys))
    authenticated, _, _ = run_callback(client, state)
    assert authenticated
    assert len(token_calls) == 1
    account = db.accounts.find_one({"google_sub": SUBJECT})
    assert account is not None and account["auth_methods"] == ["google"]


@pytest.mark.parametrize("case", REJECTED)
def test_invalid_id_token_is_rejected_without_account_changes(oidc, keys, case):
    app, db, use_id_token, token_calls = oidc
    client = app.test_client()
    state, nonce = begin_login(client)
    use_id_token(build_id_token(case, nonce, keys))
    before = snapshot(db)
    authenticated, session_state, messages = run_callback(client, state)
    assert len(token_calls) == 1  # reached the real ID-token verification
    assert messages == [REJECTED[case]]
    assert not authenticated
    assert "pending_google_link_token" not in session_state
    assert db.accounts.find_one({"google_sub": SUBJECT}) is None
    assert snapshot(db) == before


def test_verified_email_matching_password_account_requires_link_proof(oidc, keys):
    app, db, use_id_token, _ = oidc
    client = app.test_client()
    state, nonce = begin_login(client)
    use_id_token(build_id_token("email_collision", nonce, keys))
    before = snapshot(db)
    authenticated, session_state, messages = run_callback(client, state)
    assert messages == ["A Group Planner account already uses this email. Sign in to connect Google."]
    assert not authenticated
    assert session_state.get("pending_google_link_token")
    assert db.accounts.find_one({"google_sub": SUBJECT}) is None
    assert snapshot(db) == before


def test_replayed_callback_state_is_rejected_before_token_exchange(oidc, keys):
    app, db, use_id_token, token_calls = oidc
    client = app.test_client()
    state, nonce = begin_login(client)
    use_id_token(build_id_token("valid", nonce, keys))
    authenticated, _, _ = run_callback(client, state)
    assert authenticated and len(token_calls) == 1
    saved = snapshot(db)
    # Session layer: the browser's expected state was consumed.
    _, _, messages = run_callback(client, state, code="replayed-synthetic-code")
    assert messages == ["Google sign-in could not be verified. Please try again."]
    # Server layer: even with the session value restored, the stored state is single-use.
    with client.session_transaction() as session_state:
        session_state["google_oauth_state"] = state
    _, _, messages = run_callback(client, state, code="replayed-synthetic-code")
    assert messages == ["Google sign-in has expired. Please try again."]
    assert len(token_calls) == 1
    assert snapshot(db) == saved
