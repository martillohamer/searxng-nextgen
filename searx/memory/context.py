# SPDX-License-Identifier: AGPL-3.0-or-later
"""Semantic context injector for the summarizer (see the ``search.memory`` settings).

:py:func:`recall` embeds the current query, retrieves the ``top_k`` most similar
past ``(query, summary)`` pairs above ``similarity_threshold`` and renders them
as a compact context block for the summarizer.  :py:func:`remember` schedules the
asynchronous storage of a freshly generated summary.

The memory is **opt-in** (``enabled: false`` by default) and **fail-safe**: any
error yields no context / no-op so the search flow is never affected.
"""

__all__ = ["is_enabled", "recall", "remember"]

from searx import get_setting, logger

log = logger.getChild("searx.memory.context")

_DEFAULT_TOP_K: int = 3
_DEFAULT_THRESHOLD: float = 0.82
_MAX_CONTEXT_ENTRY_CHARS: int = 400
_CONTEXT_HEADER: str = "Relevant context from previous similar searches (use only if it helps; ignore if unrelated):"


def is_enabled() -> bool:
    """Return ``True`` when the semantic memory is enabled."""
    try:
        return bool(get_setting("search.memory.enabled", False))
    except Exception:  # pylint: disable=broad-except
        return False


def _format_block(hits: list[tuple[float, str]]) -> str:
    lines = [_CONTEXT_HEADER]
    for _score, summary in hits:
        text = " ".join((summary or "").split())
        if not text:
            continue
        if len(text) > _MAX_CONTEXT_ENTRY_CHARS:
            text = text[:_MAX_CONTEXT_ENTRY_CHARS] + "…"
        lines.append(f"- {text}")
    return "\n".join(lines)


def recall(query: str) -> tuple[str | None, list[float] | None]:
    """Return ``(context_block, query_embedding)`` for ``query``.

    ``context_block`` is ``None`` when the memory is disabled, the query cannot be
    embedded or no past entry is similar enough.  ``query_embedding`` is returned
    even without hits, so the caller can reuse it for :py:func:`remember`.
    """
    try:
        if not is_enabled() or not query or not query.strip():
            return None, None
        from .embedder import embed

        vector = embed(query)
        if vector is None:
            return None, None

        from .store import get_store

        store = get_store()
        if store is None:
            return None, vector

        try:
            top_k = int(get_setting("search.memory.top_k", _DEFAULT_TOP_K) or _DEFAULT_TOP_K)
        except (TypeError, ValueError):
            top_k = _DEFAULT_TOP_K
        try:
            threshold = float(get_setting("search.memory.similarity_threshold", _DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            threshold = _DEFAULT_THRESHOLD

        hits = store.search(vector, top_k, threshold)
        if not hits:
            return None, vector
        return _format_block(hits), vector
    except Exception as e:  # pylint: disable=broad-except
        log.debug("memory recall failed: %s", e)
        return None, None


def remember(query: str, summary: str, vector: list[float] | None = None) -> None:
    """Schedule the asynchronous storage of ``(query, summary)``.

    Reuses ``vector`` when provided, otherwise embeds ``query``.  Fail-safe.
    """
    try:
        if not is_enabled() or not query or not summary:
            return
        if vector is None:
            from .embedder import embed

            vector = embed(query)
        if vector is None:
            return

        from .store import get_store

        store = get_store()
        if store is not None:
            store.enqueue(query, summary, vector)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("memory remember failed: %s", e)
