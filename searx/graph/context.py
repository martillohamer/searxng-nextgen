# SPDX-License-Identifier: AGPL-3.0-or-later
"""Knowledge-graph context injector and background ingestion worker.

Two responsibilities:

- :py:func:`recall` — given a new query, find the entities it mentions in the
  local graph, gather their 1-hop relations and the **user projects** they are
  linked to, and render a compact context block for the summarizer.  This is what
  "crosses" new entities with the user's projects.
- :py:func:`remember` — schedule the asynchronous extraction of
  ``(query, summary)`` triples.  All work happens on a single daemon thread fed by
  a queue, so search latency is never affected.

The graph is **opt-in** (``search.nextgen.enabled`` + ``search.nextgen.graph.enabled``)
and **fail-safe**: every entry point degrades to "no context" / no-op.
"""

__all__ = ["is_enabled", "recall", "remember", "add_project", "export_fragments", "import_fragment", "reset"]

import queue
import threading
import typing as t

from searx import get_setting, logger
from searx.nextgen import is_enabled as _nextgen_enabled
from searx.graph.extractor import extract_triples, heuristic_entities
from searx.graph.store import get_store

log = logger.getChild("searx.graph.context")

_DEFAULT_CONTEXT_ENTITIES: int = 5
_DEFAULT_CONTEXT_RELATIONS: int = 6
_HEADER: str = "Known context from your knowledge graph (use only if it helps; ignore if unrelated):"
_PROJECTS_LABEL: str = "Related projects:"

_worker_lock = threading.Lock()
_queue: "queue.Queue[tuple[str, str] | None]" = queue.Queue()
_worker: threading.Thread | None = None


def is_enabled() -> bool:
    """Return ``True`` when the knowledge graph is enabled."""
    return _nextgen_enabled("graph")


def _get_int(path: str, default: int) -> int:
    try:
        return int(get_setting(path, default) or default)
    except Exception:  # pylint: disable=broad-except
        return default


# --- background worker ------------------------------------------------------


def _ensure_worker() -> None:
    global _worker  # pylint: disable=global-statement
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, name="searx-graph-worker", daemon=True)
        _worker.start()


def _worker_loop() -> None:
    while True:
        item = _queue.get()
        if item is None:
            return
        graph_store = get_store()
        if graph_store is None:
            continue
        query, summary = item
        try:
            triples = extract_triples(query, summary)
            if triples:
                graph_store.upsert_triples(triples, source_query=query)
                _autolink_projects(graph_store, [triple.source for triple in triples] + [triple.target for triple in triples])
        except Exception as e:  # pylint: disable=broad-except
            log.debug("graph worker: ingestion failed: %s", e)


def _autolink_projects(graph_store: t.Any, entity_names: t.Iterable[str]) -> None:
    """Link extracted entities to projects whose name/description mentions them."""
    try:
        from searx.nextgen import normalize_key

        projects = graph_store.list_projects(limit=200)
        if not projects:
            return
        seen: set[str] = set()
        for name in entity_names:
            key = normalize_key(name)
            if not key or key in seen:
                continue
            seen.add(key)
            for project_name, project_desc in projects:
                haystack = normalize_key(f"{project_name} {project_desc}")
                if key and key in haystack:
                    graph_store.link_project_entity(project_name, name)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph worker: autolink failed: %s", e)


# --- public API -------------------------------------------------------------


def remember(query: str, summary: str) -> None:
    """Schedule asynchronous triple extraction for ``(query, summary)``.  Fail-safe."""
    try:
        if not is_enabled() or not query or not summary:
            return
        _ensure_worker()
        _queue.put((str(query), str(summary)))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph remember failed: %s", e)


def recall(query: str) -> str | None:
    """Return a graph context block for ``query`` or ``None``.  Fail-safe."""
    try:
        if not is_enabled() or not query or not query.strip():
            return None

        names = heuristic_entities(query)
        if not names:
            return None

        graph_store = get_store()
        if graph_store is None:
            return None

        max_entities = _get_int("search.nextgen.graph.context_entities", _DEFAULT_CONTEXT_ENTITIES)
        max_relations = _get_int("search.nextgen.graph.context_relations", _DEFAULT_CONTEXT_RELATIONS)

        entities = graph_store.find_entities(names, limit=max_entities)
        if not entities:
            return None
        entity_ids = [eid for eid, _name in entities]
        relations = graph_store.neighbors(entity_ids, limit=max_relations)
        projects = graph_store.projects_for_entities(entity_ids, limit=5)

        lines: list[str] = [_HEADER]
        for _eid, name in entities:
            lines.append(f"- {name}")
        for relation, other, _weight in relations:
            lines.append(f"  · {relation}: {other}")
        if projects:
            lines.append(_PROJECTS_LABEL)
            for project_name, _desc, _weight in projects:
                lines.append(f"  · {project_name}")
        if len(lines) == 1:
            return None
        return "\n".join(lines)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph recall failed: %s", e)
        return None


def add_project(name: str, description: str = "", entities: t.Iterable[str] = ()) -> bool:
    """Register a user project and optionally link entities to it.  Fail-safe."""
    try:
        if not name:
            return False
        graph_store = get_store()
        if graph_store is None:
            return False
        if graph_store.add_project(name, description) is None:
            return False
        for entity in entities:
            graph_store.link_project_entity(name, entity)
        return True
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph add_project failed: %s", e)
        return False


def export_fragments(limit: int = 200) -> list[dict]:
    """Return anonymous relation fragments from the local graph (fail-safe)."""
    try:
        graph_store = get_store()
        if graph_store is None:
            return []
        return graph_store.export_fragments(limit)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph context: export_fragments failed: %s", e)
        return []


def import_fragment(fragment: dict) -> int:
    """Reinforce an existing local relation from a fragment (fail-safe)."""
    try:
        graph_store = get_store()
        if graph_store is None:
            return 0
        return graph_store.import_fragment(fragment)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph context: import_fragment failed: %s", e)
        return 0


def reset() -> None:
    """Stop the worker and clear the queue (mainly useful for tests)."""
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
