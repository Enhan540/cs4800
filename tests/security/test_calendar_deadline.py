import gzip
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from services import calendar_service

pytestmark = pytest.mark.security


@pytest.fixture
def slow_calendar_provider(tmp_path, monkeypatch):
    hostname = "calendar.example.invalid"
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                   .sign(key, hashes.SHA256()))
    certificate_file, key_file = tmp_path / "local-cert.pem", tmp_path / "local-key.pem"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))

    class SlowProvider(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.close_connection = True
            try:
                if self.path == "/redirect":
                    time.sleep(0.45)
                    self.send_response(302)
                    self.send_header("Location", "/body")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                if self.path in {"/fast", "/fast-gzip"}:
                    payload = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
                    if self.path == "/fast-gzip":
                        payload = gzip.compress(payload)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/calendar")
                    self.send_header("Content-Length", str(len(payload)))
                    if self.path == "/fast-gzip":
                        self.send_header("Content-Encoding", "gzip")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = gzip.compress(b"x" * 80) if self.path == "/gzip-body" else b"x" * 80
                if self.path == "/headers":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/calendar\r\nX-Slow: ")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/calendar")
                    self.send_header("Content-Length", str(len(payload)))
                    if self.path == "/gzip-body":
                        self.send_header("Content-Encoding", "gzip")
                    self.send_header("Connection", "close")
                    self.end_headers()
                for value in payload:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(0.06 if self.path == "/gzip-body" else 0.02)
                if self.path == "/headers":
                    self.wfile.write(b"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except (ConnectionError, ssl.SSLError):
                pass

        def log_message(self, *_arguments):
            pass

    class LocalServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
                return
            super().handle_error(request, client_address)

    server = LocalServer(("127.0.0.1", 0), SlowProvider)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_file, key_file)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.daemon_threads = True
    worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    worker.start()

    real_pool = calendar_service.urllib3.HTTPSConnectionPool

    def local_pool(address, **options):
        assert address == "127.0.0.1", "Transport fixture must remain loopback-only"
        options["port"] = server.server_port
        return real_pool(address, **options)

    monkeypatch.setattr(calendar_service, "public_calendar_addresses", lambda _host, **kw: ["127.0.0.1"])
    monkeypatch.setattr(calendar_service.certifi, "where", lambda: str(certificate_file))
    monkeypatch.setattr(calendar_service.urllib3, "HTTPSConnectionPool", local_pool)
    monkeypatch.setattr(calendar_service, "MAX_CALENDAR_FETCH_SECONDS", 0.6, raising=False)
    try:
        yield "https://" + hostname
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize("path", ["/body", "/headers", "/redirect", "/gzip-body"])
def test_continuous_slow_delivery_cannot_extend_calendar_deadline(slow_calendar_provider, path):
    started = time.monotonic()
    with pytest.raises(calendar_service.CalendarFeedError) as captured:
        calendar_service.fetch_calendar_url(slow_calendar_provider + path)
    elapsed = time.monotonic() - started
    assert captured.value.code == "timeout"
    assert elapsed < 0.9, "Buffered reads or redirects extended the shared transport deadline"


@pytest.mark.parametrize("path", ["/fast", "/fast-gzip"])
def test_validated_transport_preserves_fast_plain_and_compressed_feeds(slow_calendar_provider, path):
    assert calendar_service.fetch_calendar_url(slow_calendar_provider + path) == (
        b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    )


def test_validated_transport_limits_decompressed_feed_size(slow_calendar_provider, monkeypatch):
    monkeypatch.setattr(calendar_service, "MAX_CALENDAR_BYTES", 8)
    with pytest.raises(calendar_service.CalendarFeedError) as captured:
        calendar_service.fetch_calendar_url(slow_calendar_provider + "/fast-gzip")
    assert captured.value.code == "too_large"


def test_deadline_transport_keeps_certificate_verification(slow_calendar_provider, monkeypatch):
    from pathlib import Path

    default_bundle = Path(calendar_service.certifi.__file__).with_name("cacert.pem")
    assert default_bundle.is_file()
    monkeypatch.setattr(calendar_service, "MAX_CALENDAR_FETCH_SECONDS", 5)
    monkeypatch.setattr(calendar_service.certifi, "where", lambda: str(default_bundle))
    with pytest.raises(calendar_service.CalendarFeedError) as captured:
        calendar_service.fetch_calendar_url(slow_calendar_provider + "/fast")
    assert captured.value.code == "network"


def test_deadline_transport_keeps_hostname_verification(slow_calendar_provider, monkeypatch):
    monkeypatch.setattr(calendar_service, "MAX_CALENDAR_FETCH_SECONDS", 5)
    wrong_host = slow_calendar_provider.replace("calendar.example.invalid", "mismatch.example.invalid")
    with pytest.raises(calendar_service.CalendarFeedError) as captured:
        calendar_service.fetch_calendar_url(wrong_host + "/fast")
    assert captured.value.code == "network"
