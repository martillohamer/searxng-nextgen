# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 4 - Sovereign P2P Mesh (Phase C).

Exchanges **pre-anonymized graph fragments** (hashed node ids + relation
predicates + weights) with a small allowlist of trusted peers over the signed,
length-framed TCP protocol of :py:mod:`searx.mesh.transport`, on a periodic,
non-blocking background worker.

The pillar is **opt-in** (``search.nextgen.enabled`` + ``search.nextgen.mesh.enabled``),
**fail-safe** (every entry point degrades to a no-op) and respects the master-spec
rule that a **pillar never imports another pillar**: the local fragment source and
the fragment sink are *injected* by the integration layer
(:py:mod:`searx.nextgen_bridge`), which knows :py:mod:`searx.graph`.

Key material comes from ``SEARXNG_MESH_KEY`` (hex, 32 bytes) or, as fallback, the
file at ``search.nextgen.mesh.key_file``.  Without a valid key the pillar refuses
to start.

Submodules:

- :py:mod:`searx.mesh.protocol` - canonical JSON + HMAC + length framing.
- :py:mod:`searx.mesh.transport`  - TCP client/server.
"""

__all__ = ["is_enabled", "set_fragment_source", "on_fragment", "start", "stop", "sync_now", "reset"]

import os
import secrets
import tempfile
import threading
import typing as t

from searx import get_setting, logger
from searx.nextgen import is_enabled as _nextgen_enabled

from .protocol import DEFAULT_MAX_FRAGMENTS, DEFAULT_MAX_FRAGMENT_BYTES, sanitize_fragment
from .transport import MeshServer, Peer, send_sync

log = logger.getChild("searx.mesh")

_DEFAULT_PORT: int = 9876
_DEFAULT_LISTEN_HOST: str = "127.0.0.1"
_DEFAULT_SYNC_INTERVAL: int = 3600
_DEFAULT_SYNC_TIMEOUT: float = 5.0
_KEY_BYTES: int = 32
_STATE_FILE: str = os.path.join(tempfile.gettempdir(), "searx_mesh_node_id")

_lock = threading.Lock()
_server: MeshServer | None = None
_worker: threading.Thread | None = None
_stop = threading.Event()
_source: t.Callable[[], t.Iterable[dict]] | None = None
_sink: t.Callable[[dict], t.Any] | None = None
_node_id: str = ""
_stats: dict[str, int] = {"peers": 0, "sent": 0, "received": 0, "applied": 0, "errors": 0}


def is_enabled() -> bool:
    """Return ``True`` when the mesh pillar is enabled."""
    try:
        return _nextgen_enabled("mesh")
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh is_enabled failed: %s", e)
        return False


def _get(path: str, default: t.Any) -> t.Any:
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def set_fragment_source(source: t.Callable[[], t.Iterable[dict]] | None) -> None:
    """Register the callback that yields the local (pre-anonymized) fragments."""
    global _source  # pylint: disable=global-statement
    try:
        with _lock:
            _source = source
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh set_fragment_source failed: %s", e)


def on_fragment(handler: t.Callable[[dict], t.Any] | None) -> None:
    """Register the callback applied to each verified incoming fragment."""
    global _sink  # pylint: disable=global-statement
    try:
        with _lock:
            _sink = handler
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh on_fragment failed: %s", e)


def _load_key() -> bytes | None:
    """Return the 32-byte HMAC key from env or ``key_file``, or ``None``."""
    try:
        raw = (os.environ.get("SEARXNG_MESH_KEY", "") or "").strip()
        if not raw:
            key_file = str(_get("search.nextgen.mesh.key_file", "") or "")
            if key_file and os.path.isfile(key_file):
                with open(key_file, "r", encoding="utf-8") as handle:
                    raw = handle.read().strip()
        if not raw:
            return None
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            return None
        return key if len(key) == _KEY_BYTES else None
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: key load failed: %s", e)
        return None


def _resolve_node_id() -> str:
    """Return the configured node id, else a persisted / fresh random one."""
    configured = str(_get("search.nextgen.mesh.node_id", "") or "").strip()
    if configured:
        return configured
    try:
        if os.path.isfile(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as handle:
                existing = handle.read().strip()
            if existing:
                return existing
        generated = secrets.token_hex(8)
        with open(_STATE_FILE, "w", encoding="utf-8") as handle:
            handle.write(generated)
        return generated
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: node_id resolve failed: %s", e)
        return secrets.token_hex(8)


def _peers() -> list[Peer]:
    raw = _get("search.nextgen.mesh.peers", []) or []
    peers: list[Peer] = []
    if not isinstance(raw, list):
        return peers
    for item in raw:
        try:
            peers.append(Peer(str(item.get("node_id", "")), str(item["host"]), int(item["port"])))
        except Exception:  # pylint: disable=broad-except
            continue
    return peers


def _collect_local() -> list[dict]:
    with _lock:
        source = _source
    if source is None:
        return []
    try:
        return [f for f in (sanitize_fragment(x) for x in source()) if f]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: fragment source failed: %s", e)
        return []


def _apply(fragments: t.Iterable[dict]) -> int:
    with _lock:
        sink = _sink
    if sink is None:
        return 0
    applied = 0
    for raw in fragments:
        clean = sanitize_fragment(raw)
        if clean is None:
            continue
        try:
            sink(clean)
            applied += 1
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh: fragment sink failed: %s", e)
    return applied


def _sync_once() -> dict[str, int]:
    """Run one full sync round over all peers; return fresh stats."""
    key = _load_key()
    if key is None:
        return dict(_stats)
    peers = _peers()
    local = _collect_local()
    sent = received = applied = errors = 0
    try:
        timeout = float(_get("search.nextgen.mesh.sync_timeout", _DEFAULT_SYNC_TIMEOUT) or _DEFAULT_SYNC_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SYNC_TIMEOUT
    try:
        max_fragment_bytes = int(_get("search.nextgen.mesh.max_fragment_bytes", DEFAULT_MAX_FRAGMENT_BYTES))
    except (TypeError, ValueError):
        max_fragment_bytes = DEFAULT_MAX_FRAGMENT_BYTES
    try:
        max_fragments = int(_get("search.nextgen.mesh.max_fragments_per_sync", DEFAULT_MAX_FRAGMENTS))
    except (TypeError, ValueError):
        max_fragments = DEFAULT_MAX_FRAGMENTS
    node = _node_id or _resolve_node_id()
    for peer in peers:
        response = send_sync(
            peer,
            key,
            node,
            local,
            timeout=timeout,
            max_fragment_bytes=max_fragment_bytes,
            max_fragments=max_fragments,
        )
        if not response:
            continue
        sent += 1
        received += len(response)
        applied += _apply(response)
    with _lock:
        _stats.update(peers=len(peers), sent=sent, received=received, applied=applied, errors=errors)
        return dict(_stats)


def _worker_loop() -> None:
    while not _stop.is_set():
        _sync_once()
        try:
            interval = int(_get("search.nextgen.mesh.sync_interval", _DEFAULT_SYNC_INTERVAL) or _DEFAULT_SYNC_INTERVAL)
        except (TypeError, ValueError):
            interval = _DEFAULT_SYNC_INTERVAL
        if _stop.wait(max(5, interval)):
            return


def start() -> bool:
    """Start the server + sync worker.  Returns ``True`` when running."""
    global _server, _worker, _node_id  # pylint: disable=global-statement
    try:
        if not is_enabled():
            return False
        key = _load_key()
        if key is None:
            log.debug("mesh: no valid key; refusing to start")
            return False
        with _lock:
            if _server is not None:
                return True
            _node_id = _resolve_node_id()
            host = str(_get("search.nextgen.mesh.listen_host", _DEFAULT_LISTEN_HOST) or _DEFAULT_LISTEN_HOST)
            try:
                port = int(_get("search.nextgen.mesh.listen_port", _DEFAULT_PORT) or _DEFAULT_PORT)
            except (TypeError, ValueError):
                port = _DEFAULT_PORT
            allowlist = {peer.host for peer in _peers()}
            server = MeshServer(host, port, key, _node_id, _collect_local, allowlist)
            if not server.start():
                return False
            _server = server
            _stop.clear()
            _worker = threading.Thread(target=_worker_loop, name="searx-mesh-worker", daemon=True)
            _worker.start()
        return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh start failed: %s", e)
        return False


def stop() -> None:
    """Stop the sync worker and the server."""
    global _server, _worker  # pylint: disable=global-statement
    try:
        _stop.set()
        with _lock:
            server = _server
            _server = None
            _worker = None
        if server is not None:
            server.stop()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh stop failed: %s", e)


def sync_now() -> dict[str, int]:
    """Run one sync round immediately.  Fail-safe; returns the stats dict."""
    try:
        if not is_enabled() or _load_key() is None:
            return dict(_stats)
        return _sync_once()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh sync_now failed: %s", e)
        return dict(_stats)


def reset() -> None:
    """Stop everything and clear injected handlers (tests)."""
    global _source, _sink, _node_id  # pylint: disable=global-statement
    try:
        stop()
        with _lock:
            _source = None
            _sink = None
            _node_id = ""
            _stats.update(peers=0, sent=0, received=0, applied=0, errors=0)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh reset failed: %s", e)
