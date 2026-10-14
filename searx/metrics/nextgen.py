# SPDX-License-Identifier: AGPL-3.0-or-later
"""NextGen observability metrics (cache, summarizer and post-processing).

This module is a thin, **fail-safe** layer on top of the existing
:py:mod:`searx.metrics` subsystem.  It does not create its own storage: it
reuses the :py:obj:`CounterStorage <searx.metrics.models.CounterStorage>` and
:py:obj:`HistogramStorage <searx.metrics.models.HistogramStorage>` instances
(thread-safe, in-memory, per-process) and their *Void* counterparts used when
metrics are disabled.

To avoid a parent/child import cycle, this module never imports
:py:mod:`searx.metrics`: the storages are handed over by
:py:func:`searx.metrics.initialize` through :py:func:`configure`, and kept in
module-level references.

Design notes:

- Only **aggregate** values are recorded: no query text, URLs or content.
- Every helper swallows its exceptions (``try``/``except``) so that metrics can
  never break the search flow.
- :py:func:`configure` must run from :py:func:`searx.metrics.initialize` because
  counters raise ``KeyError`` on unconfigured keys.
"""

__all__ = [
    "configure",
    "incr_counter",
    "observe_histogram",
    "cache_hit",
    "cache_miss",
    "cache_lookup_time",
    "summarizer_run",
    "summarizer_ok",
    "summarizer_fail",
    "summarizer_timeout",
    "postprocess_removed",
    "get_nextgen_stats",
]

import typing as t

from searx import logger

log = logger.getChild("searx.metrics.nextgen")

# Storages injected by searx.metrics.initialize(); ``None`` until then.
_counter_storage: t.Any = None
_histogram_storage: t.Any = None
_enabled: bool = False

# --- metric keys (tuples used by searx.metrics' storages) --------------------

K_CACHE_HIT: tuple[str, ...] = ("nextgen", "cache", "hit")
K_CACHE_MISS: tuple[str, ...] = ("nextgen", "cache", "miss")
K_SUMMARY_RUN: tuple[str, ...] = ("nextgen", "summarizer", "run")
K_SUMMARY_OK: tuple[str, ...] = ("nextgen", "summarizer", "ok")
K_SUMMARY_FAIL: tuple[str, ...] = ("nextgen", "summarizer", "fail")
K_SUMMARY_TIMEOUT: tuple[str, ...] = ("nextgen", "summarizer", "timeout")
K_POSTPROCESS_REMOVED: tuple[str, ...] = ("nextgen", "postprocess", "removed")

H_CACHE_LOOKUP: tuple[str, ...] = ("nextgen", "cache", "lookup_time")
H_SUMMARY_DURATION: tuple[str, ...] = ("nextgen", "summarizer", "duration")

_COUNTER_KEYS: tuple[tuple[str, ...], ...] = (
    K_CACHE_HIT,
    K_CACHE_MISS,
    K_SUMMARY_RUN,
    K_SUMMARY_OK,
    K_SUMMARY_FAIL,
    K_SUMMARY_TIMEOUT,
    K_POSTPROCESS_REMOVED,
)

# cache lookup is recorded in milliseconds
_CACHE_LOOKUP_WIDTH: float = 5.0
_CACHE_LOOKUP_SIZE: int = 400  # 400 * 5ms = 2s upper bound

# summarizer duration is recorded in seconds
_SUMMARY_DURATION_WIDTH: float = 0.5
_SUMMARY_DURATION_SIZE: int = 240  # 240 * 0.5s = 120s upper bound


def configure(counter_storage: t.Any = None, histogram_storage: t.Any = None, enabled: bool | None = None) -> None:
    """Register the NextGen counters/histograms (called from metrics.initialize).

    The storages (and the ``enabled`` flag) are injected by
    :py:func:`searx.metrics.initialize`.  Idempotent: an already configured key is
    left untouched so a second call never wipes collected data.  Fail-safe: any
    error is logged and ignored.
    """
    global _counter_storage, _histogram_storage, _enabled  # pylint: disable=global-statement

    try:
        if counter_storage is not None:
            _counter_storage = counter_storage
        if histogram_storage is not None:
            _histogram_storage = histogram_storage
        if enabled is not None:
            _enabled = bool(enabled)

        cs, hs = _counter_storage, _histogram_storage
        if cs is None or hs is None:
            return

        for key in _COUNTER_KEYS:
            if key not in cs.counters:
                cs.configure(*key)

        if hs.get(*H_CACHE_LOOKUP) is None:
            hs.configure(_CACHE_LOOKUP_WIDTH, _CACHE_LOOKUP_SIZE, *H_CACHE_LOOKUP)
        if hs.get(*H_SUMMARY_DURATION) is None:
            hs.configure(_SUMMARY_DURATION_WIDTH, _SUMMARY_DURATION_SIZE, *H_SUMMARY_DURATION)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen metrics: configure failed: %s", e)


def incr_counter(*keys: str) -> None:
    """Increment the counter ``keys`` by one.  Never raises."""
    try:
        if _counter_storage is not None:
            _counter_storage.add(1, *keys)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen metrics: counter %s failed: %s", keys, e)


