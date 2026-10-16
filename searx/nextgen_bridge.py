# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-pillar integration layer (Phase C).

This module is the **only** place allowed to couple independent pillars (none of
them may import another).  It wires, fail-safe:

* **graph ⇄ mesh**: the graph's anonymous fragments become the mesh fragment
  source, and verified remote fragments reinforce the local graph.
- **stealth** (Martillohamer Sear Edition): installs a shared write guard on the
  app-owned persistent stores (graph / memory / anticipate) and registers purge
  hooks for the volatile caches (result cache, metrics).  While a
  :py:class:`searx.stealth.StealthContext` is active, those writes are buffered in
  RAM and everything is wiped on exit.

Everything here is best-effort: a failure degrades to "not wired".
"""

__all__ = ["stealth_session", "stop", "wire", "wire_stealth"]

import contextlib
import threading
import typing as t

from searx import logger

log = logger.getChild("searx.nextgen_bridge")

_lock = threading.Lock()
_wired = False
_stealth_wired = False


def wire() -> bool:
    """Connect the graph and the mesh, and install stealth hooks.  Best effort."""
    global _wired  # pylint: disable=global-statement
    mesh_ok = False
    try:
        from searx import graph, mesh

        if graph.is_enabled() and mesh.is_enabled():
            with _lock:
                if _wired:
                    mesh_ok = True
                else:
                    mesh.set_fragment_source(graph.export_fragments)
                    mesh.on_fragment(graph.import_fragment)
                    mesh_ok = bool(mesh.start())
                    _wired = mesh_ok
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: wire failed: %s", e)
    stealth_ok = wire_stealth()
    return mesh_ok or stealth_ok


def wire_stealth() -> bool:
    """Install the stealth write-guard and purge hooks when the pillar is enabled."""
    global _stealth_wired  # pylint: disable=global-statement
    try:
        from searx import stealth

        if not stealth.is_enabled():
            return False
        with _lock:
            guard = stealth.suppress
            from searx.graph.store import get_store as _graph_store
            from searx.memory.store import get_store as _memory_store
            from searx.anticipate.store import get_store as _anticipate_store

            graph_store = _graph_store()
            if graph_store is not None:
                graph_store.set_write_guard(guard)
            memory_store = _memory_store()
            if memory_store is not None:
                memory_store.set_write_guard(guard)
            anticipate_store = _anticipate_store()
            if anticipate_store is not None:
                anticipate_store.set_write_guard(guard)
            _register_stealth_purges()
            _stealth_wired = True
            return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: wire_stealth failed: %s", e)
        return False


def _register_stealth_purges() -> None:
    """Register cache/metrics wipes to run on each stealth purge."""
    from searx import stealth

    stealth.register_purge(_purge_result_cache)
    stealth.register_purge(_purge_metrics)


def _purge_result_cache() -> None:
    try:
        from searx.resultcache import reset_result_cache

        reset_result_cache()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: purge result cache failed: %s", e)


def _purge_metrics() -> None:
    try:
        from searx import metrics

        for storage in (metrics.counter_storage, metrics.histogram_storage):
            clear = getattr(storage, "clear", None)
            if callable(clear):
                clear()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: purge metrics failed: %s", e)


@contextlib.contextmanager
def stealth_session(namespace: str = "nextgen") -> t.Iterator[bool]:
    """Run the enclosed block in amnesic mode when stealth is enabled.

    Yields ``True`` when stealth is active for the block, ``False`` when the
    pillar is off (in which case the block runs normally).
    """
    try:
        from searx import stealth

        if not stealth.is_enabled():
            yield False
            return
        with stealth.StealthContext(namespace):
            yield True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: stealth_session failed: %s", e)
        yield False


def stop() -> None:
    """Tear down the mesh and stealth integration (tests / shutdown)."""
    global _wired, _stealth_wired  # pylint: disable=global-statement
    try:
        from searx import mesh

        mesh.stop()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: stop failed: %s", e)
    try:
        from searx import stealth

        stealth.reset()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: stealth reset failed: %s", e)
    with _lock:
        _wired = False
        _stealth_wired = False
