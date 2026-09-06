"""Capture the HTTP version before using it; no account/browser credentials."""
from __future__ import annotations

from html.parser import HTMLParser
import http.client
import io
import ipaddress
import json
import math
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse

from .preservation import PreservationError, Store, guard_no_secrets, timestamp

_AUTH_QUERY_KEYS = frozenset({
    "token", "access_token", "refresh_token", "api_key", "apikey", "key", "secret",
    "client_secret", "password", "authorization", "auth", "jwt", "signature", "sig",
    "x_amz_signature", "x_amz_credential", "x_goog_signature", "x_goog_credential",
})
_TEXT_MIMES = frozenset({"text/plain", "text/html", "text/markdown", "application/json", "application/xml", "text/xml"})


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _public_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        raise PreservationError("source_dns_invalid_address") from None
    if (not address.is_global or address.is_private or address.is_loopback
            or address.is_link_local or address.is_multicast or address.is_reserved
            or address.is_unspecified or "%" in raw):
        raise PreservationError("source_nonpublic_address")
    # Do not allow embedded addresses/tunnel prefixes to evade the IPv4 checks.
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None or address.sixtofour is not None
        or address.teredo is not None or address.is_site_local
    ):
        raise PreservationError("source_nonpublic_address")
    return address


_DNS_PROGRAM = """import json,socket,sys
host,port=json.load(sys.stdin)
rows=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM,proto=socket.IPPROTO_TCP)
if len(rows)>64: sys.exit(2)
json.dump([[int(row[0]),row[4][0]] for row in rows],sys.stdout)
"""


def _lookup(host: str, port: int, deadline: float) -> list:
    """A bounded DNS helper. No shell, user config, proxies, or daemon thread."""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", _DNS_PROGRAM],
            input=json.dumps([host, port]).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=_remaining(deadline), check=False,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for the helper before raising.
        raise TimeoutError from None
    if result.returncode != 0 or len(result.stdout) > 16384:
        raise PreservationError("source_dns_failed")
    try:
        rows = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise PreservationError("source_dns_failed") from None
    return rows


def _addresses(host: str, port: int, deadline: float) -> list[tuple[int, str]]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        address = _public_ip(host)
        return [(socket.AF_INET if address.version == 4 else socket.AF_INET6, str(address))]
    rows = _lookup(host, port, deadline)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise PreservationError("source_dns_failed")
    addresses = set()
    for row in rows:
        if (not isinstance(row, (list, tuple)) or len(row) != 2
                or not isinstance(row[0], int) or isinstance(row[0], bool)
                or row[0] not in (socket.AF_INET, socket.AF_INET6) or not isinstance(row[1], str)):
            raise PreservationError("source_dns_failed")
        family, raw = row
        address = _public_ip(raw)
        if (family == socket.AF_INET) != (address.version == 4):
            raise PreservationError("source_dns_failed")
        addresses.add((family, str(address)))
    # Reject the whole DNS answer if even one address was nonpublic above.
    # Prefer IPv4 when present; connection uses the literal, never the hostname.
    return sorted(addresses, key=lambda item: (item[0] != socket.AF_INET, item[1]))


class _DeadlineReader(io.RawIOBase):
    def __init__(self, raw, network_socket, deadline):
        self.raw = raw
        self.network_socket = network_socket
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        self.network_socket.settimeout(_remaining(self.deadline))
        return self.raw.readinto(buffer)

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    """Preserve socket.makefile reference ownership and enforce a total budget."""
    def __init__(self, raw, deadline):
        self.raw = raw
        self.deadline = deadline

    def sendall(self, data):
        self.raw.settimeout(_remaining(self.deadline))
        self.raw.sendall(data)

    def makefile(self, mode):
        # Real makefile keeps the socket fd alive if HTTPConnection closes its
        # reference after a Connection: close response, before its body is read.
        return io.BufferedReader(_DeadlineReader(self.raw.makefile(mode, buffering=0), self.raw, self.deadline))

    def close(self):
        self.raw.close()


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: list[tuple[int, str]],
                 *, secure: bool, deadline: float):
        super().__init__(host, port, timeout=_remaining(deadline))
        self.addresses = addresses
        self.secure = secure
        self.deadline = deadline

    def connect(self):
        context = ssl.create_default_context() if self.secure else None
        if context is not None:
            context.set_alpn_protocols(["http/1.1"])
        for family, literal in self.addresses:
            raw = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            try:
                raw.settimeout(_remaining(self.deadline))
                # socket.connect receives a validated numeric address. It does
                # not resolve the original hostname a second time (rebinding).
                raw.connect((literal, self.port, 0, 0) if family == socket.AF_INET6 else (literal, self.port))
                if str(_public_ip(raw.getpeername()[0])) != literal:
                    raise PreservationError("source_peer_address_mismatch")
                if context is not None:
                    raw.settimeout(_remaining(self.deadline))
                    raw = context.wrap_socket(raw, server_hostname=self.host)
                self.sock = _DeadlineSocket(raw, self.deadline)
                return
            except (OSError, PreservationError) as exc:
                raw.close()
                if isinstance(exc, (PreservationError, ssl.SSLError, TimeoutError)):
                    raise
        raise OSError("source_connection_failed") from None


