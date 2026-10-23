from __future__ import annotations

import io
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from auto_outbound import research
from auto_outbound.config import AppError
from auto_outbound.files import read_rows, safe_cell, spreadsheet
from openpyxl import load_workbook


def test_all_xlsx_cells_escape_formula_payloads():
    campaign = {
        "name": '=HYPERLINK("https://example.invalid", "click")',
        "demo": False,
        "sender": {
            "name": "Example",
            "company": "Company",
            "email": "x@example.com",
            "postal_address": "Example",
        },
    }
    book = load_workbook(io.BytesIO(spreadsheet(campaign, [], "xlsx")), data_only=False)
    assert book["Read me"]["B1"].data_type == "s"
    assert book["Read me"]["B1"].value.startswith("'=")
    assert not any(
        cell.data_type == "f" for sheet in book for row in sheet for cell in row
    )


@pytest.mark.parametrize(
    "payload", ["=1+1", "+SUM(1)", "-10+20", "@SUM(1)", "\t=1", "\r=1", "  =1"]
)
def test_spreadsheet_dangerous_prefixes_are_text(payload):
    assert safe_cell(payload).startswith("'")


@pytest.mark.parametrize(
    "header", ["company,,website", "company,---,website", "company,company,website"]
)
def test_empty_or_duplicate_headers_do_not_silently_discard_data(header):
    with pytest.raises(AppError) as error:
        read_rows(
            "leads.csv", f"{header}\nExample,important,https://example.com\n".encode()
        )
    assert error.value.code == "FILE-HEADERS"


def test_html_contacts_come_from_actual_links_not_script_or_comments():
    parser = research.TextParser()
    parser.feed("""
        <script>var hidden = '<a href="mailto:script@example.com">';</script>
        <!-- <a href="mailto:old@example.com">old contact</a> -->
        <style>.x::after { content: '<a href="tel:555-0000">'; }</style>
        <noscript><a href="mailto:noscript@example.com">Fallback</a></noscript>
        <svg><a href="mailto:svg@example.com">Graphic</a></svg>
        <a href="mailto:public%40example.com?subject=Hello&amp;cc=ignore@example.com">Email us</a>
        <a href="MAILTO:entity&#64;example.com">Other team</a>
        <a href="tel:%2B1-800-555-0100">Phone</a>
    """)
    text = " ".join(parser.parts)
    assert "public@example.com" in text
    assert "entity@example.com" in text
    assert "+1-800-555-0100" in text
    assert not any(
        value in text
        for value in (
            "script@example.com",
            "old@example.com",
            "ignore@example.com",
            "555-0000",
            "svg@example.com",
        )
    )


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "10.0.0.1",
        "192.168.1.1",
        "localhost",
        "metadata.internal",
    ],
)
def test_non_public_addresses_rejected(host):
    with pytest.raises(research.SourceUnavailable):
        research.public_addresses(host)


class Response:
    def __init__(self, content=b"Public page", *, status=200, headers=None):
        self.content = io.BytesIO(content)
        self.status = status
        self.headers = {"Content-Type": "text/plain"} | (headers or {})

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, count):
        return self.content.read(count)


def install_responses(monkeypatch, responses):
    calls = []

    class Connection:
        def __init__(self, host, address, secure, timeout=8):
            self.response = responses.pop(0)
            calls.append({"host": host, "address": address, "secure": secure})

        def request(self, method, path, headers):
            calls[-1]["path"] = path

        def getresponse(self):
            return self.response

        def close(self):
            pass

        def abort(self):
            pass

    monkeypatch.setattr(research, "PinnedConnection", Connection)
    return calls


def test_redirects_revalidate_private_destinations(monkeypatch):
    original = research.public_addresses

    def resolve(host):
        return ["93.184.216.34"] if host == "example.com" else original(host)

    monkeypatch.setattr(research, "public_addresses", resolve)
    calls = install_responses(
        monkeypatch,
        [Response(status=302, headers={"Location": "http://127.0.0.1/secret"})],
    )
    with pytest.raises(research.SourceUnavailable):
        research.fetch_public_text("https://example.com")
    assert len(calls) == 1


