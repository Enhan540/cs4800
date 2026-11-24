"""The scheduled dispatcher needs only scoped delivery credentials."""

import importlib.util
from pathlib import Path

import pytest
from pymongo.errors import ConnectionFailure

pytestmark = pytest.mark.security
spec = importlib.util.spec_from_file_location("alert_dispatcher", Path(__file__).resolve().parents[2] / "scripts/security_dispatch_alerts.py")
dispatcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatcher)


@pytest.fixture
def delivery_env(monkeypatch):
    values = {"APP_BASE_URL": "https://planner.example.invalid", "MONGO_URI": "mongodb+srv://synthetic.example.invalid/", "MONGO_DB_NAME": "synthetic", "RESEND_API_KEY": "synthetic-delivery-placeholder", "FROM_EMAIL": "planner@example.invalid"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


@pytest.mark.parametrize("uri", ["mongodb://localhost/", "http://example.invalid/", "mongodb+srv://example.invalid/?tls=false", "mongodb+srv://example.invalid/?tls=true&tls=false", "mongodb+srv://example.invalid/?tlsAllowInvalidCertificates=true", "mongodb+srv://example.invalid/?tlsAllowInvalidHostnames=true", "mongodb+srv://example.invalid/?tlsInsecure=true"])
def test_dispatcher_requires_verified_database_tls(delivery_env, monkeypatch, uri):
    monkeypatch.setenv("MONGO_URI", uri)
    with pytest.raises(ValueError):
        dispatcher.configuration()


def test_dispatcher_is_bounded_and_redacts_failures(delivery_env, monkeypatch, capsys):
    class Client:
        admin = None
        def __init__(self, *args, **kwargs):
            self.admin = self
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def command(self, _name):pass
        def __getitem__(self, name):return name
    monkeypatch.setattr(dispatcher, "MongoClient", Client)
    def deliver(app, db, *, limit):
        assert app.secret_key is None and db == "synthetic" and limit == 3
        assert app.config["EMAIL_DELIVERY_MODE"] == "resend"
        return {"sent": 2, "deferred": 1}
    monkeypatch.setattr(dispatcher, "deliver_due", deliver)
    assert dispatcher.main(["--limit", "3"]) == 1
    assert "sent=2, deferred=1" in capsys.readouterr().out
    def fail(*args, **kwargs):
        raise ConnectionFailure("DO-NOT-PRINT-private-provider-payload")
    monkeypatch.setattr(dispatcher, "deliver_due", fail)
    assert dispatcher.main([]) == 1
    assert "DO-NOT-PRINT" not in capsys.readouterr().out