def _url_parts(url: str):
    if not isinstance(url, str) or len(url) > 16384 or re.search(r"[\x00-\x20\x7f]", url):
        raise PreservationError("public_http_url_required")
    guard_no_secrets(url.encode())
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or "%" in host or parsed.port == 0):
            raise ValueError
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local")) or host == "metadata.google.internal":
            raise PreservationError("source_nonpublic_address")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
                raise ValueError
        for component in (parsed.query, parsed.fragment):
            for key, _ in urllib.parse.parse_qsl(component, keep_blank_values=True, max_num_fields=200):
                if key.lower().replace("-", "_") in _AUTH_QUERY_KEYS:
                    raise PreservationError("source_authenticated_url_not_supported")
    except (ValueError, UnicodeError):
        raise PreservationError("public_http_url_required") from None
    target = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        target += "?" + urllib.parse.quote(parsed.query, safe="?/%:@!$&'()*+,;=-._~")
    host_header = "[" + host + "]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    if port != default_port:
        host_header += ":" + str(port)
    return parsed, host, port, host_header, target


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.ignored += 1
        if not self.ignored and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "pre"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.ignored = max(0, self.ignored - 1)
        if not self.ignored and tag in {"p", "div", "li", "h1", "h2", "h3", "tr", "pre"}:
            self.text.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.text.append(data)

    def value(self):
        # Preserve indentation/code whitespace. This is structural HTML text,
        # not a claim that CSS/JavaScript-rendered visibility was reproduced.
        return "".join(self.text).strip("\r\n")


def capture_url(store: Store, url: str, *, scope: str, title: str,
                source_key: str | None = None, max_bytes: int = 32 * 1024 * 1024,
                timeout: float = 30):
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 64 * 1024 * 1024:
        raise PreservationError("invalid_source_size_limit")
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise PreservationError("invalid_source_timeout")
    parsed, host, port, host_header, target = _url_parts(url)
    deadline = time.monotonic() + timeout
    connection = None
    try:
        addresses = _addresses(host, port, deadline)
        connection = _PinnedConnection(host, port, addresses, secure=parsed.scheme == "https", deadline=deadline)
        connection.request("GET", target, headers={
            "Host": host_header, "User-Agent": "Olympus/0.1 source capture",
            "Accept-Encoding": "identity", "Connection": "close",
        })
        with connection.getresponse() as response:
            if 300 <= response.status < 400:
                raise PreservationError("source_redirect_requires_explicit_url")
            if response.status == 206 or response.headers.get("Content-Range"):
                raise PreservationError("source_partial_response")
            if response.status != 200:
                raise PreservationError("source_http_status_" + str(response.status))
            if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
                raise PreservationError("source_content_encoding_requires_extractor")
            lengths = response.headers.get_all("Content-Length", [])
            transfer = response.headers.get("Transfer-Encoding")
            if len({value.strip() for value in lengths}) > 1 or transfer and lengths:
                raise PreservationError("source_ambiguous_response_length")
            if transfer and transfer.strip().lower() != "chunked":
                raise PreservationError("source_transfer_encoding_unsupported")
            expected_length = None
            if lengths:
                if not re.fullmatch(r"[0-9]{1,20}", lengths[0].strip()):
                    raise PreservationError("source_invalid_response_length")
                expected_length = int(lengths[0])
                if expected_length > max_bytes:
                    raise PreservationError("source_size_limit")
            mime = response.headers.get_content_type()
            if mime not in _TEXT_MIMES:
                raise PreservationError("source_requires_file_extractor")
            raw = response.read(max_bytes + 1)
            _remaining(deadline)
            if len(raw) > max_bytes:
                raise PreservationError("source_size_limit")
            if expected_length is not None and len(raw) != expected_length:
                raise PreservationError("source_incomplete_response")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                decoded = raw.decode(charset, errors="strict")
            except (LookupError, UnicodeError):
                raise PreservationError("source_encoding_requires_extractor") from None
            if "\x00" in decoded:
                raise PreservationError("source_requires_file_extractor")
            meta = {"media_type": mime, "fetched_at": timestamp(), "effective_url": urllib.parse.urldefrag(url).url,
                    "transport": "public-ip-pinned-http-v1",
                    "extraction": "html-structural-text-v1" if mime == "text/html" else "decoded-full-text"}
            for header in ("ETag", "Last-Modified"):
                if response.headers.get(header):
                    meta[header.lower()] = response.headers[header]
    except (TimeoutError, subprocess.TimeoutExpired):
        raise PreservationError("source_timeout") from None
    except (http.client.HTTPException, OSError):
        raise PreservationError("source_fetch_failed") from None
    finally:
        if connection is not None:
            connection.close()
    if mime == "text/html":
        extractor = TextExtractor()
        extractor.feed(decoded)
        text = extractor.value()
    else:
        text = decoded
    # fetched_at is observation, not identity or evidence effective time.
    observed_at = meta.pop("fetched_at")
    return store.capture(source_key=source_key or urllib.parse.urldefrag(url).url,
                         scope=scope, title=title, original=raw, text=text, locator=url,
                         kind="document", metadata=meta, observed_at=observed_at)
