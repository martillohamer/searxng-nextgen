# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent vector store for the semantic memory (local SQLite, stdlib only).

Design (see the ``search.memory`` settings):

- Pairs ``(query, summary)`` are stored in a local SQLite DB; the query is unique
  (a repeated query updates its summary).
- Query embeddings are stored as raw ``array('f').tobytes()`` blobs and
  **L2-normalized**, so the cosine similarity reduces to a plain dot product.
- A flat in-memory index (``array('f')`` per entry) is built lazily on first use;
  the cosine search runs in pure Python (no NumPy).
- Writes happen on a single background **daemon** thread fed by a thread-safe
  queue, and the store is pruned FIFO when it exceeds ``max_rows``.

Everything is fail-safe: any error is logged at debug level and degrades to an
empty result / no-op, never propagating into the search flow.
"""

__all__ = ["VectorStore", "get_store", "reset_store"]

import array
import math
import os
import queue
import sqlite3
import tempfile
import threading
import time
import typing as t

from searx import get_setting, logger

log = logger.getChild("searx.memory.store")

_DEFAULT_DB_PATH: str = os.path.join(tempfile.gettempdir(), "searx_memory.db")
_DEFAULT_MAX_ROWS: int = 10000

_DDL_TABLE: str = """
CREATE TABLE IF NOT EXISTS memory (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  query    TEXT    NOT NULL UNIQUE,
  summary  TEXT    NOT NULL,
  model    TEXT    NOT NULL DEFAULT '',
  dim      INTEGER NOT NULL,
  vec      BLOB    NOT NULL,
  created  REAL    NOT NULL
)"""
_DDL_INDEX: str = "CREATE INDEX IF NOT EXISTS memory_created_idx ON memory(created)"
_SQL_UPSERT: str = (
    "INSERT INTO memory (query, summary, model, dim, vec, created) VALUES (?, ?, ?, ?, ?, ?)"
    "    ON CONFLICT(query) DO UPDATE SET"
    "      summary=excluded.summary, model=excluded.model,"
    "      dim=excluded.dim, vec=excluded.vec, created=excluded.created"
)
_SQL_PURGE: str = "DELETE FROM memory WHERE id NOT IN (SELECT id FROM memory ORDER BY id DESC LIMIT ?)"


def _normalize(vector: t.Sequence[float]) -> array.array | None:
    """Return the L2-normalized ``array('f')`` of ``vector`` or ``None``."""
    try:
        arr = array.array("f", (float(x) for x in vector))
    except (TypeError, ValueError):
        return None
    if not arr:
        return None
    norm_sq = 0.0
    for x in arr:
        norm_sq += x * x
    if norm_sq <= 0.0 or not math.isfinite(norm_sq):
        return None
    inv = 1.0 / math.sqrt(norm_sq)
    for i in range(len(arr)):
        arr[i] *= inv
    return arr


def _dot(a: array.array, b: array.array) -> float:
    """Dot product of two equal-length normalized vectors (== cosine similarity)."""
    total = 0.0
    for x, y in zip(a, b):
        total += x * y
    return total


class VectorStore:
    """SQLite-backed vector store with an in-memory index and a background writer."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH, max_rows: int = _DEFAULT_MAX_ROWS) -> None:
        self._db_path: str = db_path or _DEFAULT_DB_PATH
        self._max_rows: int = max(1, int(max_rows))
        self._lock: threading.RLock = threading.RLock()
        self._local: threading.local = threading.local()
        self._loaded: bool = False
        self._queries: list[str] = []
        self._summaries: list[str] = []
        self._vectors: list[array.array] = []
        self._model: str = ""
        self._queue: queue.Queue[tuple[str, str, array.array] | None] = queue.Queue()
        self._writer: threading.Thread = threading.Thread(
            target=self._writer_loop, name="searx-memory-writer", daemon=True
        )
        self._writer.start()
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
            return bool(guard("memory", payload))
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
                conn.execute(_DDL_TABLE)
                conn.execute(_DDL_INDEX)
            self._local.conn = conn
        return conn

    def _model_name(self) -> str:
        try:
            return str(get_setting("search.memory.embedding_model", "") or "")
        except Exception:  # pylint: disable=broad-except
            return ""

    # --- lazy in-memory index ----------------------------------------------

    def _load(self) -> None:
        """Load the index from the DB once (idempotent).  Caller may hold the lock."""
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                conn = self._conn()
                rows = conn.execute("SELECT query, summary, model, dim, vec FROM memory ORDER BY id").fetchall()
            except Exception as e:  # pylint: disable=broad-except
                log.debug("memory store: load failed: %s", e)
                return

            queries: list[str] = []
            summaries: list[str] = []
            vectors: list[array.array] = []
            model = ""
            for query, summary, row_model, dim, blob in rows:
                try:
                    arr = array.array("f")
                    arr.frombytes(blob)
                    if len(arr) != int(dim):
                        continue
                    if vectors and len(arr) != len(vectors[0]):
                        continue
                    vectors.append(arr)
                    queries.append(query)
                    summaries.append(summary)
                    if not model:
                        model = row_model
                except Exception:  # pylint: disable=broad-except
                    continue
            self._queries, self._summaries, self._vectors, self._model = queries, summaries, vectors, model

    def _index_put(self, query: str, summary: str, vector: array.array) -> None:
        with self._lock:
            self._load()
            for i in range(len(self._queries) - 1, -1, -1):
                if self._queries[i] == query:
                    del self._queries[i]
                    del self._summaries[i]
                    del self._vectors[i]
            self._queries.append(query)
            self._summaries.append(summary)
            self._vectors.append(vector)

    # --- public API ---------------------------------------------------------

    def search(self, query_vector: t.Sequence[float], top_k: int, threshold: float) -> list[tuple[float, str]]:
        """Return up to ``top_k`` ``(score, summary)`` above ``threshold``, best first."""
        try:
            self._load()
            query = _normalize(query_vector)
            if query is None:
                return []
            limit = max(1, int(top_k))
            results: list[tuple[float, str]] = []
            with self._lock:
                for vector, summary in zip(self._vectors, self._summaries):
                    if len(vector) != len(query):
                        continue
                    score = _dot(query, vector)
                    if score >= threshold:
                        results.append((score, summary))
            results.sort(key=lambda item: item[0], reverse=True)
            return results[:limit]
        except Exception as e:  # pylint: disable=broad-except
            log.debug("memory store: search failed: %s", e)
            return []

    def enqueue(self, query: str, summary: str, vector: t.Sequence[float]) -> None:
        """Schedule an asynchronous ``(query, summary)`` write.  Never raises."""
        try:
            arr = _normalize(vector)
            if arr is None:
                return
            if self._suppressed((str(query), str(summary))):
                return
            self._queue.put((str(query), str(summary), arr))
        except Exception as e:  # pylint: disable=broad-except
            log.debug("memory store: enqueue failed: %s", e)

    def close(self) -> None:
        """Stop the background writer (best effort)."""
        try:
            self._queue.put(None)
        except Exception:  # pylint: disable=broad-except
            pass

    # --- background writer --------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            query, summary, arr = item
            try:
                blob = arr.tobytes()
                conn = self._conn()
                with conn:
                    conn.execute(
                        _SQL_UPSERT,
                        (query, summary, self._model_name(), len(arr), blob, time.time()),
                    )
                self._index_put(query, summary, arr)
                self._purge(conn)
            except Exception as e:  # pylint: disable=broad-except
                log.debug("memory store: write failed: %s", e)

    def _purge(self, conn: sqlite3.Connection) -> None:
        """Prune the store FIFO so it never exceeds ``max_rows``."""
        with self._lock:
            with conn:
                conn.execute(_SQL_PURGE, (self._max_rows,))
            overflow = len(self._queries) - self._max_rows
            if overflow > 0:
                del self._queries[:overflow]
                del self._summaries[:overflow]
                del self._vectors[:overflow]


_STORE: VectorStore | None = None


def get_store() -> VectorStore | None:
    """Return the singleton :py:obj:`VectorStore`, building it on first use."""
    global _STORE  # pylint: disable=global-statement
    if _STORE is None:
        try:
            max_rows = int(get_setting("search.memory.max_rows", _DEFAULT_MAX_ROWS) or _DEFAULT_MAX_ROWS)
            _STORE = VectorStore(_DEFAULT_DB_PATH, max_rows)
        except Exception as e:  # pylint: disable=broad-except
            log.exception("memory store: initialization failed: %s", e)
            return None
    return _STORE


def reset_store() -> None:
    """Drop the singleton (mainly useful for tests)."""
    global _STORE  # pylint: disable=global-statement
    store = _STORE
    _STORE = None
    if store is not None:
        store.close()
