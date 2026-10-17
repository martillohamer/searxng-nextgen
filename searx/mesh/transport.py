# SPDX-License-Identifier: AGPL-3.0-or-later
"""TCP transport for the P2P mesh (Phase C - Pillar 4).

A minimal client (:py:func:`send_sync`) and a threaded server
(:py:class:`MeshServer`) speaking the signed, length-framed protocol of
:py:mod:`searx.mesh.protocol`.  stdlib ``socket`` only; **plaintext** (no TLS).
The server rejects any peer whose source IP is not in the configured allowlist
(fail-closed).  Every entry point is fail-safe.
"""

__all__ = ["Peer", "send_sync", "send_query", "send_hello", "MeshServer"]

import secrets
import socket
import threading
import time
import typing as t

from searx import logger

from .protocol import (
    DEFAULT_MAX_FRAGMENT_BYTES,
    DEFAULT_MAX_FRAGMENTS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MAX_RESULT_BYTES,
    KIND_HELLO,
    KIND_SEARCH_REQUEST,
    KIND_SEARCH_RESPONSE,
    PROTOCOL_VERSION,
    decode_message,
    encode_message,
    query_hash,
    read_frame,
    sanitize_fragment,
    sanitize_search_request,
    sanitize_search_results,
    write_frame,
)

log = logger.getChild("searx.mesh.transport")

_READ_CAP: int = max(DEFAULT_MAX_FRAGMENT_BYTES * DEFAULT_MAX_FRAGMENTS, DEFAULT_MAX_RESULT_BYTES)


class Peer(t.NamedTuple):
    """A remote mesh node endpoint."""

    node_id: str
    host: str
    port: int


def _nonce() -> str:
    return secrets.token_hex(8)


