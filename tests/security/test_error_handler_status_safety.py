"""Generic error-handler HTTP-status-safety fix.

A real PostgreSQL run exposed that `handle_error()` used to do:

    error_code = getattr(error, "code", 500) or 500

which happily forwards ANY exception's `.code` attribute straight to
Werkzeug as the HTTP response status - including a SQLAlchemy
IntegrityError's `.code`, which is a short alphanumeric doc-reference
string (e.g. "gkpj"), not a status. That produced a malformed status line
("HTTP/1.1 0 gkpj") that broke the HTTP response for every client, not
merely a wrong error page.

Fixed via `_safe_http_status(error)`: only a real werkzeug HTTPException
with an in-range integer `.code` (400-599) is ever used as-is; every other
exception (arbitrary runtime/application/database errors, or a fabricated
object whose `.code` is a string/None/out-of-range int) falls back to 500.
"""
import re

from werkzeug.exceptions import NotFound, TooManyRequests, BadRequest


class _FakeCodedError(Exception):
    """Simulates an exception that merely HAPPENS to expose a `.code`
    attribute that is not an HTTP status - e.g. a SQLAlchemy error, whose
    `.code` is a doc-reference string. Not a real HTTPException, so
    `isinstance(error, HTTPException)` correctly excludes it - this is
    exactly the shape the old `getattr(error, "code", 500)` logic could not
    tell apart from a real HTTP status.
    """
    def __init__(self, code):
        self.code = code
        super().__init__(f"fake error code={code!r}")


# ===========================================================================
# A-D: arbitrary/fabricated exceptions must always fall back to 500
# ===========================================================================
def test_ordinary_runtime_error_falls_back_to_500(app_module):
    assert app_module._safe_http_status(RuntimeError("boom")) == 500


def test_sqlalchemy_like_string_code_falls_back_to_500(app_module):
    """The exact real-world shape that broke production: SQLAlchemy's
    IntegrityError.code == "gkpj" (a doc-reference string)."""
    assert app_module._safe_http_status(_FakeCodedError("gkpj")) == 500


def test_code_none_falls_back_to_500(app_module):
    assert app_module._safe_http_status(_FakeCodedError(None)) == 500


def test_code_out_of_range_int_falls_back_to_500(app_module):
    assert app_module._safe_http_status(_FakeCodedError(999)) == 500


def test_code_bool_falls_back_to_500(app_module):
    """bool is a subclass of int in Python - True/False must never be
    mistaken for a valid HTTP status just because isinstance(True, int)."""
    assert app_module._safe_http_status(_FakeCodedError(True)) == 500


# ===========================================================================
# E-F: real HTTPExceptions keep their real, in-range status
# ===========================================================================
def test_real_not_found_preserves_404(app_module):
    assert app_module._safe_http_status(NotFound()) == 404


def test_real_too_many_requests_preserves_429(app_module):
    assert app_module._safe_http_status(TooManyRequests()) == 429


def test_real_bad_request_preserves_400(app_module):
    assert app_module._safe_http_status(BadRequest()) == 400


# ===========================================================================
# G-H: /api and /detect unexpected exceptions return valid 500 JSON, no leak.
# One real, side-effect-free route per prefix is temporarily swapped for a
# raiser via app.view_functions (not a change to production code, and not a
# fabricated production bug) - the REAL error handler still runs, since
# Flask's error handling wraps whichever view function actually executes.
# ===========================================================================
def test_api_unexpected_exception_returns_valid_500_json(client, app_module, monkeypatch):
    def _raiser(*a, **k):
        raise RuntimeError("boom - should never reach the client")
    monkeypatch.setitem(app_module.app.view_functions, "processing_job_status", _raiser)

    resp = client.get("/api/processing/jobs/1")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["error"] is True
    assert body["reason"] == "Server error"
    raw = resp.get_data(as_text=True)
    assert "boom" not in raw
    assert "RuntimeError" not in raw
    assert "Traceback" not in raw


def test_detect_unexpected_exception_returns_valid_500_json(client, app_module, monkeypatch):
    def _raiser(*a, **k):
        raise RuntimeError("boom - should never reach the client")
    monkeypatch.setitem(app_module.app.view_functions, "detect_init", _raiser)

    resp = client.post("/detect_init", data={})
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["detected"] is False
    assert body["error"] is True
    raw = resp.get_data(as_text=True)
    assert "boom" not in raw
    assert "RuntimeError" not in raw


# ===========================================================================
# I: the response status line stays a genuinely valid HTTP status through a
# REAL socket-level HTTP client - not just Flask's in-process test client,
# which never serializes an actual "HTTP/1.1 <code> <reason>" wire line the
# way the real bug manifested (curl: "Unsupported HTTP/1 subversion in
# response"; requests: BadStatusLine, both against a real dev server).
# ===========================================================================
def test_real_http_response_status_line_is_valid_through_raw_socket(app_module, monkeypatch):
    import socket
    import threading
    from werkzeug.serving import make_server

    def _raiser(*a, **k):
        raise RuntimeError("boom - should never reach the client")
    monkeypatch.setitem(app_module.app.view_functions, "processing_job_status", _raiser)

    server = make_server("127.0.0.1", 0, app_module.app)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"GET /api/processing/jobs/1 HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        status_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        # The exact real-world failure this guards against: "HTTP/1.1 0 gkpj"
        # (a non-numeric, non-3-digit status token) instead of a real status.
        match = re.match(r"^HTTP/1\.[01] (\d{3}) ", status_line)
        assert match, f"malformed status line: {status_line!r}"
        assert match.group(1) == "500"
    finally:
        server.shutdown()
        thread.join(timeout=5)
