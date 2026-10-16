# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amnesic ("ghost") mode for the Martillohamer Sear edition (Phase C pillar 5).

Scope of the guarantee (realistic, application level)
-----------------------------------------------------

While a :py:obj:`StealthContext` is active, the application's **own** persistence
layers consult :py:func:`suppress` before writing.  When suppression is on, the
payload is diverted into a bounded, volatile, in-RAM buffer instead of being
written to disk; on exit the buffer is dropped and every registered purge
callback runs (``__exit__``), so the enclosed block leaves no local trace.

This is **not** an OS-level "block every write" sandbox: it never touches the
filesystem or the kernel.  Wiring of the persistence layers is done by the
integration layer (:py:mod:`searx.nextgen_bridge`) via the injected
:py:meth:`set_write_guard` hook, keeping the pillar-isolation rule intact (this
module imports no other pillar).

Everything here is **fail-safe**: any error degrades to "inactive" / no
suppression and is logged at ``debug`` level only.
"""

__all__ = [
    "ASSISTANT_STATE_NS",
    "StealthContext",
    "activate",
    "buffered",
    "deactivate",
    "drain",
    "is_active",
    "is_enabled",
    "purge",
    "register_purge",
    "reset",
    "suppress",
]

import threading
import typing as t

from searx import get_setting, logger

log = logger.getChild("searx.stealth")

#: Default namespace used for buffered, suppressed writes.
ASSISTANT_STATE_NS: str = "stealth"

_MAX_BUFFER_PER_NAMESPACE: int = 1000

_lock: threading.RLock = threading.RLock()
_depth: int = 0
_buffers: dict[str, list[t.Any]] = {}
_purges: list[t.Callable[[], t.Any]] = []


def _get(path: str, default: t.Any) -> t.Any:
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def is_enabled() -> bool:
    """Return ``True`` when stealth mode is configured on (opt-in, three gates)."""
    try:
        if not bool(_get("search.nextgen.enabled", False)):
            return False
        if not bool(_get("search.nextgen.martillohamer_sear.enabled", False)):
            return False
        return bool(_get("search.nextgen.martillohamer_sear.stealth_mode", False))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth is_enabled failed: %s", e)
        return False


def is_active() -> bool:
    """Return ``True`` while at least one :py:obj:`StealthContext` is open."""
    try:
        with _lock:
            return _depth > 0
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth is_active failed: %s", e)
        return False


def activate() -> int:
    """Increment the activation depth and return the new depth (nesting-safe)."""
    global _depth  # pylint: disable=global-statement
    try:
        with _lock:
            _depth += 1
            return _depth
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth activate failed: %s", e)
        return 0


def deactivate() -> int:
    """Decrement the activation depth; at zero, purge all traces.

    Returns the remaining depth.
    """
    global _depth  # pylint: disable=global-statement
    try:
        with _lock:
            if _depth > 0:
                _depth -= 1
            depth = _depth
        if depth == 0:
            purge()
        return depth
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth deactivate failed: %s", e)
        return 0


def suppress(namespace: str, payload: t.Any = None) -> bool:
    """Return ``True`` when a write to ``namespace`` must be suppressed.

    When active, an optional ``payload`` is appended to the volatile in-RAM
    buffer (bounded per namespace) instead of being persisted.  When inactive the
    call is a cheap no-op returning ``False`` so the caller writes normally.
    """
    try:
        with _lock:
            if _depth <= 0:
                return False
            if payload is not None:
                bucket = _buffers.setdefault(str(namespace or ASSISTANT_STATE_NS), [])
                if len(bucket) < _MAX_BUFFER_PER_NAMESPACE:
                    bucket.append(payload)
            return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth suppress failed: %s", e)
        return False


def buffered(namespace: str | None = None) -> list[t.Any]:
    """Return a copy of the volatile buffer (all namespaces, or just one)."""
    try:
        with _lock:
            if namespace is None:
                return [item for bucket in _buffers.values() for item in bucket]
            return list(_buffers.get(str(namespace), []))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth buffered failed: %s", e)
        return []


def drain() -> dict[str, list[t.Any]]:
    """Return the whole buffer as a snapshot and clear it."""
    global _buffers  # pylint: disable=global-statement
    try:
        with _lock:
            snapshot = {name: list(bucket) for name, bucket in _buffers.items()}
            _buffers = {}
            return snapshot
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth drain failed: %s", e)
        return {}


def register_purge(callback: t.Callable[[], t.Any]) -> bool:
    """Register a no-arg callback run on each full purge.  Returns success."""
    try:
        if not callable(callback):
            return False
        with _lock:
            if callback not in _purges:
                _purges.append(callback)
        return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth register_purge failed: %s", e)
        return False


def purge() -> int:
    """Drop the volatile buffer and run every registered purge callback.

    Returns the number of callbacks that completed without raising.
    """
    try:
        with _lock:
            callbacks = list(_purges)
            _buffers.clear()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth purge snapshot failed: %s", e)
        return 0
    done = 0
    for callback in callbacks:
        try:
            callback()
            done += 1
        except Exception as e:  # pylint: disable=broad-except
            log.debug("stealth purge callback failed: %s", e)
    return done


def reset() -> None:
    """Reset depth, buffer and callbacks (mainly useful for tests)."""
    global _depth, _buffers, _purges  # pylint: disable=global-statement
    try:
        with _lock:
            _depth = 0
            _buffers = {}
            _purges = []
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth reset failed: %s", e)


class StealthContext:
    """Context manager enabling amnesic mode for the enclosed block.

    Usage::

        with StealthContext():
            ...  # writes buffered in RAM, purged on exit

    Nesting is reference-counted: only the outermost exit purges.
    """

    def __init__(self, namespace: str = ASSISTANT_STATE_NS) -> None:
        self._namespace: str = str(namespace or ASSISTANT_STATE_NS)
        self._entered: bool = False

    @property
    def namespace(self) -> str:
        """The namespace this context buffers into."""
        return self._namespace

    def __enter__(self) -> "StealthContext":
        activate()
        self._entered = True
        return self

    def __exit__(self, exc_type: t.Any, exc: t.Any, tb: t.Any) -> bool:
        if self._entered:
            self._entered = False
            deactivate()
        return False

    def buffered(self) -> list[t.Any]:
        """Return this context's volatile buffer."""
        return buffered(self._namespace)
