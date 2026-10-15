# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 3 — Neural Anticipation Cache (Phase C).

The anticipation cache learns which queries tend to follow which, from the local
history of **normalized** consecutive queries, and precomputes — **in the
background, locally and without ever querying an external search engine** — the
context the next search will need: the query embedding plus the assembled
memory + knowledge-graph context block.

Life-cycle::

    from searx import anticipate

    anticipate.set_warmer(warm_fn)   # warm_fn(query) -> (embedding, context) | None
    anticipate.observe(query)        # after a successful search: learn + warm (async)
    result = anticipate.recall(query)  # (embedding, context) | None, on the request

The pillar is **opt-in** (``search.nextgen.enabled`` + ``search.nextgen.anticipate.enabled``),
**fail-safe** (every entry point degrades to a no-op / ``None``) and respects the
master-spec rule that a **pillar never imports another pillar**: the expensive
context computation is injected here as a *warmer* callback by the integration
layer (:py:mod:`searx.summarizer`), which legitimately knows both
:py:mod:`searx.memory` and :py:mod:`searx.graph`.

Submodules:

- :py:mod:`searx.anticipate.store` — SQLite storage (transitions + artifacts).
- :py:mod:`searx.anticipate.predictor` — Markov predictor over query history.
"""

__all__ = ["is_enabled", "set_warmer", "observe", "store_artifact", "recall", "reset", "reset_store"]

import queue
import threading
import typing as t

from searx import logger
from searx.nextgen import is_enabled as _nextgen_enabled

from .predictor import advance, predictions_for, record_transition, reset as _reset_session
from .store import get_store, reset_store

log = logger.getChild("searx.anticipate")

#: A warmed artifact: the query embedding plus the assembled context block.
WarmerResult = t.Tuple[t.List[float], t.Optional[str]]
#: Computes the local context for a query, or ``None`` when nothing can be warmed.
Warmer = t.Callable[[str], t.Optional[WarmerResult]]

_warmer_lock: threading.Lock = threading.Lock()
_warmer: Warmer | None = None

_worker_lock: threading.Lock = threading.Lock()
_queue: "queue.Queue[tuple[str | None, str] | None]" = queue.Queue()
_worker: threading.Thread | None = None


def is_enabled() -> bool:
    """Return ``True`` when the anticipation cache is enabled."""
    return _nextgen_enabled("anticipate")


def set_warmer(warmer: Warmer | None) -> None:
    """Register the callback used to precompute (embedding, context) for a query."""
    global _warmer  # pylint: disable=global-statement
    with _warmer_lock:
        _warmer = warmer


# --- background worker ------------------------------------------------------


def _ensure_worker() -> None:
    global _worker  # pylint: disable=global-statement
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="searx-anticipate-worker", daemon=True)
        _worker.start()


def _warm(query: str, store: t.Any) -> None:
    """Precompute and persist the artifact for ``query`` using the warmer callback."""
    with _warmer_lock:
        warmer = _warmer
    if warmer is None:
        return
    try:
        result = warmer(query)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("anticipate warm failed: %s", e)
        return
    if not result:
        return
    vector, context = result
    if not vector:
        return
    store.store_artifact(query, vector, context or "")


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        if item is None:
            return
        store = get_store()
        if store is None:
            continue
        prev, current = item
        try:
            if prev:
                record_transition(prev, current)
            for predicted in predictions_for(current):
                _warm(predicted, store)
            store.purge()
        except Exception as e:  # pylint: disable=broad-except
            log.debug("anticipate worker failed: %s", e)


# --- public API -------------------------------------------------------------


def observe(query: str) -> None:
    """Record ``query`` in the query history and warm the predicted next queries.

    Runs entirely on a daemon thread: the request is never blocked.  Fail-safe.
    """
    try:
        if not is_enabled() or not query:
            return
        prev, current = advance(query)
        if not current:
            return
        _ensure_worker()
        _queue.put((prev, current))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("anticipate observe failed: %s", e)


def store_artifact(query: str, vector: t.Sequence[float], context: str | None = None) -> bool:
    """Persist the artifact ``(vector, context)`` for ``query``.  Fail-safe."""
    try:
        if not is_enabled() or not query or vector is None:
            return False
        store = get_store()
        if store is None:
            return False
        return store.store_artifact(query, vector, context or "")
    except Exception as e:  # pylint: disable=broad-except
        log.debug("anticipate store_artifact failed: %s", e)
        return False


def recall(query: str) -> WarmerResult | None:
    """Return the precomputed ``(embedding, context_block)`` for ``query`` or ``None``.

    ``context_block`` is ``None`` when the artifact holds no context.
    """
    try:
        if not is_enabled() or not query:
            return None
        store = get_store()
        if store is None:
            return None
        row = store.get_artifact(query)
        if row is None:
            return None
        vector, context = row
        return vector, (context or None)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("anticipate recall failed: %s", e)
        return None


def reset() -> None:
    """Stop the worker, drain the queue and clear the session state (tests)."""
    global _worker  # pylint: disable=global-statement
    try:
        _queue.put(None)
    except Exception:  # pylint: disable=broad-except
        pass
    _worker = None
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break
        except Exception:  # pylint: disable=broad-except
            break
    _reset_session()
