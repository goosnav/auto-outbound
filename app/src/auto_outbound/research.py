"""Bounded public-page verification. Never follows a URL into a private network."""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urljoin, urlsplit

import dns.exception
import dns.resolver

from .models import public_url


class SourceUnavailable(Exception):
    pass


FETCH_TIMEOUT_SECONDS = 20.0


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3"}:
            self.parts.append(" ")
        if tag in {"a", "area"}:
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("mailto:", "tel:")):
                # HTMLParser decodes entities; unquote handles percent-encoded
                # addresses. A real link is evidence; a JS string/comment is not.
                self.parts.append(unquote(href.split(":", 1)[1].split("?", 1)[0].split("#", 1)[0]))

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "br"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def public_addresses(host: str) -> list[str]:
    try:
        ip = ipaddress.ip_address(host)
        addresses = [str(ip)]
    except ValueError:
        if "." not in host or host.endswith(
            (".local", ".localhost", ".internal", ".test", ".invalid")
        ):
            raise SourceUnavailable("Non-public source") from None
        addresses = []
        resolver = dns.resolver.Resolver()
        resolver.lifetime = 4
        resolver.timeout = 2
        for kind in ("A", "AAAA"):
            try:
                addresses.extend(str(record) for record in resolver.resolve(host, kind))
            except dns.exception.DNSException:
                continue
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise SourceUnavailable("Source did not resolve to a public address")
    return addresses


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, secure: bool, timeout: float = 8):
        super().__init__(host, 443 if secure else 80, timeout=timeout)
        self.address, self.secure = address, secure
        self.transport_socket = None
        self.aborted = threading.Event()

    def connect(self):
        # Connect to the address already validated above. No second DNS resolution.
        if self.aborted.is_set():
            raise OSError("Connection cancelled")
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.transport_socket = sock
        if self.aborted.is_set():
            self.abort()
            raise OSError("Connection cancelled")
        if self.secure:
            # Expose the SSL socket to the watchdog before a server can stall
            # its handshake. wrap_socket otherwise detaches the raw socket
            # while doing the handshake, leaving abort() nothing to interrupt.
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.transport_socket = sock
            if self.aborted.is_set():
                self.abort()
                raise OSError("Connection cancelled")
            sock.do_handshake()
        self.sock = sock

    def abort(self):
        # Keep a reference even when HTTPConnection clears self.sock for a
        # Connection: close response. shutdown also interrupts socket.makefile
        # reads, which socket.close alone can leave waiting indefinitely.
        self.aborted.set()
        if self.transport_socket is not None:
            try:
                self.transport_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.transport_socket.close()
        self.close()


def fetch_public_text(
    url: str, limit: int = 1_000_000, cancel: threading.Event | None = None
) -> str:
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS

    def check_budget():
        if cancel and cancel.is_set():
            raise SourceUnavailable("Source check cancelled")
        if time.monotonic() >= deadline:
            raise SourceUnavailable("Source time limit reached")

    for _ in range(4):
        try:
            check_budget()
            url = public_url(url)
            parts = urlsplit(url)
            if parts.port not in {None, 80 if parts.scheme == "http" else 443}:
                raise SourceUnavailable("Nonstandard source port")
            host = (parts.hostname or "").encode("idna").decode("ascii")
            addresses = public_addresses(host)
            check_budget()
            connection = PinnedConnection(
                host,
                addresses[0],
                parts.scheme == "https",
                timeout=min(8, max(0.05, deadline - time.monotonic())),
            )
            finished = threading.Event()

            def watch_connection(current=connection, done=finished):
                # A deadline around read1 alone cannot interrupt a slowly
                # streamed HTTP status line/header. Abort the socket as well.
                while not done.wait(0.05):
                    if (cancel and cancel.is_set()) or time.monotonic() >= deadline:
                        current.abort()
                        return

            watcher = threading.Thread(target=watch_connection, daemon=True)
            watcher.start()
            try:
                path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
                if parts.query:
                    path += "?" + quote(parts.query, safe="%/:?@!$&'()*+,;=-._~[]")
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": "AutoOutbound/0.1 (public business source verification)",
                        "Accept": "text/html,text/plain",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                check_budget()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location:
                        raise SourceUnavailable("Redirect has no destination")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise SourceUnavailable(f"Source returned HTTP {response.status}")
                content_type = response.getheader("Content-Type", "").lower()
                if not any(
                    t in content_type for t in ("text/html", "text/plain", "application/xhtml")
                ):
                    raise SourceUnavailable("Source is not a text page")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise SourceUnavailable("Source uses unsupported content encoding")
                chunks, size = [], 0
                while True:
                    check_budget()
                    chunk = response.read1(min(65536, limit + 1 - size))
                    check_budget()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise SourceUnavailable("Source exceeds the page-size limit")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                text = raw.decode("utf-8", errors="replace")
                if "html" in content_type:
                    parser = TextParser()
                    parser.feed(text)
                    text = " ".join(parser.parts)
                return re.sub(r"\s+", " ", text).strip()
            finally:
                finished.set()
                connection.close()
                watcher.join(timeout=0.2)
        except SourceUnavailable:
            raise
        except (OSError, ValueError, http.client.HTTPException) as exc:
            check_budget()
            raise SourceUnavailable("Source could not be read") from exc
    raise SourceUnavailable("Too many source redirects")


EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def email_syntax(address: str) -> bool:
    if not address or len(address) > 254 or not EMAIL.fullmatch(address):
        return False
    local = address.rsplit("@", 1)[0]
    return (
        len(local) <= 64
        and not local.startswith(".")
        and not local.endswith(".")
        and ".." not in local
    )


def email_dns_status(address: str) -> str:
    if not address:
        return "missing"
    if not email_syntax(address):
        return "invalid"
    host = address.rsplit("@", 1)[1].lower()
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    resolver.timeout = 2
    try:
        mx = resolver.resolve(host, "MX")
        # RFC 7505 null MX explicitly says this domain accepts no email.
        return "mx_found" if any(str(record.exchange) != "." for record in mx) else "no_mx"
    except dns.resolver.NXDOMAIN:
        return "no_mx"
    except dns.resolver.NoAnswer:
        # SMTP permits implicit MX via A/AAAA. Report routing, never mailbox validity.
        for kind in ("A", "AAAA"):
            try:
                if resolver.resolve(host, kind):
                    return "mx_found"
            except dns.resolver.NXDOMAIN:
                return "no_mx"
            except dns.exception.DNSException:
                continue
        return "unknown"
    except dns.exception.DNSException:
        return "unknown"
