# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local SQLite store for the neural anticipation cache (Phase C, pillar 3).

Two tables back the anticipation cache:

- ``transition`` — the learned first-order Markov weights between consecutive
  **normalized** queries (``src`` → ``dst``).  Re-seeing a transition reinforces
  its weight; a **rewarded** transition (the prediction was confirmed by the next
  real query) is reinforced by ``reward_boost`` instead of ``1``.  Weights decay
  over time and drop below ``min_weight`` are pruned.
- ``artifact`` — the precomputed local context for a predicted query: the query
  embedding (raw ``array('f')`` blob) plus the assembled memory + knowledge-graph
  context block, keyed by the normalized query.  Artifacts are bounded by a **TTL**
  and pruned FIFO beyond ``max_rows``.

It is local-only (a single SQLite file with WAL) and **fail-safe**: any error is
logged at ``debug`` level and yields an empty result / no-op.  No query text is
ever sent anywhere; only local aggregates and artifacts are stored, and the
query keys are normalized (see :py:func:`searx.nextgen.normalize_key`).
"""

__all__ = ["AnticipateStore", "get_store", "reset_store"]

import array
import os
import sqlite3
import tempfile
import threading
import time
import typing as t

from searx import get_setting, logger
from searx.nextgen import normalize_key

log = logger.getChild("searx.anticipate.store")

_DEFAULT_DB_PATH: str = os.path.join(tempfile.gettempdir(), "searx_anticipate.db")
_DEFAULT_MAX_ROWS: int = 5000
_DEFAULT_TTL: int = 1800
_DEFAULT_WEIGHT_DECAY: float = 0.99
_DEFAULT_DECAY_AFTER: int = 86400
_DEFAULT_MIN_WEIGHT: float = 0.05
_QUERY_MAX_LEN: int = 200
_MAX_DELTA: float = 100.0

_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS transition (
      src     TEXT NOT NULL,
      dst     TEXT NOT NULL,
      weight  REAL NOT NULL DEFAULT 1.0,
      updated REAL NOT NULL,
      PRIMARY KEY (src, dst)
    )""",
    "CREATE INDEX IF NOT EXISTS transition_src_idx ON transition(src)",
    "CREATE INDEX IF NOT EXISTS transition_updated_idx ON transition(updated)",
    """
    CREATE TABLE IF NOT EXISTS artifact (
      query    TEXT PRIMARY KEY,
      model    TEXT NOT NULL DEFAULT '',
      dim      INTEGER NOT NULL,
      vec      BLOB NOT NULL,
      context  TEXT NOT NULL DEFAULT '',
      created  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS artifact_created_idx ON artifact(created)",
)

_SQL_UPSERT_TRANSITION: str = (
    "INSERT INTO transition (src, dst, weight, updated) VALUES (?, ?, ?, ?)"
    "    ON CONFLICT(src, dst) DO UPDATE SET weight = transition.weight + excluded.weight, updated = excluded.updated"
)
_SQL_PREDICT: str = "SELECT dst FROM transition WHERE src = ? ORDER BY weight DESC, updated DESC LIMIT ?"
_SQL_GET_WEIGHT: str = "SELECT weight FROM transition WHERE src = ? AND dst = ?"
_SQL_DECAY_TRANSITION: str = "UPDATE transition SET weight = weight * ? WHERE updated < ?"
_SQL_PRUNE_TRANSITION_LOW: str = "DELETE FROM transition WHERE weight < ?"
_SQL_UPSERT_ARTIFACT: str = (
    "INSERT INTO artifact (query, model, dim, vec, context, created) VALUES (?, ?, ?, ?, ?, ?)"
    "    ON CONFLICT(query) DO UPDATE SET"
    "      model=excluded.model, dim=excluded.dim, vec=excluded.vec,"
    "      context=excluded.context, created=excluded.created"
)
_SQL_GET_ARTIFACT: str = "SELECT model, dim, vec, context, created FROM artifact WHERE query = ?"
_SQL_PURGE_ARTIFACT_TTL: str = "DELETE FROM artifact WHERE created < ?"
_SQL_PURGE_ARTIFACT_FIFO: str = (
    "DELETE FROM artifact WHERE rowid NOT IN (SELECT rowid FROM artifact ORDER BY created DESC LIMIT ?)"
)
_SQL_PURGE_TRANSITION_FIFO: str = (
    "DELETE FROM transition WHERE rowid NOT IN (SELECT rowid FROM transition ORDER BY updated DESC LIMIT ?)"
)


def _clean_query(value: t.Any, limit: int = _QUERY_MAX_LEN) -> str:
    """Normalize and cap a query into its stable, comparable key; never raises."""
    try:
        key = normalize_key(value)
    except Exception:  # pylint: disable=broad-except
        return ""
    return key[:limit]


def _num(value: t.Any, default: float, cast: t.Callable[[t.Any], t.Any]) -> float:
    """Coerce ``value`` with ``cast``; return ``default`` when it is unusable."""
    try:
        return float(cast(value))
    except (TypeError, ValueError):
        return float(default)


def _to_blob(vector: t.Iterable[float]) -> bytes | None:
    """Serialize a float vector to a raw ``array('f')`` blob, or ``None``."""
    try:
        arr = array.array("f", (float(x) for x in vector))
    except (TypeError, ValueError):
        return None
    if not arr:
        return None
    return arr.tobytes()


def _from_blob(blob: bytes, dim: int) -> list[float] | None:
    """Deserialize an ``array('f')`` blob of the expected ``dim``, or ``None``."""
    try:
        arr = array.array("f")
        arr.frombytes(blob)
    except Exception:  # pylint: disable=broad-except
        return None
    if len(arr) != int(dim):
        return None
    return [float(x) for x in arr]


class AnticipateStore:
    """SQLite-backed anticipation store (query transitions + precomputed artifacts)."""

    def __init__(
        self,
        db_path: str = _DEFAULT_DB_PATH,
        max_rows: int = _DEFAULT_MAX_ROWS,
        ttl: int = _DEFAULT_TTL,
        decay: float = _DEFAULT_WEIGHT_DECAY,
        decay_after: int = _DEFAULT_DECAY_AFTER,
        min_weight: float = _DEFAULT_MIN_WEIGHT,
    ) -> None:
        self._db_path: str = db_path or _DEFAULT_DB_PATH
        self._max_rows: int = max(1, int(max_rows))
        self._ttl: int = max(1, int(ttl))
        self._decay: float = min(1.0, max(0.0, float(decay)))
        self._decay_after: int = max(0, int(decay_after))
        self._min_weight: float = max(0.0, float(min_weight))
        self._lock: threading.RLock = threading.RLock()
        self._local: threading.local = threading.local()
        self._guard: t.Callable[[str, t.Any], bool] | None = None

    def set_write_guard(self, guard: t.Callable[[str, t.Any], bool] | None) -> None:
        """Install (or clear) an injected write guard (e.g. ``searx.stealth.suppress``)."""
        self._guard = guard if callable(guard) else None

    def _suppressed(self, payload: t.Any = None) -> bool:
        """Return ``True`` when the injected guard asks to suppress this write."""
        guard = self._guard
        if guard is None:
            return False
        try:
            return bool(guard("anticipate", payload))
        except Exception:  # pylint: disable=broad-except
            return False

    # --- connections --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return a thread-local DB connection (created on first use)."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=3.0)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass
            with conn:
                for statement in _DDL:
                    conn.execute(statement)
            self._local.conn = conn
        return conn

    # --- transitions --------------------------------------------------------

    def record_transition(self, src: str, dst: str, delta: float = 1.0) -> bool:
        """Record (or reinforce) the transition ``src`` → ``dst`` by ``delta``.

        ``delta`` is the reinforcement amount (``>0``, capped at ``_MAX_DELTA``);
        a non-positive/invalid amount is a no-op.  Fail-safe.
        """
        src_key, dst_key = _clean_query(src), _clean_query(dst)
        if not src_key or not dst_key or src_key == dst_key:
            return False
        amount = _num(delta, 0.0, float)
        if amount <= 0.0:
            return False
        amount = min(amount, _MAX_DELTA)
        if self._suppressed((src_key, dst_key)):
            return False
        try:
            conn = self._conn()
            with self._lock, conn:
                conn.execute(_SQL_UPSERT_TRANSITION, (src_key, dst_key, amount, time.time()))
            return True
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: record_transition failed: %s", e)
            return False

    def weight(self, src: str, dst: str) -> float | None:
        """Return the current reinforcement weight of ``src`` → ``dst``, or ``None``."""
        src_key, dst_key = _clean_query(src), _clean_query(dst)
        if not src_key or not dst_key:
            return None
        try:
            conn = self._conn()
            row = conn.execute(_SQL_GET_WEIGHT, (src_key, dst_key)).fetchone()
            return float(row[0]) if row is not None else None
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: weight failed: %s", e)
            return None

    def decay(
        self,
        factor: float | None = None,
        before: float | None = None,
        min_weight: float | None = None,
    ) -> int:
        """Age every transition older than ``before`` and prune the weakest ones.

        Each stale transition's weight is multiplied by ``factor`` (clamped to
        ``[0, 1]``); transitions whose weight then falls below ``min_weight`` are
        removed.  Returns how many transitions were pruned.  Fail-safe.
        """
        amount = self._decay if factor is None else min(1.0, max(0.0, _num(factor, self._decay, float)))
        cutoff = self._num_cutoff(before)
        floor = self._min_weight if min_weight is None else max(0.0, _num(min_weight, self._min_weight, float))
        try:
            conn = self._conn()
            with self._lock, conn:
                conn.execute(_SQL_DECAY_TRANSITION, (amount, cutoff))
                remaining_before = int(conn.execute("SELECT COUNT(*) FROM transition").fetchone()[0])
                conn.execute(_SQL_PRUNE_TRANSITION_LOW, (floor,))
                remaining_after = int(conn.execute("SELECT COUNT(*) FROM transition").fetchone()[0])
            return max(0, remaining_before - remaining_after)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: decay failed: %s", e)
            return 0

    def _num_cutoff(self, before: float | None) -> float:
        return time.time() - self._decay_after if before is None else _num(before, 0.0, float)

    def predict(self, src: str, limit: int = 2) -> list[str]:
        """Return the most likely next queries after ``src``, best first."""
        src_key = _clean_query(src)
        if not src_key:
            return []
        try:
            conn = self._conn()
            rows = conn.execute(_SQL_PREDICT, (src_key, max(1, int(limit)))).fetchall()
            return [str(r[0]) for r in rows]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: predict failed: %s", e)
            return []

    # --- artifacts ----------------------------------------------------------

    def store_artifact(self, query: str, vector: t.Sequence[float], context: str = "", model: str = "") -> bool:
        """Persist the precomputed artifact (embedding + context) for ``query``."""
        query_key = _clean_query(query)
        if not query_key:
            return False
        blob = _to_blob(vector)
        if blob is None:
            return False
        if self._suppressed((query_key, str(context or ""))):
            return False
        try:
            conn = self._conn()
            with self._lock, conn:
                conn.execute(
                    _SQL_UPSERT_ARTIFACT,
                    (
                        query_key,
                        str(model or ""),
                        len(blob) // array.array("f").itemsize,
                        blob,
                        str(context or ""),
                        time.time(),
                    ),
                )
            return True
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: store_artifact failed: %s", e)
            return False

    def get_artifact(self, query: str) -> tuple[list[float], str] | None:
        """Return ``(embedding, context_block)`` for ``query`` if fresh, else ``None``.

        An artifact older than the configured TTL is treated as missing.
        """
        query_key = _clean_query(query)
        if not query_key:
            return None
        try:
            conn = self._conn()
            row = conn.execute(_SQL_GET_ARTIFACT, (query_key,)).fetchone()
            if row is None:
                return None
            _model, dim, blob, context, created = row
            if time.time() - float(created) > self._ttl:
                return None
            vector = _from_blob(blob, int(dim))
            if vector is None:
                return None
            return vector, str(context or "")
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: get_artifact failed: %s", e)
            return None

    def purge(self) -> None:
        """Decay stale transitions, drop expired artifacts and prune both FIFO."""
        self.decay()
        try:
            now = time.time()
            conn = self._conn()
            with self._lock, conn:
                conn.execute(_SQL_PURGE_ARTIFACT_TTL, (now - self._ttl,))
                conn.execute(_SQL_PURGE_ARTIFACT_FIFO, (self._max_rows,))
                conn.execute(_SQL_PURGE_TRANSITION_FIFO, (self._max_rows,))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: purge failed: %s", e)

    # --- introspection ------------------------------------------------------

    def count(self) -> tuple[int, int]:
        """Return ``(transitions, artifacts)`` counts, or zeros."""
        try:
            conn = self._conn()
            transitions = int(conn.execute("SELECT COUNT(*) FROM transition").fetchone()[0])
            artifacts = int(conn.execute("SELECT COUNT(*) FROM artifact").fetchone()[0])
            return transitions, artifacts
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate store: count failed: %s", e)
            return 0, 0

    def close(self) -> None:
        """Close the current thread's connection (best effort)."""
        try:
            conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None
        except Exception:  # pylint: disable=broad-except
            pass