def observe_histogram(value: float, *keys: str) -> None:
    """Observe ``value`` in the histogram ``keys``.  Never raises."""
    try:
        if _histogram_storage is not None:
            _histogram_storage.get(*keys).observe(value)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen metrics: histogram %s failed: %s", keys, e)


# --- semantic helpers -------------------------------------------------------


def cache_hit() -> None:
    """Record a result-cache hit."""
    incr_counter(*K_CACHE_HIT)


def cache_miss() -> None:
    """Record a result-cache miss (real search performed)."""
    incr_counter(*K_CACHE_MISS)


def cache_lookup_time(milliseconds: float) -> None:
    """Record the duration of a result-cache lookup, in milliseconds."""
    observe_histogram(max(float(milliseconds), 0.0), *H_CACHE_LOOKUP)


def summarizer_run() -> None:
    """Record a summarizer invocation."""
    incr_counter(*K_SUMMARY_RUN)


def summarizer_ok(seconds: float) -> None:
    """Record a successful summarizer run and its duration (seconds)."""
    incr_counter(*K_SUMMARY_OK)
    observe_histogram(max(float(seconds), 0.0), *H_SUMMARY_DURATION)


def summarizer_fail(seconds: float) -> None:
    """Record a failed summarizer run (error or empty result)."""
    incr_counter(*K_SUMMARY_FAIL)
    observe_histogram(max(float(seconds), 0.0), *H_SUMMARY_DURATION)


def summarizer_timeout(seconds: float) -> None:
    """Record a summarizer timeout (the backend ignored the wall-clock limit)."""
    incr_counter(*K_SUMMARY_TIMEOUT)


def postprocess_removed(count: int) -> None:
    """Record ``count`` results removed by the post-processing pipeline."""
    if count <= 0:
        return
    try:
        if _counter_storage is not None:
            _counter_storage.add(int(count), *K_POSTPROCESS_REMOVED)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen metrics: postprocess counter failed: %s", e)


# --- read / aggregate -------------------------------------------------------


def _counter_value(counter_storage: t.Any, key: tuple[str, ...]) -> int:
    try:
        return int(counter_storage.get(*key))
    except Exception:  # pylint: disable=broad-except
        return 0


def _histogram_stats(histogram_storage: t.Any, key: tuple[str, ...]) -> tuple[float | None, float | None]:
    """Return ``(average, p95)`` for ``key`` or ``(None, None)`` when empty."""
    try:
        histogram = histogram_storage.get(*key)
        if histogram is None or histogram.count == 0:
            return None, None
        average = round(float(histogram.average), 3)
        p95 = histogram.percentage(95)
        return average, (round(float(p95), 3) if p95 is not None else None)
    except Exception:  # pylint: disable=broad-except
        return None, None


def get_nextgen_stats() -> dict[str, t.Any]:
    """Return the aggregated NextGen metrics as a template-friendly dict.

    Defensive by design: with uninitialized storage, disabled metrics or missing
    keys it returns zeros / ``None`` instead of raising.
    """
    stats: dict[str, t.Any] = {
        "enabled": False,
        "cache": {
            "hit": 0,
            "miss": 0,
            "total": 0,
            "hit_ratio": None,
            "lookup_avg_ms": None,
            "lookup_p95_ms": None,
        },
        "summarizer": {
            "run": 0,
            "ok": 0,
            "fail": 0,
            "timeout": 0,
            "duration_avg_s": None,
            "duration_p95_s": None,
        },
        "postprocess": {"removed": 0},
    }

    try:
        cs, hs = _counter_storage, _histogram_storage
        if cs is None or hs is None:
            return stats

        stats["enabled"] = _enabled

        hit = _counter_value(cs, K_CACHE_HIT)
        miss = _counter_value(cs, K_CACHE_MISS)
        total = hit + miss
        stats["cache"]["hit"] = hit
        stats["cache"]["miss"] = miss
        stats["cache"]["total"] = total
        stats["cache"]["hit_ratio"] = round(hit / total, 4) if total else None
        stats["cache"]["lookup_avg_ms"], stats["cache"]["lookup_p95_ms"] = _histogram_stats(hs, H_CACHE_LOOKUP)

        stats["summarizer"]["run"] = _counter_value(cs, K_SUMMARY_RUN)
        stats["summarizer"]["ok"] = _counter_value(cs, K_SUMMARY_OK)
        stats["summarizer"]["fail"] = _counter_value(cs, K_SUMMARY_FAIL)
        stats["summarizer"]["timeout"] = _counter_value(cs, K_SUMMARY_TIMEOUT)
        (stats["summarizer"]["duration_avg_s"], stats["summarizer"]["duration_p95_s"]) = _histogram_stats(
            hs, H_SUMMARY_DURATION
        )

        stats["postprocess"]["removed"] = _counter_value(cs, K_POSTPROCESS_REMOVED)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen metrics: get_nextgen_stats failed: %s", e)

    return stats
