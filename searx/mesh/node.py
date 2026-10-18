# SPDX-License-Identifier: AGPL-3.0-or-later
"""Peer identity and health tracking for the P2P mesh (Phase C - Pillar 4).

A :py:class:`NodeProfile` is the local view of one remote node: where it lives
(``host``/``port``), what it advertises (``capabilities`` / ``engines``) and how
it has been behaving (``last_seen``, an exponentially-weighted moving average of
observed round-trip latency, and a consecutive-failure counter).

A :py:class:`NodeRegistry` keeps the profiles in a thread-safe map and applies
the two cheap, local health rules the router needs: a node is *healthy* while it
has fewer than ``max_failures`` consecutive failures and it has been seen within
``ttl`` seconds.  A node that has never been contacted is optimistically healthy
so that seed/bootstrap nodes get a first chance.

The module is self-contained: it imports **no** other pillar and performs no I/O,
so it is trivially unit-testable.
"""

__all__ = ["NodeProfile", "NodeRegistry", "parse_seed"]

import hashlib
import threading
import time
import typing as t
from dataclasses import dataclass, field

from searx import logger

log = logger.getChild("searx.mesh.node")

DEFAULT_PORT: int = 9876
DEFAULT_MAX_FAILURES: int = 3
DEFAULT_TTL: float = 300.0
_LATENCY_ALPHA: float = 0.3
_ID_LEN: int = 16


def _clock(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def _derive_id(host: str, port: int) -> str:
    """Return a stable local identifier for a seed without an explicit node id."""
    digest = hashlib.sha1(f"{host}:{int(port)}".encode("utf-8")).hexdigest()
    return digest[:_ID_LEN]


@dataclass
class NodeProfile:
    """Local view of one remote mesh node."""

    node_id: str
    host: str
    port: int
    capabilities: frozenset[str] = field(default_factory=frozenset)
    engines: tuple[str, ...] = ()
    last_seen: float = 0.0
    latency_ema: float = 0.0
    failures: int = 0

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self.host, self.port)

    def is_healthy(
        self,
        *,
        now: float | None = None,
        max_failures: int = DEFAULT_MAX_FAILURES,
        ttl: float = DEFAULT_TTL,
    ) -> bool:
        """Return ``True`` while the node is within its failure and freshness budget."""
        try:
            if self.failures >= int(max_failures):
                return False
            if self.last_seen <= 0:
                return True
            return (_clock(now) - self.last_seen) < float(ttl)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh node: is_healthy failed: %s", e)
            return False

    def observe_success(self, latency: float, *, now: float | None = None) -> None:
        """Record a successful interaction and fold ``latency`` into the EMA."""
        try:
            sample = max(0.0, float(latency))
        except (TypeError, ValueError):
            sample = 0.0
        if self.latency_ema <= 0:
            self.latency_ema = sample
        else:
            self.latency_ema = (_LATENCY_ALPHA * sample) + ((1.0 - _LATENCY_ALPHA) * self.latency_ema)
        self.failures = 0
        self.last_seen = _clock(now)

    def observe_failure(self, *, now: float | None = None) -> None:
        """Record a failed interaction (consecutive-failure counter increments)."""
        self.failures += 1
        self.last_seen = _clock(now)


def _split_hostport(text: str, default_port: int) -> tuple[str, int]:
    host = text
    port = int(default_port)
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            host = text[1:end]
            tail = text[end + 1 :]
            if tail.startswith(":") and tail[1:].isdigit():
                port = int(tail[1:])
            return host, port
    head, sep, tail = text.rpartition(":")
    if sep and tail.isdigit():
        return head, int(tail)
    return host, port


def parse_seed(seed: t.Any, *, default_port: int = DEFAULT_PORT) -> NodeProfile | None:
    """Normalize a seed entry (``"host:port"`` or ``{host, port, ...}``) into a profile."""
    try:
        capabilities: frozenset[str] = frozenset()
        if isinstance(seed, dict):
            host = str(seed.get("host", "") or "").strip()
            try:
                port = int(seed.get("port", default_port))
            except (TypeError, ValueError):
                port = int(default_port)
            node_id = str(seed.get("node_id", "") or "").strip()
            raw_caps = seed.get("capabilities") or []
            if isinstance(raw_caps, (list, tuple, set, frozenset)):
                capabilities = _capabilities(raw_caps)
        elif isinstance(seed, str):
            text = seed.strip()
            if not text:
                return None
            host, port = _split_hostport(text, int(default_port))
            node_id = ""
        else:
            return None
        host = host.strip()
        if not host:
            return None
        return NodeProfile(
            node_id=str(node_id).strip() or _derive_id(host, int(port)),
            host=host,
            port=int(port),
            capabilities=capabilities,
        )
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh node: parse_seed failed: %s", e)
        return None


def _capabilities(raw: t.Iterable[t.Any]) -> frozenset[str]:
    return frozenset(str(c) for c in raw if str(c))


class NodeRegistry:
    """Thread-safe map of ``node_id -> NodeProfile`` with local health rules."""

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        ttl: float = DEFAULT_TTL,
    ) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[str, NodeProfile] = {}
        self._max_failures = int(max_failures)
        self._ttl = float(ttl)

    def upsert(self, profile: NodeProfile | None) -> NodeProfile | None:
        """Insert or merge ``profile``; observations on a known node are preserved."""
        if profile is None or not str(profile.node_id or ""):
            return None
        try:
            with self._lock:
                existing = self._nodes.get(profile.node_id)
                if existing is None:
                    self._nodes[profile.node_id] = profile
                    return profile
                if profile.capabilities:
                    existing.capabilities = frozenset(profile.capabilities)
                if profile.engines:
                    existing.engines = tuple(profile.engines)
                if profile.host:
                    existing.host = profile.host
                if profile.port:
                    existing.port = int(profile.port)
                return existing
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh node: upsert failed: %s", e)
            return None

    def get(self, node_id: str) -> NodeProfile | None:
        with self._lock:
            return self._nodes.get(str(node_id))

    def remove(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(str(node_id), None)

    def clear(self) -> None:
        with self._lock:
            self._nodes.clear()

    def all(self) -> list[NodeProfile]:
        with self._lock:
            return list(self._nodes.values())

    def healthy(self, *, now: float | None = None) -> list[NodeProfile]:
        """Return the healthy nodes (fresh and below the failure budget)."""
        try:
            return [
                node for node in self.all() if node.is_healthy(now=now, max_failures=self._max_failures, ttl=self._ttl)
            ]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("mesh node: healthy failed: %s", e)
            return []

    def add_seeds(self, seeds: t.Iterable[t.Any]) -> int:
        """Register bootstrap seeds (``host:port`` strings or dicts); return the count."""
        added = 0
        for seed in seeds or ():
            profile = parse_seed(seed)
            if profile is not None and self.upsert(profile) is not None:
                added += 1
        return added
