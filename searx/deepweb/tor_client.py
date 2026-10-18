# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal, dependency-free HTTP-over-SOCKS5 client for the deepweb pillar.

Tor is reached through its SOCKS5 proxy (default ``socks5h://127.0.0.1:9050``).
The handshake and the HTTP/1.1 exchange are implemented directly on top of the
stdlib (``socket``/``ssl``) so the pillar pulls in **no** HTTP library and works
on a locked-down install.

Scope and honesty:

* ``socks5h`` resolves the target name *at the proxy* (required for ``.onion``).
* TLS to hidden services usually presents a self-signed certificate; verification
  is therefore **off by default** (the ``.onion`` address is the authenticator).
  Pass ``verify_tls=True`` for clearnet hosts where a real CA is expected.
* The client sends ``Connection: close`` and reads to EOF; chunked responses are
  de-chunked, ``gzip``/``deflate`` are not requested.

Every failure raises :py:class:`TorClientError` (never a bare socket error), so
callers can handle a down/locked Tor uniformly.
"""

__all__ = ["DEFAULT_TOR_PROXY", "Response", "TorClientError", "get", "is_tor_available", "post", "request"]

import json as _json
import socket
import ssl
import typing as t
from urllib.parse import urljoin, urlsplit

DEFAULT_TOR_PROXY: str = "socks5h://127.0.0.1:9050"
DEFAULT_TIMEOUT: float = 15.0
DEFAULT_USER_AGENT: str = "Mozilla/5.0 (compatible; MartillohamerSear/1.0; +https://searxng.org)"
MAX_REDIRECTS: int = 3
_MAX_RESPONSE_BYTES: int = 8 * 1024 * 1024


class TorClientError(RuntimeError):
    """Raised for any SOCKS/TLS/HTTP failure in this client."""


class Response:
    """A tiny HTTP response value object."""

    __slots__ = ("status", "reason", "headers", "body", "url")

    def __init__(self, status: int, reason: str, headers: dict[str, str], body: bytes, url: str) -> None:
        self.status: int = int(status)
        self.reason: str = str(reason)
        self.headers: dict[str, str] = headers
        self.body: bytes = body
        self.url: str = url

    @property
    def text(self) -> str:
        """Decode the body as UTF-8 (lenient)."""
        return self.body.decode("utf-8", "replace")

    def json(self) -> t.Any:
        """Parse the body as JSON (raises :py:class:`TorClientError` on failure)."""
        try:
            return _json.loads(self.text)
        except Exception as e:  # pylint: disable=broad-except
            raise TorClientError(f"invalid JSON: {e}") from e

    def __repr__(self) -> str:
        return f"Response(status={self.status}, url={self.url!r}, bytes={len(self.body)})"


# --- low-level helpers ------------------------------------------------------


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes or raise :py:class:`TorClientError`."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise TorClientError("connection closed during SOCKS handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _is_ip_literal(host: str) -> bool:
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host)
        return True
    except OSError:
        return False


def _parse_proxy(proxy: str) -> tuple[str, int, str, str, bool]:
    """Return ``(host, port, user, password, remote_resolve)`` for a proxy URL."""
    text = str(proxy or "").strip()
    if not text:
        raise TorClientError("empty proxy")
    scheme = "socks5h"
    if "://" in text:
        scheme, text = text.split("://", 1)
    scheme = scheme.lower()
    if scheme not in ("socks5h", "socks5"):
        raise TorClientError(f"unsupported proxy scheme: {scheme!r}")
    user = password = ""
    if "@" in text:
        creds, text = text.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        else:
            user = creds
    if ":" not in text:
        raise TorClientError(f"proxy has no port: {proxy!r}")
    host, port_text = text.rsplit(":", 1)
    try:
        port = int(port_text)
    except ValueError as e:
        raise TorClientError(f"invalid proxy port: {port_text!r}") from e
    return host, port, user, password, scheme == "socks5h"


def _socks5_handshake(
    sock: socket.socket, host: str, port: int, user: str, password: str, remote_resolve: bool
) -> None:
    """Perform the RFC 1929 + RFC 1928 SOCKS5 CONNECT to ``host:port``."""
    if user or password:
        sock.sendall(b"\x05\x02\x00\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    greeting = _recv_exact(sock, 2)
    if greeting[0] != 0x05:
        raise TorClientError("proxy is not SOCKS5")
    method = greeting[1]
    if method == 0x02:
        raw_user = user.encode("utf-8")
        raw_pass = password.encode("utf-8")
        request = b"\x01" + bytes([len(raw_user)]) + raw_user + bytes([len(raw_pass)]) + raw_pass
        sock.sendall(request)
        auth = _recv_exact(sock, 2)
        if auth[1] != 0x00:
            raise TorClientError("SOCKS username/password auth failed")
    elif method != 0x00:
        raise TorClientError(f"proxy offered no acceptable auth method ({method:#04x})")

    use_name = remote_resolve or not _is_ip_literal(host)
    if use_name:
        raw_host = host.encode("idna" if not host.endswith(".onion") else "utf-8")
        request = b"\x05\x01\x00\x03" + bytes([len(raw_host)]) + raw_host
    elif ":" in host:
        request = b"\x05\x01\x00\x04" + socket.inet_pton(socket.AF_INET6, host)
    else:
        request = b"\x05\x01\x00\x01" + socket.inet_aton(host)
    request += int(port).to_bytes(2, "big")
    sock.sendall(request)

    reply = _recv_exact(sock, 4)
    if reply[1] != 0x00:
        raise TorClientError(f"SOCKS connect refused (code {request[1] if False else reply[1]})")
    atyp = reply[3]
    if atyp == 0x01:
        _recv_exact(sock, 4 + 2)
    elif atyp == 0x04:
        _recv_exact(sock, 16 + 2)
    elif atyp == 0x03:
        length = _recv_exact(sock, 1)[0]
        _recv_exact(sock, length + 2)
    else:
        raise TorClientError(f"unknown SOCKS address type ({atyp:#04x})")


def _open_socket(host: str, port: int, timeout: float) -> socket.socket:
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as e:
        raise TorClientError(f"cannot connect to {host}:{port}: {e}") from e
    sock.settimeout(timeout)
    return sock


def _wrap_tls(sock: socket.socket, host: str, verify: bool) -> socket.socket:
    try:
        if verify:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()  # noqa: SLF001
        return context.wrap_socket(sock, server_hostname=host)
    except Exception as e:  # pylint: disable=broad-except
        try:
            sock.close()
        except OSError:
            pass
        raise TorClientError(f"TLS handshake with {host} failed: {e}") from e


def _connect(scheme: str, host: str, port: int, proxy: str | None, timeout: float, verify_tls: bool) -> socket.socket:
    """Open a (optionally TLS-wrapped) socket to the target via the proxy."""
    if proxy:
        phost, pport, puser, ppass, remote = _parse_proxy(proxy)
        sock = _open_socket(phost, pport, timeout)
        try:
            _socks5_handshake(sock, host, port, puser, ppass, remote)
        except TorClientError:
            try:
                sock.close()
            except OSError:
                pass
            raise
    else:
        sock = _open_socket(host, port, timeout)
    if scheme == "https":
        return _wrap_tls(sock, host, verify_tls)
    return sock


# --- HTTP/1.1 exchange ------------------------------------------------------


def _build_request(method: str, path: str, host: str, headers: dict[str, str], body: bytes | None) -> bytes:
    lines = [f"{method} {path or '/'} HTTP/1.1", f"Host: {host}", f"User-Agent: {DEFAULT_USER_AGENT}"]
    lines.append("Accept: */*")
    lines.append("Connection: close")
    for key, value in (headers or {}).items():
        if str(key).lower() in ("host", "user-agent", "connection"):
            continue
        lines.append(f"{key}: {value}")
    if body is not None:
        lines.append(f"Content-Length: {len(body)}")
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode("utf-8") + (body or b"")


def _dechunk(body: bytes) -> bytes:
    """Decode a chunked transfer body (best effort)."""
    out: list[bytes] = []
    index = 0
    while index < len(body):
        line_end = body.find(b"\r\n", index)
        if line_end == -1:
            break
        try:
            size = int(body[index:line_end].split(b";", 1)[0].strip() or b"0", 16)
        except ValueError:
            return body
        index = line_end + 2
        if size == 0:
            break
        out.append(body[index : index + size])
        index += size + 2
    return b"".join(out)


def _recv_all(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < _MAX_RESPONSE_BYTES:
        try:
            chunk = sock.recv(65536)
        except socket.timeout as e:
            raise TorClientError("timed out reading response") from e
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _parse_response(raw: bytes, url: str) -> Response:
    head, _, body = raw.partition(b"\r\n\r\n")
    head_lines = head.split(b"\r\n")
    if not head_lines or not head_lines[0]:
        raise TorClientError("empty HTTP response")
    status_parts = head_lines[0].decode("latin-1").split(" ", 2)
    try:
        status = int(status_parts[1])
    except (IndexError, ValueError) as e:
        raise TorClientError(f"malformed status line: {head_lines[0]!r}") from e
    reason = status_parts[2] if len(status_parts) > 2 else ""
    headers: dict[str, str] = {}
    for line in head_lines[1:]:
        key, sep, value = line.decode("latin-1").partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        body = _dechunk(body)
    return Response(status, reason, headers, body, url)


def _once(
    method: str, url: str, proxy: str | None, timeout: float, headers: dict[str, str], body: bytes | None, verify_tls: bool
) -> Response:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise TorClientError(f"unsupported scheme: {scheme!r}")
    host = parts.hostname or ""
    if not host:
        raise TorClientError(f"URL has no host: {url!r}")
    port = parts.port or (443 if scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    sock = _connect(scheme, host, port, proxy, timeout, verify_tls)
    try:
        sock.sendall(_build_request(method, path, host, headers, body))
        return _parse_response(_recv_all(sock), url)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def request(
    method: str,
    url: str,
    proxy: str | None = DEFAULT_TOR_PROXY,
    timeout: float = DEFAULT_TIMEOUT,
    headers: dict[str, str] | None = None,
    data: bytes | str | None = None,
    redirects: int = MAX_REDIRECTS,
    verify_tls: bool = False,
) -> Response:
    """Perform an HTTP request (optionally over the SOCKS5 proxy) and return a :py:class:`Response`.

    Follows up to ``redirects`` 3xx responses.  Raises :py:class:`TorClientError`.
    """
    if isinstance(data, str):
        body: bytes | None = data.encode("utf-8")
    else:
        body = data
    current = url
    remaining = max(0, int(redirects))
    while True:
        response = _once(method, current, proxy, float(timeout), headers or {}, body, verify_tls)
        location = response.headers.get("location")
        if 300 <= response.status < 400 and location and remaining > 0:
            current = urljoin(current, location)
            remaining -= 1
            continue
        return response


def get(url: str, **kwargs: t.Any) -> Response:
    """Convenience ``GET`` wrapper around :py:func:`request`."""
    return request("GET", url, **kwargs)


def post(url: str, data: bytes | str | None = None, **kwargs: t.Any) -> Response:
    """Convenience ``POST`` wrapper around :py:func:`request`."""
    return request("POST", url, data=data, **kwargs)


def is_tor_available(proxy: str = DEFAULT_TOR_PROXY, timeout: float = 2.0) -> bool:
    """Return ``True`` when the SOCKS proxy accepts a TCP connection."""
    try:
        host, port, _, _, _ = _parse_proxy(proxy)
    except TorClientError:
        return False
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True
