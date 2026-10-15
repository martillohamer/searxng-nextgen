# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entity/relation extraction for the personal knowledge graph.

Primary path: ask the **local** LLM (Ollama, via ``urllib`` only) for a JSON list
of ``(source, relation, target)`` triples.  If the model is unavailable or returns
something unusable, a deterministic **heuristic** extractor takes over so the
graph still grows without any external dependency.

The extractor is **fail-safe**: it never raises and returns an empty list on any
error.  It respects the ``local_only`` gate of the summarizer and reuses its
``base_url``/``timeout``.
"""

__all__ = ["Triple", "extract_triples", "heuristic_triples", "heuristic_entities"]

import json
import re
import typing as t
import urllib.error
import urllib.request
from urllib.parse import urlparse

from searx import get_setting, logger

log = logger.getChild("searx.graph.extractor")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_BASE_URL = "http://127.0.0.1:11434"
_DEFAULT_MODEL = "llama3"
_DEFAULT_TIMEOUT = 8.0
_DEFAULT_MAX_TRIPLES = 20
_MAX_TEXT_CHARS = 2000

_RELATION_FALLBACK = "related_to"

_CAP_SEQ = re.compile(r"\b([A-Z][\w.+/-]*(?:[ \t]+[A-Z][\w.+/-]*){0,3})")
_PHRASE = re.compile(r"\b([A-Z][\w.+/-]+)[ \t]+(de|del|of|for|in|en)[ \t]+([A-Z][\w.+/-]+)")
_TRAILING_PUNCT = ".,;:!?"
_STOPWORDS = frozenset(
    {
        "the", "this", "that", "these", "those", "a", "an", "in", "on", "at", "to", "of", "for",
        "and", "or", "but", "if", "then", "else", "when", "while", "how", "what", "why", "where",
        "who", "which", "it", "its", "is", "are", "was", "were", "be", "been", "being", "do",
        "does", "did", "not", "no", "yes", "we", "you", "they", "he", "she", "i", "as", "by",
        "with", "from", "about", "into", "over", "under", "use", "using", "example",
    }
)


class Triple(t.NamedTuple):
    """A directed ``source -[relation]-> target`` triple."""

    source: str
    relation: str
    target: str


class _ExtractorConfig(t.NamedTuple):
    base_url: str
    model: str
    timeout: float
    local_only: bool
    max_triples: int


def _get(path: str, default: t.Any) -> t.Any:
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def _config() -> _ExtractorConfig:
    base_url = str(_get("search.summarizer.base_url", _DEFAULT_BASE_URL) or _DEFAULT_BASE_URL)
    model = str(_get("search.nextgen.graph.extract_model", "") or "")
    if not model:
        model = str(_get("search.summarizer.model", _DEFAULT_MODEL) or _DEFAULT_MODEL)
    try:
        timeout = float(_get("search.summarizer.timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    local_only = bool(_get("search.summarizer.local_only", True))
    try:
        max_triples = int(_get("search.nextgen.graph.max_triples", _DEFAULT_MAX_TRIPLES) or _DEFAULT_MAX_TRIPLES)
    except (TypeError, ValueError):
        max_triples = _DEFAULT_MAX_TRIPLES
    return _ExtractorConfig(base_url, model, timeout, local_only, max(1, max_triples))


def _is_loopback(base_url: str) -> bool:
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:  # pylint: disable=broad-except
        return False
    return host.lower() in _LOOPBACK_HOSTS


def _build_prompt(query: str, summary: str, max_triples: int) -> str:
    text = f"Query: {query}\n\nSummary: {summary}"[:_MAX_TEXT_CHARS]
    return (
        "Extract knowledge-graph triples from the text below. "
        "Return ONLY minified JSON of the form "
        '{"triples":[{"source":"...","relation":"...","target":"..."}]}. '
        "Use short, canonical entity names and short lowercase relation verbs. "
        f"Return at most {max_triples} triples. No prose, no markdown.\n\n"
        f"{text}"
    )


def _post_blocking(cfg: _ExtractorConfig, prompt: str) -> str | None:
    url = cfg.base_url.rstrip("/") + "/api/generate"
    payload = {"model": cfg.model, "prompt": prompt, "stream": False, "format": "json"}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as resp:  # noqa: S310
            obj = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as e:
        log.debug("graph extractor: LLM request failed: %s", e)
        return None
    if isinstance(obj, dict) and isinstance(obj.get("response"), str):
        return obj["response"]
    return None


def _parse_llm_response(raw: str, max_triples: int) -> list[Triple]:
    text = raw.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # tolerate a fenced/embedded JSON object
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return []

    items: t.Any = data
    if isinstance(data, dict):
        items = data.get("triples") or data.get("relations") or []
    if not isinstance(items, list):
        return []

    triples: list[Triple] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        src = item.get("source") or item.get("s") or item.get("subject")
        rel = item.get("relation") or item.get("r") or item.get("predicate")
        dst = item.get("target") or item.get("t") or item.get("object")
        if isinstance(src, str) and isinstance(rel, str) and isinstance(dst, str):
            if src.strip() and rel.strip() and dst.strip():
                triples.append(Triple(src.strip(), rel.strip(), dst.strip()))
        if len(triples) >= max_triples:
            break
    return triples


def _llm_triples(cfg: _ExtractorConfig, query: str, summary: str) -> list[Triple]:
    if cfg.local_only and not _is_loopback(cfg.base_url):
        log.debug("graph extractor: local_only rejects base_url %r", cfg.base_url)
        return []
    raw = _post_blocking(cfg, _build_prompt(query, summary, cfg.max_triples))
    if raw is None:
        return []
    return _parse_llm_response(raw, cfg.max_triples)


def _dedup(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip().rstrip(_TRAILING_PUNCT)
        key = cleaned.lower()
        if not key or key in seen or key in _STOPWORDS:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def heuristic_entities(text: str, max_entities: int = 12) -> list[str]:
    """Return candidate proper-noun entity phrases found in ``text`` (deterministic)."""
    try:
        if not text:
            return []
        sample = str(text)[:_MAX_TEXT_CHARS]
        phrase_entities = [f"{m.group(1)} {m.group(2)} {m.group(3)}" for m in _PHRASE.finditer(sample)]
        return _dedup([*phrase_entities, *_CAP_SEQ.findall(sample)])[:max_entities]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph extractor: heuristic_entities failed: %s", e)
        return []


def heuristic_triples(query: str, summary: str, max_triples: int = _DEFAULT_MAX_TRIPLES) -> list[Triple]:
    """Deterministic fallback extractor (proper-noun phrases + phrasal patterns)."""
    triples: list[Triple] = []
    try:
        text = f"{query}\n{summary}"[:_MAX_TEXT_CHARS]
        entities = heuristic_entities(text)
        for m in _PHRASE.finditer(text):
            src, verb, dst = m.group(1).strip(), m.group(2).strip().lower(), m.group(3).strip()
            if src and dst and src.lower() != dst.lower():
                triples.append(Triple(src, verb, dst))
            if len(triples) >= max_triples:
                return triples
        for i in range(len(entities) - 1):
            triples.append(Triple(entities[i], _RELATION_FALLBACK, entities[i + 1]))
            if len(triples) >= max_triples:
                break
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph extractor: heuristic failed: %s", e)
    return triples[:max_triples]


def extract_triples(query: str, summary: str) -> list[Triple]:
    """Extract triples from ``(query, summary)``; LLM first, heuristic fallback."""
    try:
        if not query or not summary:
            return []
        cfg = _config()
        triples = _llm_triples(cfg, query, summary)
        if triples:
            return triples
        return heuristic_triples(query, summary, cfg.max_triples)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("graph extractor failed: %s", e)
        return []
