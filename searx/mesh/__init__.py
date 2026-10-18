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

__all__ = [
    "is_enabled",
    "set_fragment_source",
    "on_fragment",
    "set_search_handler",
    "start",
    "stop",
    "sync_now",
    "search",
    "reset",
]

import os
import secrets
import tempfile
import threading
import time
import typing as t

from searx import get_setting, logger
from searx.nextgen import is_enabled as _nextgen_enabled

from .node import NodeProfile, NodeRegistry, parse_seed
from .protocol import DEFAULT_MAX_FRAGMENTS, DEFAULT_MAX_FRAGMENT_BYTES, sanitize_fragment
from .routing import aggregate, select
from .transport import MeshServer, Peer, send_query, send_sync

log = logger.getChild("searx.mesh")

_DEFAULT_PORT: int = 9876
_DEFAULT_LISTEN_HOST: str = "127.0.0.1"
_DEFAULT_SYNC_INTERVAL: int = 3600
_DEFAULT_SYNC_TIMEOUT: float = 5.0
_DEFAULT_SEARCH_TIMEOUT: float = 5.0
_DEFAULT_MAX_RESULTS: int = 20
_DEFAULT_STRATEGY: str = "latency"
_CAPABILITIES: tuple[str, ...] = ("search",)
_KEY_BYTES: int = 32
_STATE_FILE: str = os.path.join(tempfile.gettempdir(), "searx_mesh_node_id")

_lock = threading.Lock()
_server: MeshServer | None = None
_worker: threading.Thread | None = None
_stop = threading.Event()
_source: t.Callable[[], t.Iterable[dict]] | None = None
_sink: t.Callable[[dict], t.Any] | None = None
_search_handler: t.Callable[[str, dict], t.Any] | None = None
_node_id: str = ""
_registry: NodeRegistry = NodeRegistry()
_rr_offset: int = 0
_stats: dict[str, int] = {
    "peers": 0,
    "sent": 0,
    "received": 0,
    "applied": 0,
    "errors": 0,
    "queries": 0,
    "results": 0,
    "search_errors": 0,
}


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


def set_search_handler(handler: t.Callable[[str, dict], t.Any] | None) -> None:
    """Register the callback that runs a *local* search for a peer's query.

    The integration layer injects it so this pillar never imports the search
    engine directly.  It receives ``(query, options)`` and returns either a list
    of result dicts or a mapping with ``results`` / ``error``.
    """
    global _search_handler  # pylint: disable=global-statement
    try:
        with _lock:
            _search_handler = handler
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh set_search_handler failed: %s", e)


def _invoke_search_handler(query: str, opts: dict) -> t.Any:
    """Run the injected handler (under lock-free read); never raises."""
    with _lock:
        handler = _search_handler
    if handler is None:
        return {"results": [], "error": "no_handler"}
    try:
        return handler(query, opts)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: search handler failed: %s", e)
        return {"results": [], "error": "handler_error"}


def _load_key() -> bytes | None:
    """Return the 32-byte HMAC key from env or ``key_file``, or ``None``."""
    try:
        raw = (os.environ.get("SEARXNG_MESH_KEY", "") or "").strip()
        if not raw:
            raw = str(_get("search.nextgen.mesh.secret_key", "") or "").strip()
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


def _as_list(value: t.Any) -> list:
    return value if isinstance(value, list) else []


def _peers() -> list[Peer]:
    peers: list[Peer] = []
    seen: set[tuple[str, int]] = set()
    for path in ("search.nextgen.mesh.peers", "search.nextgen.mesh.seeds"):
        for item in _as_list(_get(path, []) or []):
            try:
                if isinstance(item, dict) and item.get("node_id"):
                    node_id = str(item["node_id"])
                    host, port = str(item["host"]), int(item["port"])
                else:
                    profile = parse_seed(item)
                    if profile is None:
                        continue
                    node_id, host, port = profile.node_id, profile.host, profile.port
                if (host, port) in seen:
                    continue
                seen.add((host, port))
                peers.append(Peer(node_id, host, port))
            except Exception:  # pylint: disable=broad-except
                continue
    return peers


def _refresh_registry() -> None:
    """Merge configured peers/seeds into the node registry (keeps observations)."""
    try:
        for peer in _peers():
            _registry.upsert(NodeProfile(node_id=peer.node_id, host=peer.host, port=peer.port))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: registry refresh failed: %s", e)


