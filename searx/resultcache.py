# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=too-few-public-methods
"""Intelligent hybrid result cache (see the ``search.result_cache`` settings).

This module caches the *engine results* of a standard search, keyed by a
normalized, PII-free hash of the query parameters.  On a cache hit the
:py:obj:`ResultContainer <searx.results.ResultContainer>` is populated
instantly and no network requests are made to the engines.  Everything that
runs after the engine phase (``post_search`` plugins, the post-processing
pipeline and the summarizer) still runs as usual, so cached entries stay
consistent with the current configuration.

Design notes:

- The cache key never contains user data (no IP, headers, cookies or user id).
- Values are stored as a *versioned* and *HMAC-signed* pickle blob.  The HMAC
  (keyed with ``server.secret_key``) is verified before the blob is unpickled,
  so a tampered store cannot trigger arbitrary code execution.
- All backends are fail-safe: any error degrades to a :py:obj:`NullResultCache`.

----

.. autoclass:: CachedResults
   :members:

.. autoclass:: ResultCache
   :members:
"""

__all__ = [
    "CACHE_VERSION",
    "CachedResults",
    "build_cache_key",
    "ResultCache",
    "SQLiteResultCache",
    "ValkeyResultCache",
    "NullResultCache",
    "get_result_cache",
    "load",
    "store",
    "load_into",
]

import abc
import dataclasses
from collections import defaultdict
import hashlib
import hmac
import pickle
import time
import typing as t

from searx import get_setting, logger
from searx.cache import ExpireCacheCfg, ExpireCacheSQLite
from searx.result_types.answer import AnswerSet
from searx.metrics import nextgen

if t.TYPE_CHECKING:
    from searx.results import ResultContainer
    from searx.search.models import SearchQuery
    from searx.result_types import LegacyResult, MainResult
    from searx.result_types.answer import BaseAnswer

log = logger.getChild("resultcache")

CACHE_VERSION: int = 1
"""Version of the cached snapshot format.

Bumping this value invalidates every existing entry (the version is part of the
:py:obj:`build_cache_key` and of the serialized blob header).
"""

_MAGIC: bytes = b"SXC1"
"""Magic prefix of a serialized blob."""

_SIG_LEN: int = hashlib.sha256().digest_size
"""Length of the HMAC-SHA256 signature in bytes."""

_DEFAULT_TTL: int = 600
"""Fallback TTL (seconds) when the setting is missing."""

_MAX_VALUE_LEN: int = 1024 * 1024
"""Max length (bytes) of a serialized snapshot stored in the SQLite backend."""


@dataclasses.dataclass
class CachedResults:
    """A serializable snapshot of the engine results of a standard search."""

    main_results: list["MainResult | LegacyResult"] = dataclasses.field(default_factory=list)
    answers: list["BaseAnswer"] = dataclasses.field(default_factory=list)
    infoboxes: list["LegacyResult"] = dataclasses.field(default_factory=list)
    suggestions: list[str] = dataclasses.field(default_factory=list)
    corrections: list[str] = dataclasses.field(default_factory=list)
    engine_data: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)
    paging: bool = False
    created: float = 0.0


def _normalize_query(query: str) -> str:
    """Collapse whitespace of the query text (stable across trivial variants)."""
    return " ".join((query or "").split())


def build_cache_key(search_query: "SearchQuery") -> str:
    """Build a stable, normalized and PII-free cache key for ``search_query``.

    The key combines the normalized query text, the ordered categories, the
    selected engines (name + category), the language, the safesearch level, the
    time range and the page number.  The result is a plain SHA-256 hex digest
    prefixed with the cache version.
    """
    try:
        categories = sorted(set(search_query.categories))
    except Exception:  # pylint: disable=broad-except
        categories = []
    try:
        engines = sorted((ref.name, ref.category) for ref in search_query.engineref_list)
    except Exception:  # pylint: disable=broad-except
        engines = []

    parts = [
        f"v{CACHE_VERSION}",
        _normalize_query(search_query.query),
        ",".join(categories),
        ";".join(f"{name}:{category}" for name, category in engines),
        str(search_query.lang or ""),
        str(search_query.safesearch),
        str(search_query.time_range or ""),
        str(search_query.pageno or 1),
    ]
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _hmac_key() -> bytes:
    """Return the HMAC key derived from ``server.secret_key``."""
    secret = get_setting("server.secret_key")
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    return secret or b""


