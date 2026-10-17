# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orchestrator of the result *summarizer* (see the ``search.summarizer`` settings).

This module bridges the asynchronous :py:meth:`searx.summarizer.base.Summarizer.summarize`
coroutine into SearXNG's synchronous search flow.  The coroutine runs in an
isolated thread with its own event loop and a hard wall-clock timeout, so a slow
or hanging backend can never block the HTTP request or break the search.
"""

__all__ = ["add_summary", "get_summarizer", "build_config"]

import asyncio
import threading
import time
import typing as t

from searx import get_setting, logger
from searx.result_types import Answer
from searx.metrics import nextgen
from searx.memory import recall as memory_recall, remember as memory_remember
from searx import graph as knowledge_graph
from searx import anticipate as anticipation

from .base import Summarizer, SummarizerConfig
from .backends import BACKENDS, DisabledSummarizer

if t.TYPE_CHECKING:
    from searx.results import ResultContainer
    from searx.result_types import MainResult, LegacyResult
    from searx.search.models import SearchQuery

log: t.Any = logger.getChild("searx.summarizer")

_GRACE: float = 1.0
"""Extra seconds added to the wall-clock timeout before giving up on the thread."""


def build_config() -> SummarizerConfig:
    """Build a :py:obj:`SummarizerConfig` from the ``search.summarizer`` settings."""
    raw = get_setting("search.summarizer", {}) or {}
    default = SummarizerConfig()
    return SummarizerConfig(
        enabled=bool(raw.get("enabled", default.enabled)),
        backend=raw.get("backend", default.backend),
        model=raw.get("model", default.model),
        base_url=raw.get("base_url", default.base_url),
        timeout=float(raw.get("timeout", default.timeout)),
        local_only=bool(raw.get("local_only", default.local_only)),
        max_tokens=int(raw.get("max_tokens", default.max_tokens)),
        temperature=float(raw.get("temperature", default.temperature)),
        min_results=int(raw.get("min_results", default.min_results)),
        categories=list(raw.get("categories", default.categories)),
        max_results=int(raw.get("max_results", default.max_results)),
        max_input_chars=int(raw.get("max_input_chars", default.max_input_chars)),
    )


def get_summarizer(config: SummarizerConfig | None = None) -> Summarizer:
    """Return the configured summarizer backend.

    Falls back to :py:obj:`DisabledSummarizer` on an unknown backend or any
    initialization error.
    """
    config = config or build_config()
    backend_cls = BACKENDS.get(config.backend)
    if backend_cls is None:
        log.warning("Unknown summarizer backend %r, using 'disabled'", config.backend)
        backend_cls = DisabledSummarizer
    try:
        return backend_cls(config)
    except Exception as e:  # pylint: disable=broad-except
        log.exception("Failed to initialize summarizer backend %r: %s", config.backend, e)
        return DisabledSummarizer(config)


def _run_summarize(
    summarizer: Summarizer,
    results: "list[MainResult | LegacyResult]",
    query: str,
    timeout: float,
    context: str | None = None,
) -> str | None:
    """Run ``summarizer.summarize`` in an isolated thread with a hard timeout.

    Returns ``None`` if the backend times out, raises or returns a non-string.
    """
    box: dict[str, t.Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(asyncio.wait_for(summarizer.summarize(results, query, context), timeout))
        except Exception as e:  # pylint: disable=broad-except
            box["error"] = e

    started = time.perf_counter()
    nextgen.summarizer_run()

    thread = threading.Thread(target=_target, name="summarizer", daemon=True)
    thread.start()
    thread.join(timeout + _GRACE)

    elapsed = time.perf_counter() - started

    if thread.is_alive():
        # the coroutine ignored the timeout (or is stuck in blocking I/O):
        # abandon the daemon thread and fail safe.
        log.warning("summarizer exceeded wall-clock timeout (%.1fs)", timeout + _GRACE)
        nextgen.summarizer_timeout(elapsed)
        return None

    if "error" in box:
        log.debug("summarizer backend error: %s", box["error"])
        nextgen.summarizer_fail(elapsed)
        return None

    value = box.get("value")
    if isinstance(value, str) and value.strip():
        nextgen.summarizer_ok(elapsed)
        return value

    nextgen.summarizer_fail(elapsed)
    return None


def _category_matches(config: SummarizerConfig, search_query: "SearchQuery") -> bool:
    if not config.categories:
        return True
    try:
        query_categories = set(search_query.categories or [])
    except Exception:  # pylint: disable=broad-except
        return False
    return bool(query_categories & set(config.categories))


def add_summary(container: "ResultContainer", search_query: "SearchQuery") -> None:
    """Generate a summary (if enabled) and add it as an :py:obj:`Answer`.

    The function is fail-safe: any error is caught and logged, and never
    propagates into the search flow.
    """
    try:
        config = build_config()
        if not config.enabled:
            return

        results = container.get_ordered_results()
        if len(results) < config.min_results:
            return

        if not _category_matches(config, search_query):
            return

        summarizer = get_summarizer(config)
        artifact = anticipation.recall(search_query.query)
        if artifact is not None:
            query_vector, context_block = artifact
        else:
            memory_block, query_vector = memory_recall(search_query.query)
            graph_block = knowledge_graph.recall(search_query.query)
            context_block = "\n\n".join(part for part in (memory_block, graph_block) if part) or None
        text = _run_summarize(summarizer, results, search_query.query, config.timeout, context_block)
        if not text:
            return

        container.answers.add(
            Answer(answer=text, template="answer/summary.html", engine="summarizer")
        )
        memory_remember(search_query.query, text, query_vector)
        knowledge_graph.remember(search_query.query, text)
        anticipation.observe(search_query.query)
    except Exception as e:  # pylint: disable=broad-except
        log.exception("summarizer failed: %s", e)
