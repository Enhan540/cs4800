import copy

import pytest

from tests.security.conftest import login
from tests.security.test_authorization import OBJECT_ROUTES, PROTECTED, path_for, snapshot

pytestmark = pytest.mark.security


@pytest.mark.parametrize("row", [row for row in PROTECTED if "GET" in row["methods"]], ids=lambda row: row["endpoint"])
def test_implicit_head_rejects_anonymous_access(secured, row):
    app, db, ids, password = secured
    actor = app.test_client()
    before = copy.deepcopy(snapshot(db))
    response = actor.head(path_for(row, ids))
    assert response.status_code in {302, 401, 403}
    assert response.data == b""
    assert snapshot(db) == before


@pytest.mark.parametrize("row", [row for row in OBJECT_ROUTES if "GET" in row["methods"]], ids=lambda row: row["endpoint"])
def test_implicit_head_rejects_another_accounts_objects(secured, row):
    app, db, ids, password = secured
    actor = app.test_client()
    login(actor, "outsider", password)
    before = copy.deepcopy(snapshot(db))
    response = actor.head(path_for(row, ids))
    assert response.status_code in {403, 404}
    assert response.data == b""
    assert snapshot(db) == before


@pytest.mark.parametrize("row", PROTECTED, ids=lambda row: row["endpoint"])
def test_automatic_options_discloses_no_private_data_or_mutation(secured, row):
    app, db, ids, password = secured
    before = copy.deepcopy(snapshot(db))
    response = app.test_client().options(path_for(row, ids))
    # A request validator may reject missing JSON before Flask serves OPTIONS.
    # Either result must leave private application data and records untouched.
    assert response.status_code in {200, 400}
    if response.status_code == 200:
        assert response.data == b""
        assert set(row["methods"]) <= set(response.headers["Allow"].replace(" ", "").split(","))
    else:
        assert b"Bad Request" in response.data
    assert b"Private audit" not in response.data
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert snapshot(db) == before


@pytest.mark.parametrize("path", ["/.env", "/.git/config", "/app.py", "/static/../app.py", "/static/%2e%2e/app.py", "/static/..%5capp.py"])
def test_public_static_route_cannot_escape_to_repository_files(secured, path):
    app, db, ids, password = secured
    response = app.test_client().get(path)
    assert response.status_code in {400, 403, 404}
    assert b"def create_app" not in response.data and b"MONGO_URI=" not in response.data