def _capabilities() -> frozenset[str]:
    raw = _get("search.nextgen.mesh.capabilities", None)
    if isinstance(raw, list) and raw:
        return frozenset(str(c) for c in raw if str(c))
    return frozenset(_CAPABILITIES)


def _engines() -> tuple[str, ...]:
    raw = _get("search.nextgen.mesh.engines", None)
    if isinstance(raw, list):
        return tuple(str(engine) for engine in raw if str(engine))
    try:
        from searx import engines as searx_engines  # pylint: disable=import-outside-toplevel

        return tuple(sorted(str(name) for name in getattr(searx_engines, "engines", {})))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh: engine discovery failed: %s", e)
        return ()


def _strategy() -> str:
    return str(_get("search.nextgen.mesh.routing_strategy", _DEFAULT_STRATEGY) or _DEFAULT_STRATEGY)


def _fanout() -> int:
    try:
        return max(1, int(_get("search.nextgen.mesh.fanout", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _search_timeout() -> float:
    try:
        return float(_get("search.nextgen.mesh.timeout", _DEFAULT_SEARCH_TIMEOUT) or _DEFAULT_SEARCH_TIMEOUT)
    except (TypeError, ValueError):
        return _DEFAULT_SEARCH_TIMEOUT


def _next_offset() -> int:
    global _rr_offset  # pylint: disable=global-statement
    with _lock:
        current = _rr_offset
        _rr_offset += 1
        return current


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
            _refresh_registry()
            server = MeshServer(
                host,
                port,
                key,
                _node_id,
                _collect_local,
                allowlist,
                search_handler=_invoke_search_handler,
                capabilities=_capabilities(),
                engines=_engines(),
            )
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


def search(
    query: str,
    *,
    categories: t.Iterable[str] | None = None,
    language: str = "all",
    safesearch: int = 0,
    pageno: int = 1,
    max_results: int | None = None,
) -> dict[str, t.Any]:
    """Federate ``query`` across healthy peers; returns ``{results, count, peers, errors}``.

    Never raises: on any failure it returns an empty (but well-shaped) report.
    """
    report: dict[str, t.Any] = {"results": [], "count": 0, "peers": [], "errors": {}}
    try:
        text = str(query or "").strip()
        if not text or not is_enabled():
            return report
        key = _load_key()
        if key is None:
            return report
        try:
            limit = (
                int(max_results)
                if max_results is not None
                else int(_get("search.nextgen.mesh.max_results", _DEFAULT_MAX_RESULTS))
            )
        except (TypeError, ValueError):
            limit = _DEFAULT_MAX_RESULTS
        limit = max(1, limit)
        _refresh_registry()
        nodes = _registry.healthy()
        if not nodes:
            return report
        chosen = select(nodes, _strategy(), _fanout(), required_capabilities=_capabilities(), offset=_next_offset())
        if not chosen:
            return report
        node_id = _node_id or _resolve_node_id()
        timeout = _search_timeout()
        responses: list[dict] = []
        errors: dict[str, str] = {}
        for node in chosen:
            peer = Peer(node.node_id, node.host, node.port)
            started = time.monotonic()
            response = send_query(
                peer,
                key,
                node_id,
                text,
                categories=categories,
                language=language,
                safesearch=safesearch,
                pageno=pageno,
                max_results=limit,
                timeout=timeout,
            )
            if response:
                node.observe_success(time.monotonic() - started)
                responses.append({"node_id": node.node_id, "results": response.get("results", [])})
                if response.get("error"):
                    errors[node.node_id] = str(response["error"])
            else:
                node.observe_failure()
                errors[node.node_id] = "unreachable"
        merged = aggregate(responses, max_results=limit)
        report["results"] = merged.get("results", [])
        report["count"] = int(merged.get("count", 0) or 0)
        report["peers"] = [node.node_id for node in chosen]
        report["errors"] = errors
        with _lock:
            _stats["queries"] += 1
            _stats["results"] += report["count"]
            _stats["search_errors"] += len(errors)
        return report
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh search failed: %s", e)
        return report


def reset() -> None:
    """Stop everything and clear injected handlers (tests)."""
    global _source, _sink, _search_handler, _node_id, _rr_offset  # pylint: disable=global-statement
    try:
        stop()
        with _lock:
            _source = None
            _sink = None
            _search_handler = None
            _node_id = ""
            _rr_offset = 0
            _registry.clear()
            _stats.update(
                peers=0,
                sent=0,
                received=0,
                applied=0,
                errors=0,
                queries=0,
                results=0,
                search_errors=0,
            )
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh reset failed: %s", e)