_STORE: AnticipateStore | None = None


def get_store() -> AnticipateStore | None:
    """Return the singleton :py:obj:`AnticipateStore`, building it on first use."""
    global _STORE  # pylint: disable=global-statement
    if _STORE is None:
        try:
            db_path = str(get_setting("search.nextgen.anticipate.db_path", "") or _DEFAULT_DB_PATH)
            max_rows = int(get_setting("search.nextgen.anticipate.max_rows", _DEFAULT_MAX_ROWS) or _DEFAULT_MAX_ROWS)
            ttl = int(get_setting("search.nextgen.anticipate.ttl", _DEFAULT_TTL) or _DEFAULT_TTL)
            decay = _num(
                get_setting("search.nextgen.anticipate.weight_decay", _DEFAULT_WEIGHT_DECAY),
                _DEFAULT_WEIGHT_DECAY,
                float,
            )
            decay_after = int(
                get_setting("search.nextgen.anticipate.decay_after", _DEFAULT_DECAY_AFTER) or _DEFAULT_DECAY_AFTER
            )
            min_weight = _num(
                get_setting("search.nextgen.anticipate.min_weight", _DEFAULT_MIN_WEIGHT), _DEFAULT_MIN_WEIGHT, float
            )
            _STORE = AnticipateStore(db_path, max_rows, ttl, decay, decay_after, min_weight)
        except Exception as e:  # pylint: disable=broad-except
            log.exception("anticipate store: initialization failed: %s", e)
            return None
    return _STORE


def reset_store() -> None:
    """Drop the singleton (mainly useful for tests)."""
    global _STORE  # pylint: disable=global-statement
    store = _STORE
    _STORE = None
    if store is not None:
        store.close()
