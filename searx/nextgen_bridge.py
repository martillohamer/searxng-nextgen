# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-pillar integration layer (Phase C).

This module is the **only** place allowed to couple independent pillars (none of
them may import another).  It wires, fail-safe:

* **graph ⇄ mesh**: the graph's anonymous fragments become the mesh fragment
  source, and verified remote fragments reinforce the local graph.
- **mesh ⇄ search**: the mesh search handler is wired to the local search stack
  so a peer's query can be answered without the mesh pillar importing it.
- **anticipate ⇄ memory/graph**: the anticipation cache's *warmer* — the callback
  that precomputes the (embedding, context) of the predicted next queries from
  the memory + knowledge-graph pillars — is owned here.
- **stealth** (Martillohamer Sear Edition): installs a shared write guard on the
  app-owned persistent stores (graph / memory / anticipate) and registers purge
  hooks for the volatile caches (result cache, metrics).  While a
  :py:class:`searx.stealth.StealthContext` is active, those writes are buffered in
  RAM and everything is wiped on exit.

Everything here is best-effort: a failure degrades to "not wired".
"""

__all__ = [
    "anticipate_predict",
    "anticipate_recall",
    "stealth_session",
    "stop",
    "wire",
    "wire_anticipate",
    "wire_stealth",
]

import contextlib
import threading
import typing as t

from searx import logger

log = logger.getChild("searx.nextgen_bridge")

_lock = threading.Lock()
_wired = False
_stealth_wired = False
_anticipate_wired = False


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
                    mesh.set_search_handler(_local_search_handler)
                    mesh_ok = bool(mesh.start())
                    _wired = mesh_ok
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: wire failed: %s", e)
    stealth_ok = wire_stealth()
    anticipate_ok = wire_anticipate()
    return mesh_ok or stealth_ok or anticipate_ok


def wire_anticipate() -> bool:
    """Register the anticipation warmer (memory + graph).  Idempotent, best effort."""
    global _anticipate_wired  # pylint: disable=global-statement
    try:
        from searx import anticipate

        if not anticipate.is_enabled():
            return False
        with _lock:
            if not _anticipate_wired:
                anticipate.set_warmer(_anticipate_warmer)
                _anticipate_wired = True
            return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: wire_anticipate failed: %s", e)
        return False


def _anticipate_warmer(query: str) -> tuple[list[float], str | None] | None:
    """Precompute the ``(embedding, context)`` for ``query`` from memory + graph.

    This is the *only* place where the anticipation pillar meets the memory and
    knowledge-graph pillars.  Returns ``None`` when no embedding is available.
    """
    try:
        from searx.memory import recall as memory_recall

        memory_block, vector = memory_recall(query)
        if not vector:
            return None
        graph_block = None
        try:
            from searx import graph

            graph_block = graph.recall(query)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("nextgen bridge: anticipate graph recall failed: %s", e)
        context_block = "\n\n".join(part for part in (memory_block, graph_block) if part) or None
        return vector, context_block
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: anticipate warmer failed: %s", e)
        return None


def anticipate_recall(query: str) -> tuple[list[float], str | None] | None:
    """Return the warmed ``(embedding, context)`` for ``query`` or ``None``.  Fail-safe."""
    try:
        from searx import anticipate

        return anticipate.recall(query)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: anticipate_recall failed: %s", e)
        return None


def anticipate_predict(query: str, limit: int | None = None) -> list[str]:
    """Return the most likely next queries after ``query`` (``[]``).  Fail-safe."""
    try:
        from searx import anticipate

        return anticipate.predict(query, limit)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: anticipate_predict failed: %s", e)
        return []


def _mesh_search_engines() -> list[str]:
    """Names of the engines a peer query may use (``mesh.engines``, else general)."""
    try:
        from searx import settings

        mesh = settings.get("search", {}).get("nextgen", {}).get("mesh", {}) or {}
        configured = mesh.get("engines")
        if isinstance(configured, list) and configured:
            return [str(engine) for engine in configured if str(engine)]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: mesh engines setting failed: %s", e)
    try:
        from searx import engines as searx_engines

        names = []
        for name, engine in (getattr(searx_engines, "engines", {}) or {}).items():
            if "general" in (getattr(engine, "categories", None) or []):
                names.append(str(name))
        return names
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: mesh engine discovery failed: %s", e)
        return []


def _serialize_result(result: t.Any) -> dict[str, str]:
    """Reduce a search result object to the mesh wire shape (``title/url/content/engine``)."""
    data = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    engines = data.get("engines")
    if isinstance(engines, (list, tuple, set)):
        engine = ",".join(str(entry) for entry in engines)
    else:
        engine = str(data.get("engine", "") or "")
    return {
        "title": str(data.get("title", "") or ""),
        "url": str(data.get("url", "") or ""),
        "content": str(data.get("content", "") or ""),
        "engine": engine,
    }


def _local_search_handler(query: str, opts: dict[str, t.Any]) -> dict[str, t.Any]:
    """Answer a mesh peer's query with a local search (fail-safe)."""
    try:
        import importlib

        webapp = importlib.import_module("searx.webapp")
        from searx.search import Search
        from searx.search.models import EngineRef, SearchQuery

        names = _mesh_search_engines()
        if not names:
            return {"results": [], "error": "no_engines"}
        search_query = SearchQuery(
            str(query),
            [EngineRef(name, "general") for name in names],
            lang=str(opts.get("language", "all") or "all"),
            safesearch=opts.get("safesearch", 0),
            pageno=int(opts.get("pageno", 1) or 1),
        )
        with webapp.app.app_context(), webapp.app.test_request_context("/search"):
            container = Search(search_query).search()
            results = [_serialize_result(result) for result in container.get_ordered_results()]
        try:
            limit = max(1, int(opts.get("max_results", len(results) or 1) or len(results) or 1))
        except (TypeError, ValueError):
            limit = len(results) or 1
        results = results[:limit]
        return {"results": results, "count": len(results)}
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: local mesh search failed: %s", e)
        return {"results": [], "error": "unavailable"}


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
    """Tear down the mesh, stealth and anticipation integration (tests / shutdown)."""
    global _wired, _stealth_wired, _anticipate_wired  # pylint: disable=global-statement
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
    try:
        from searx import anticipate

        anticipate.reset()
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen bridge: anticipate reset failed: %s", e)
    with _lock:
        _wired = False
        _stealth_wired = False
        _anticipate_wired = False
