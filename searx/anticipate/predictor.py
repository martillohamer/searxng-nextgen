# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightweight first-order Markov predictor over **normalized** search queries.

The predictor is deliberately tiny and stateless on disk: it keeps only the
*last seen query* and its timestamp in process memory, so that two consecutive
searches within ``search.nextgen.anticipate.session_window`` seconds are treated
as a transition ``prev → current``.  Transitions are persisted and queried
through :py:mod:`searx.anticipate.store`.

Everything here is **fail-safe** (never raises into the search flow) and
**local-only** (no external requests).
"""

__all__ = [
    "session_window",
    "top_k_predictions",
    "advance",
    "record_transition",
    "predictions_for",
    "reset",
]

import threading
import time

from searx import get_setting, logger
from searx.nextgen import normalize_key
from searx.anticipate.store import get_store

log = logger.getChild("searx.anticipate.predictor")

_DEFAULT_SESSION_WINDOW: float = 900.0
_DEFAULT_TOP_K: int = 2

_lock: threading.Lock = threading.Lock()
_last_query: str | None = None
_last_ts: float = 0.0


def session_window() -> float:
    """Return the session window (seconds) for chaining two queries."""
    try:
        return max(0.0, float(get_setting("search.nextgen.anticipate.session_window", _DEFAULT_SESSION_WINDOW)))
    except Exception:  # pylint: disable=broad-except
        return _DEFAULT_SESSION_WINDOW


def top_k_predictions() -> int:
    """Return how many next queries to predict (and warm) per search."""
    try:
        return max(1, int(get_setting("search.nextgen.anticipate.top_k_predictions", _DEFAULT_TOP_K) or _DEFAULT_TOP_K))
    except Exception:  # pylint: disable=broad-except
        return _DEFAULT_TOP_K


def advance(query: str) -> tuple[str | None, str]:
    """Normalize ``query``, roll the session state and return ``(prev, current)``.

    ``prev`` is the previous normalized query when it was seen within the session
    window and differs from ``current``; otherwise ``None``.  ``current`` is the
    normalized query (``""`` when ``query`` is empty/blank).
    """
    current = normalize_key(query)
    if not current:
        return None, ""
    global _last_query, _last_ts  # pylint: disable=global-statement
    now = time.monotonic()
    with _lock:
        prev, prev_ts = _last_query, _last_ts
        _last_query, _last_ts = current, now
    if prev and prev != current and (now - prev_ts) <= session_window():
        return prev, current
    return None, current


def record_transition(prev: str, current: str) -> bool:
    """Persist the transition ``prev`` → ``current`` in the store.  Fail-safe."""
    try:
        store = get_store()
        if store is None or not prev or not current or prev == current:
            return False
        return store.record_transition(prev, current)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("anticipate predictor: record_transition failed: %s", e)
        return False


def predictions_for(query: str, limit: int | None = None) -> list[str]:
    """Return the most likely next normalized queries after ``query``."""
    try:
        store = get_store()
        if store is None:
            return []
        return store.predict(normalize_key(query), limit if limit is not None else top_k_predictions())
    except Exception as e:  # pylint: disable=broad-except
        log.debug("anticipate predictor: predictions_for failed: %s", e)
        return []


def reset() -> None:
    """Clear the in-process session state (mainly useful for tests)."""
    global _last_query, _last_ts  # pylint: disable=global-statement
    with _lock:
        _last_query = None
        _last_ts = 0.0
