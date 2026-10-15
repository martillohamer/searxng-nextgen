# SPDX-License-Identifier: AGPL-3.0-or-later
"""TCP transport for the P2P mesh (Phase C - Pillar 4).

A minimal client (:py:func:`send_sync`) and a threaded server
(:py:class:`MeshServer`) speaking the signed, length-framed protocol of
:py:mod:`searx.mesh.protocol`.  stdlib ``socket`` only; **plaintext** (no TLS).
The server rejects any peer whose source IP is not in the configured allowlist
(fail-closed).  Every entry point is fail-safe.
"""

__all__ = ["Peer", "send_sync", "MeshServer"]

import secrets
import socket
import threading
import time
import typing as t

from searx import logger

from .protocol import (
    DEFAULT_MAX_FRAGMENT_BYTES,
    DEFAULT_MAX_FRAGMENTS,
    PROTOCOL_VERSION,
    decode_message,
    encode_message,
    read_frame,
    sanitize_fragment,
    write_frame,
)

log = logger.getChild("searx.mesh.transport")


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


class MeshServer:
    """Threaded TCP server answering ``sync_request`` with local fragments."""

    def __init__(
        self,
        host: str,
        port: int,
        key: bytes,
        node_id: str,
        fragment_source: t.Callable[[], t.Iterable[dict]] | None,
        allowlist: t.Iterable[str] | None = None,
    ) -> None:
        self._host = str(host or "127.0.0.1")
        self._port = int(port)
        self._key = key
        self._node_id = str(node_id)
        self._source = fragment_source
        self._allowlist: set[str] = set(allowlist or ())
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
            if self._allowlist and addr and addr[0] not in self._allowlist:
                return
            body = read_frame(conn, DEFAULT_MAX_FRAGMENT_BYTES * DEFAULT_MAX_FRAGMENTS)
            if body is None:
                return
            message = decode_message(body, self._key)
            if message.get("kind") != "sync_request":
                return
            response = {
                "v": PROTOCOL_VERSION,
                "node_id": self._node_id,
                "kind": "sync_response",
                "ts": time.time(),
                "nonce": _nonce(),
                "fragments": self._local_fragments(),
            }
            write_frame(conn, encode_message(response, self._key))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh server: handle failed: %s", e)
        finally:
            self._close(conn)

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
