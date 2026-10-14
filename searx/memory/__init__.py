# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local semantic memory for the next-gen summarizer.

The subsystem is **opt-in** (``search.memory.enabled``, disabled by default) and
fully **fail-safe**: it stores ``(query, summary)`` pairs locally, and on a new
query it recalls the most similar past summaries to enrich the summarizer prompt.

Submodules:

- :py:mod:`searx.memory.embedder` — local text embeddings (Ollama, stdlib only).
- :py:mod:`searx.memory.store` — SQLite vector store with a background writer.
- :py:mod:`searx.memory.context` — recall/inject and remember entry points.

Public API::

    from searx.memory import recall, remember, is_enabled

    block, vector = recall(query)          # (str | None, list[float] | None)
    remember(query, summary, vector)       # schedules an async write
"""

__all__ = ["is_enabled", "recall", "remember", "reset_store"]

from .context import is_enabled, recall, remember
from .store import reset_store
