# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signed, length-framed wire protocol for the P2P mesh (Phase C - Pillar 4).

Messages are canonical JSON (sorted keys, compact separators), authenticated with
HMAC-SHA256 over the canonical bytes (excluding the ``sig`` field) and framed as
``uint32 length (big-endian) || payload``.  There is **no encryption**: the
security model is a shared symmetric key plus an IP allowlist on a trusted
network, with endpoints shipped pre-anonymized (hashed ids).
"""

__all__ = [
    "PROTOCOL_VERSION",
    "DEFAULT_CLOCK_SKEW_S",
    "DEFAULT_MAX_FRAGMENT_BYTES",
    "DEFAULT_MAX_FRAGMENTS",
    "canonical_bytes",
    "sign",
    "verify",
    "encode_message",
    "decode_message",
    "read_frame",
    "write_frame",
    "sanitize_fragment",
]

import hashlib
import hmac
import json
import socket
import struct
import time

from searx import logger

log = logger.getChild("searx.mesh.protocol")

PROTOCOL_VERSION: int = 1
DEFAULT_CLOCK_SKEW_S: float = 300.0
DEFAULT_MAX_FRAGMENT_BYTES: int = 4096
DEFAULT_MAX_FRAGMENTS: int = 200
_REL_MAX_LEN: int = 60
_ID_MAX_LEN: int = 64

_HEADER: struct.Struct = struct.Struct(">I")


def canonical_bytes(message: dict) -> bytes:
    """Return the canonical JSON encoding of ``message`` (``sig`` excluded)."""
    body = {k: v for k, v in message.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(payload: bytes, key: bytes) -> str:
    """Return the HMAC-SHA256 hex digest of ``payload`` under ``key``."""
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def verify(payload: bytes, signature: str, key: bytes) -> bool:
    """Constant-time verification of ``signature`` over ``payload``."""
    try:
        expected = sign(payload, key)
        return hmac.compare_digest(expected, str(signature or ""))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh protocol: verify failed: %s", e)
        return False


def sanitize_fragment(fragment: dict) -> dict | None:
    """Validate / normalize one fragment; return ``None`` when invalid."""
    try:
        if not isinstance(fragment, dict):
            return None
        src = str(fragment.get("s", ""))[:_ID_MAX_LEN]
        dst = str(fragment.get("t", ""))[:_ID_MAX_LEN]
        rel = str(fragment.get("r", ""))[:_REL_MAX_LEN]
        if not src or not dst or not rel or src == dst:
            return None
        try:
            weight = float(fragment.get("w", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        if weight <= 0:
            return None
        return {"s": src, "r": rel, "t": dst, "w": round(weight, 3)}
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh protocol: sanitize_fragment failed: %s", e)
        return None


def _dump(message: dict) -> bytes:
    return json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encode_message(message: dict, key: bytes) -> bytes:
    """Sign ``message`` and return a length-framed byte string."""
    payload = canonical_bytes(message)
    signed = dict(message)
    signed["sig"] = sign(payload, key)
    frame = _dump(signed)
    return _HEADER.pack(len(frame)) + frame


def _load_json(data: bytes) -> dict:
    try:
        loaded = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        if len(data) >= 4:
            loaded = json.loads(data[4:].decode("utf-8"))
        else:
            raise
    if not isinstance(loaded, dict):
        raise ValueError("not a message")
    return loaded


def decode_message(
    data: bytes,
    key: bytes,
    *,
    max_skew: float = DEFAULT_CLOCK_SKEW_S,
    now: float | None = None,
) -> dict:
    """Verify and parse a message (framed or raw).  Raises on any failure."""
    message = _load_json(data)
    if int(message.get("v", 0)) != PROTOCOL_VERSION:
        raise ValueError("bad version")
    if not verify(canonical_bytes(message), str(message.get("sig", "")), key):
        raise ValueError("bad signature")
    ts = float(message.get("ts", 0) or 0)
    current = time.time() if now is None else float(now)
    if abs(current - ts) > float(max_skew):
        raise ValueError("stale message")
    if not str(message.get("nonce", "")):
        raise ValueError("missing nonce")
    if not isinstance(message.get("fragments"), list):
        raise ValueError("bad fragments")
    return message


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < n:
        chunk = sock.recv(n - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(sock: socket.socket, max_bytes: int) -> bytes | None:
    """Read exactly one framed message payload; ``None`` on clean EOF."""
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length <= 0 or length > max(1, int(max_bytes)) + 4096:
        raise ValueError("frame too large")
    body = _recv_exact(sock, length)
    if body is None:
        raise ValueError("short frame")
    return body


def write_frame(sock: socket.socket, data: bytes) -> None:
    """Write ``data`` as one framed message (adds the header if absent)."""
    frame = data
    if not (len(data) >= 4 and _HEADER.unpack(data[:4])[0] == len(data) - 4):
        frame = _HEADER.pack(len(data)) + data
    sock.sendall(frame)
