# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 2 — Personal Cross-Context Knowledge Graph (Phase C).

A local, opt-in knowledge graph that extracts ``(entity, relation, entity)``
triples from completed searches/summaries (local LLM with heuristic fallback) and
crosses the entities a new query mentions with the user's registered **projects**
to enrich the summarizer context.

Public API::

    from searx.graph import is_enabled, recall, remember, add_project

    block = recall(query)              # str | None
    remember(query, summary)           # schedules async ingestion
    add_project("my project", "desc", entities=["Ollama", "SQLite"])

Submodules:

- :py:mod:`searx.graph.store` — SQLite storage (entities, relations, projects).
- :py:mod:`searx.graph.extractor` — LLM + heuristic triple extraction.
- :py:mod:`searx.graph.context` — context injection + background worker.

The subsystem is **fail-safe** and **local-only**; a disabled graph behaves as if
it did not exist.
"""

__all__ = ["is_enabled", "recall", "remember", "add_project", "reset", "reset_store", "Triple"]

from .context import add_project, is_enabled, recall, remember, reset
from .extractor import Triple
from .store import reset_store