def test_fetch_pins_resolved_address_and_quotes_unicode_paths(monkeypatch):
    monkeypatch.setattr(research, "public_addresses", lambda host: ["93.184.216.34"])
    calls = install_responses(monkeypatch, [Response()])
    assert research.fetch_public_text("https://example.com/café?q=é") == "Public page"
    assert calls[0] == {
        "host": "example.com",
        "address": "93.184.216.34",
        "secure": True,
        "path": "/caf%C3%A9?q=%C3%A9",
    }


def test_page_byte_limit_enforced(monkeypatch):
    monkeypatch.setattr(research, "public_addresses", lambda host: ["93.184.216.34"])
    install_responses(monkeypatch, [Response(b"12345")])
    with pytest.raises(research.SourceUnavailable, match="page-size"):
        research.fetch_public_text("https://example.com", limit=4)


def test_nonstandard_port_is_rejected_before_dns(monkeypatch):
    monkeypatch.setattr(
        research, "public_addresses", lambda host: pytest.fail("DNS must not run")
    )
    with pytest.raises(research.SourceUnavailable, match="Nonstandard"):
        research.fetch_public_text("https://example.com:8080")


def test_cancel_before_fetch_performs_no_network(monkeypatch):
    monkeypatch.setattr(
        research, "public_addresses", lambda host: pytest.fail("DNS must not run")
    )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(research.SourceUnavailable, match="cancelled"):
        research.fetch_public_text("https://example.com", cancel=cancel)


def test_deadline_is_shared_across_redirects_and_body(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(research.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(research, "FETCH_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(research, "public_addresses", lambda host: ["93.184.216.34"])

    class SlowResponse(Response):
        def read1(self, count):
            clock[0] += 1.1
            return b"a"

    install_responses(monkeypatch, [SlowResponse()])
    with pytest.raises(research.SourceUnavailable, match="time limit"):
        research.fetch_public_text("https://example.com")
    assert clock[0] == pytest.approx(2.2)


@pytest.mark.parametrize("mode", ["slow_body", "slow_headers"])
@pytest.mark.parametrize("stop_reason", ["deadline", "cancel"])
def test_deadline_and_cancel_interrupt_real_slow_socket(monkeypatch, mode, stop_reason):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if mode == "slow_headers":
                    # A per-socket timeout resets on each byte: the watchdog
                    # must interrupt this before the status line is complete.
                    for byte in b"HTTP/1.1 200 OK\r\n":
                        self.connection.sendall(bytes([byte]))
                        time.sleep(0.05)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    for _ in range(30):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(0.05)
            except (OSError, ValueError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_connection = research.PinnedConnection

    def local_test_connection(host, address, secure, timeout=8):
        connection = real_connection(host, "127.0.0.1", False, timeout=timeout)
        connection.port = server.server_port
        return connection

    monkeypatch.setattr(research, "public_addresses", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(research, "PinnedConnection", local_test_connection)
    cancel = threading.Event()
    timer = threading.Timer(0.18, cancel.set)
    monkeypatch.setattr(
        research, "FETCH_TIMEOUT_SECONDS", 0.18 if stop_reason == "deadline" else 2
    )
    if stop_reason == "cancel":
        timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(
            research.SourceUnavailable,
            match="time limit" if stop_reason == "deadline" else "cancelled",
        ):
            research.fetch_public_text("http://example.com", cancel=cancel)
        assert time.monotonic() - started < 1.5
    finally:
        timer.cancel()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_pinned_connection_does_not_resolve_hostname_again(monkeypatch):
    sockets = []

    class FakeSocket:
        def close(self):
            pass

    def connect(address, timeout):
        sockets.append(address)
        return FakeSocket()

    monkeypatch.setattr(socket, "create_connection", connect)
    connection = research.PinnedConnection("example.com", "93.184.216.34", False)
    connection.connect()
    assert sockets == [("93.184.216.34", 80)]
    connection.close()