def send_sync(
    peer: Peer,
    key: bytes,
    node_id: str,
    fragments: t.Iterable[dict],
    *,
    timeout: float = 5.0,
    max_fragment_bytes: int = DEFAULT_MAX_FRAGMENT_BYTES,
    max_fragments: int = DEFAULT_MAX_FRAGMENTS,
) -> list[dict]:
    """Send a sync request to ``peer``; return the verified response fragments."""
    try:
        limit = max(1, int(max_fragments))
        clean = [f for f in (sanitize_fragment(x) for x in (fragments or [])) if f][:limit]
        request = {
            "v": PROTOCOL_VERSION,
            "node_id": str(node_id),
            "kind": "sync_request",
            "ts": time.time(),
            "nonce": _nonce(),
            "fragments": clean,
        }
        cap = max(1, int(max_fragment_bytes)) * limit
        with socket.create_connection((peer.host, int(peer.port)), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(encode_message(request, key))
            body = read_frame(sock, cap)
        if body is None:
            return []
        response = decode_message(body, key)
        if response.get("kind") != "sync_response":
            return []
        return [f for f in (sanitize_fragment(x) for x in response.get("fragments", [])) if f]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh send_sync failed: %s", e)
        return []


def send_query(
    peer: Peer,
    key: bytes,
    node_id: str,
    query: str,
    *,
    categories: t.Iterable[str] | None = None,
    language: str = "all",
    safesearch: int = 0,
    pageno: int = 1,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 5.0,
) -> dict[str, t.Any]:
    """Send one search request; return the normalized response (``{}`` on failure).

    ``query`` travels in clear text over the signed channel; only a short
    correlation hash is echoed back in the response.
    """
    try:
        request = {
            "v": PROTOCOL_VERSION,
            "node_id": str(node_id),
            "kind": KIND_SEARCH_REQUEST,
            "ts": time.time(),
            "nonce": _nonce(),
            "query": str(query),
            "categories": [str(c) for c in (categories or ())],
            "language": str(language or "all"),
            "safesearch": int(safesearch),
            "pageno": int(pageno),
            "max_results": int(max_results),
        }
        with socket.create_connection((peer.host, int(peer.port)), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(encode_message(request, key))
            body = read_frame(sock, DEFAULT_MAX_RESULT_BYTES)
        if body is None:
            return {}
        response = decode_message(body, key)
        if response.get("kind") != KIND_SEARCH_RESPONSE:
            return {}
        results = sanitize_search_results(response.get("results", []))
        try:
            count = int(response.get("count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        return {
            "node_id": str(response.get("node_id", "") or peer.node_id),
            "query_hash": str(response.get("query_hash", "") or ""),
            "results": results,
            "count": count if count > 0 else len(results),
            "error": str(response.get("error", "") or ""),
        }
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh send_query failed: %s", e)
        return {}


def send_hello(
    peer: Peer,
    key: bytes,
    node_id: str,
    *,
    capabilities: t.Iterable[str] | None = None,
    engines: t.Iterable[str] | None = None,
    timeout: float = 5.0,
) -> dict:
    """Exchange capabilities with a peer; returns ``{node_id, capabilities, engines}``."""
    try:
        request = {
            "v": PROTOCOL_VERSION,
            "node_id": str(node_id),
            "kind": KIND_HELLO,
            "ts": time.time(),
            "nonce": _nonce(),
            "capabilities": [str(c) for c in (capabilities or ())],
            "engines": [str(engine) for engine in (engines or ())],
        }
        with socket.create_connection((peer.host, int(peer.port)), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(encode_message(request, key))
            body = read_frame(sock, DEFAULT_MAX_RESULT_BYTES)
        if body is None:
            return {}
        response = decode_message(body, key)
        if response.get("kind") != KIND_HELLO:
            return {}
        return {
            "node_id": str(response.get("node_id", "") or peer.node_id),
            "capabilities": [str(c) for c in (response.get("capabilities") or [])],
            "engines": [str(engine) for engine in (response.get("engines") or [])],
        }
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh send_hello failed: %s", e)
        return {}


class MeshServer:
    """Threaded TCP server answering ``sync_request``, ``hello`` and ``search_request``."""

    def __init__(
        self,
        host: str,
        port: int,
        key: bytes,
        node_id: str,
        fragment_source: t.Callable[[], t.Iterable[dict]] | None,
        allowlist: t.Iterable[str] | None = None,
        *,
        search_handler: t.Callable[[str, dict], t.Any] | None = None,
        capabilities: t.Iterable[str] | None = None,
        engines: t.Iterable[str] | None = None,
    ) -> None:
        self._host = str(host or "127.0.0.1")
        self._port = int(port)
        self._key = key
        self._node_id = str(node_id)
        self._source = fragment_source
        self._allowlist: set[str] = set(allowlist or ())
        self._search_handler = search_handler
        self._capabilities = tuple(str(c) for c in (capabilities or ()))
        self._engines = tuple(str(engine) for engine in (engines or ()))
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> bool:
        """Bind, listen and launch the accept loop.  Returns success."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._host, self._port))
            sock.listen(16)
            sock.settimeout(1.0)
            self._sock = sock
            self._stop.clear()
            self._thread = threading.Thread(target=self.serve_forever, name="searx-mesh-server", daemon=True)
            self._thread.start()
            return True
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: start failed: %s", e)
            self.stop()
            return False

    def serve_forever(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()
            except Exception as e:  # pylint: disable=broad-except
                log.debug("mesh transport: serve_forever thread spawn failed: %s", e)
                self._close(conn)

    def _handle(self, conn: socket.socket, addr: t.Any) -> None:
        try:
            conn.settimeout(5.0)
            if not addr or addr[0] not in self._allowlist:
                return
            body = read_frame(conn, _READ_CAP)
            if body is None:
                return
            message = decode_message(body, self._key)
            kind = message.get("kind")
            if kind == "sync_request":
                response: dict | None = {
                    "v": PROTOCOL_VERSION,
                    "node_id": self._node_id,
                    "kind": "sync_response",
                    "ts": time.time(),
                    "nonce": _nonce(),
                    "fragments": self._local_fragments(),
                }
            elif kind == KIND_SEARCH_REQUEST:
                response = self._build_search_response(message)
            elif kind == KIND_HELLO:
                response = self._build_hello_response()
            else:
                response = None
            if response is None:
                return
            write_frame(conn, encode_message(response, self._key))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: handle failed: %s", e)
        finally:
            self._close(conn)

    def _build_search_response(self, message: dict) -> dict:
        request = sanitize_search_request(message)
        response: dict[str, t.Any] = {
            "v": PROTOCOL_VERSION,
            "node_id": self._node_id,
            "kind": KIND_SEARCH_RESPONSE,
            "ts": time.time(),
            "nonce": _nonce(),
            "query_hash": query_hash(str(message.get("query", "")), str(message.get("language", "") or "all")),
            "results": [],
            "count": 0,
            "error": "",
        }
        if request is None:
            response["error"] = "bad_request"
            return response
        handler = self._search_handler
        if handler is None:
            response["error"] = "no_handler"
            return response
        try:
            out = handler(request["query"], request)
            raw = out.get("results", []) if isinstance(out, dict) else (out or [])
            clean = sanitize_search_results(raw)[: request["max_results"]]
            response["results"] = clean
            response["count"] = len(clean)
            if isinstance(out, dict):
                try:
                    count = int(out.get("count", 0) or 0)
                except (TypeError, ValueError):
                    count = 0
                response["count"] = count if count > 0 else len(clean)
                response["error"] = str(out.get("error", "") or "")
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: search handler failed: %s", e)
            response["error"] = "handler_error"
        return response

    def _build_hello_response(self) -> dict:
        return {
            "v": PROTOCOL_VERSION,
            "node_id": self._node_id,
            "kind": KIND_HELLO,
            "ts": time.time(),
            "nonce": _nonce(),
            "capabilities": list(self._capabilities),
            "engines": list(self._engines),
        }

    def _local_fragments(self) -> list[dict]:
        try:
            source = self._source
            if source is None:
                return []
            return [f for f in (sanitize_fragment(x) for x in source()) if f][:DEFAULT_MAX_FRAGMENTS]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: fragment source failed: %s", e)
            return []

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        self._sock = None
        if sock is not None:
            self._close(sock)

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.close()
        except Exception:  # pylint: disable=broad-except
            pass