def _pack(snapshot: CachedResults) -> bytes:
    """Serialize ``snapshot`` into a versioned, HMAC-signed blob."""
    payload = pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL)
    signature = hmac.new(_hmac_key(), payload, hashlib.sha256).digest()
    return _MAGIC + bytes([CACHE_VERSION]) + signature + payload


def _unpack(blob: bytes | bytearray | memoryview | None) -> CachedResults | None:
    """Deserialize a blob produced by :py:obj:`_pack`, verifying version + HMAC."""
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None
    blob = bytes(blob)
    header_len = len(_MAGIC) + 1 + _SIG_LEN
    if len(blob) < header_len or not blob.startswith(_MAGIC):
        return None
    if blob[len(_MAGIC)] != CACHE_VERSION:
        return None
    signature = blob[len(_MAGIC) + 1 : header_len]
    payload = blob[header_len:]
    expected = hmac.new(_hmac_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        log.warning("result cache: HMAC verification failed, discarding entry")
        return None
    try:
        obj = pickle.loads(payload)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("result cache: failed to unpickle entry: %s", e)
        return None
    if not isinstance(obj, CachedResults):
        return None
    return obj


class ResultCache(abc.ABC):
    """Abstract base class of the result-cache backends."""

    name: str = "base"
    """Name of the backend."""

    @abc.abstractmethod
    def get(self, key: str) -> CachedResults | None:
        """Return the cached snapshot for ``key`` or ``None``."""

    @abc.abstractmethod
    def set(self, key: str, value: CachedResults, ttl: int) -> bool:
        """Store ``value`` under ``key`` for ``ttl`` seconds."""


class NullResultCache(ResultCache):
    """No-op backend used when the cache is disabled or a backend failed."""

    name = "null"

    def get(self, key: str) -> CachedResults | None:
        return None

    def set(self, key: str, value: CachedResults, ttl: int) -> bool:
        return False


class SQLiteResultCache(ResultCache):
    """Result cache backed by :py:obj:`ExpireCacheSQLite`.

    The cache uses the dedicated context (DB table) ``search_results``.  The
    serialized blob is stored as-is (identity ``serialize``/``deserialize``).
    """

    name = "sqlite"
    CTX: str = "search_results"

    class _BlobCache(ExpireCacheSQLite):
        """Expire cache that stores the already serialized blob verbatim."""

        def serialize(self, value: t.Any) -> bytes:
            return value

        def deserialize(self, value: bytes) -> t.Any:
            return value

    def __init__(self, db_url: str = "") -> None:
        cfg = ExpireCacheCfg(name="result_cache", db_url=db_url, MAX_VALUE_LEN=_MAX_VALUE_LEN)
        self._cache = SQLiteResultCache._BlobCache(cfg)

    def get(self, key: str) -> CachedResults | None:
        return _unpack(self._cache.get(key, ctx=self.CTX))

    def set(self, key: str, value: CachedResults, ttl: int) -> bool:
        return bool(self._cache.set(key, _pack(value), expire=ttl, ctx=self.CTX))


class ValkeyResultCache(ResultCache):
    """Distributed result cache backed by a Valkey/Redis DB (``SETEX``/``GET``)."""

    name = "valkey"
    PREFIX: str = "searxng:search_results:"

    def __init__(self, client: t.Any) -> None:
        self._client = client

    def get(self, key: str) -> CachedResults | None:
        try:
            blob = self._client.get(self.PREFIX + key)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("result cache (valkey): GET failed: %s", e)
            return None
        return _unpack(blob)

    def set(self, key: str, value: CachedResults, ttl: int) -> bool:
        try:
            return bool(self._client.setex(self.PREFIX + key, max(1, int(ttl)), _pack(value)))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("result cache (valkey): SETEX failed: %s", e)
            return False


def _build_sqlite() -> ResultCache:
    try:
        return SQLiteResultCache()
    except Exception as e:  # pylint: disable=broad-except
        log.exception("result cache: failed to build the SQLite backend: %s", e)
        return NullResultCache()


def _build_valkey() -> ResultCache | None:
    try:
        from searx import valkeydb

        client = valkeydb.client()
        if client is None:
            valkeydb.initialize()
            client = valkeydb.client()
        if client is None:
            return None
        return ValkeyResultCache(client)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("result cache: failed to build the Valkey backend: %s", e)
        return None


def _build_cache() -> ResultCache:
    if not get_setting("search.result_cache.enabled", True):
        return NullResultCache()

    backend = str(get_setting("search.result_cache.backend", "sqlite") or "sqlite").lower()

    if backend == "valkey":
        return _build_valkey() or _build_sqlite()

    if backend == "auto":
        return _build_valkey() or _build_sqlite()

    if backend not in ("sqlite", "valkey", "auto"):
        log.warning("result cache: unknown backend %r, falling back to sqlite", backend)

    return _build_sqlite()


_CACHE: ResultCache | None = None


def get_result_cache() -> ResultCache:
    """Return the singleton result cache, building it on first use."""
    global _CACHE  # pylint: disable=global-statement
    if _CACHE is None:
        try:
            _CACHE = _build_cache()
        except Exception as e:  # pylint: disable=broad-except
            log.exception("result cache: initialization failed: %s", e)
            _CACHE = NullResultCache()
    return _CACHE


def reset_result_cache() -> None:
    """Drop the cached singleton (mainly useful for tests / after config reload)."""
    global _CACHE  # pylint: disable=global-statement
    _CACHE = None


def _make_snapshot(container: "ResultContainer") -> CachedResults:
    return CachedResults(
        main_results=list(container.main_results_map.values()),
        answers=list(container.answers),
        infoboxes=list(container.infoboxes),
        suggestions=sorted(container.suggestions),
        corrections=sorted(container.corrections),
        engine_data={name: dict(data) for name, data in container.engine_data.items()},
        paging=bool(container.paging),
        created=time.time(),
    )


def _restore(container: "ResultContainer", snapshot: CachedResults) -> None:
    """Populate ``container`` from ``snapshot`` without touching the network."""
    container.main_results_map = {hash(result): result for result in snapshot.main_results}
    container.infoboxes = list(snapshot.infoboxes)

    answers = AnswerSet()
    for answer in snapshot.answers:
        answers.add(answer)
    container.answers = answers

    container.suggestions = set(snapshot.suggestions)
    container.corrections = set(snapshot.corrections)
    container.engine_data = defaultdict(dict, {name: dict(data) for name, data in snapshot.engine_data.items()})
    container.paging = bool(snapshot.paging)
    container.from_cache = True


def load(search_query: "SearchQuery") -> CachedResults | None:
    """Return the cached snapshot for ``search_query`` (or ``None``)."""
    try:
        if not get_setting("search.result_cache.enabled", True):
            return None
        return get_result_cache().get(build_cache_key(search_query))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("result cache: load failed: %s", e)
        return None


def store(search_query: "SearchQuery", container: "ResultContainer") -> bool:
    """Store the engine results of ``container`` for ``search_query``.

    Entries are only stored for successful standard searches: a bang redirect or
    an empty container is skipped.
    """
    try:
        if not get_setting("search.result_cache.enabled", True):
            return False
        if container.redirect_url:
            return False
        snapshot = _make_snapshot(container)
        if not (snapshot.main_results or snapshot.answers or snapshot.infoboxes):
            return False
        ttl = int(get_setting("search.result_cache.ttl", _DEFAULT_TTL) or _DEFAULT_TTL)
        return get_result_cache().set(build_cache_key(search_query), snapshot, ttl)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("result cache: store failed: %s", e)
        return False


def load_into(container: "ResultContainer", search_query: "SearchQuery") -> bool:
    """Populate ``container`` from the cache.  Returns ``True`` on a cache hit."""
    if not get_setting("search.result_cache.enabled", True):
        return False
    started = time.perf_counter()
    snapshot = load(search_query)
    nextgen.cache_lookup_time((time.perf_counter() - started) * 1000.0)
    if snapshot is None:
        nextgen.cache_miss()
        return False
    try:
        _restore(container, snapshot)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("result cache: restore failed: %s", e)
        nextgen.cache_miss()
        return False
    nextgen.cache_hit()
    try:
        container.add_timing("cache", max(time.perf_counter() - started, 0.001), 0.0)
    except Exception:  # pylint: disable=broad-except
        pass
    return True
