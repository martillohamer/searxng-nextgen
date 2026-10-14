# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local text embedder for the semantic memory (see the ``search.memory`` settings).

The embedder talks to a **local** inference service (Ollama by default) using
only the Python standard library (``urllib.request``), reusing the ``base_url``,
``local_only`` and ``timeout`` of the :ref:`summarizer <search.summarizer>`
settings.  It is strictly fail-safe: any error, timeout or unavailable service
results in ``None`` and the search flow continues unaffected.
"""

__all__ = ["embed"]

import json
import typing as t
import urllib.error
import urllib.request
from urllib.parse import urlparse

from searx import get_setting, logger

log = logger.getChild("searx.memory.embedder")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_BASE_URL = "http://127.0.0.1:11434"
_DEFAULT_MODEL = "nomic-embed-text"
_DEFAULT_TIMEOUT = 5.0


class _EmbedderConfig(t.NamedTuple):
    base_url: str
    model: str
    timeout: float
    local_only: bool


def _config() -> _EmbedderConfig:
    """Build the embedder configuration from the settings (reusing the summarizer)."""
    base_url = str(get_setting("search.summarizer.base_url", _DEFAULT_BASE_URL) or _DEFAULT_BASE_URL)
    model = str(get_setting("search.memory.embedding_model", _DEFAULT_MODEL) or _DEFAULT_MODEL)
    try:
        timeout = float(get_setting("search.summarizer.timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    local_only = bool(get_setting("search.summarizer.local_only", True))
    return _EmbedderConfig(base_url, model, timeout, local_only)


def _is_loopback(base_url: str) -> bool:
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:  # pylint: disable=broad-except
        return False
    return host.lower() in _LOOPBACK_HOSTS


def _use_openai_api(base_url: str) -> bool:
    try:
        return "/v1" in (urlparse(base_url).path or "")
    except Exception:  # pylint: disable=broad-except
        return False


def _request_spec(cfg: _EmbedderConfig, text: str) -> tuple[str, bytes]:
    """Return ``(url, body)`` for the configured service (Ollama or OpenAI-compatible)."""
    base = cfg.base_url.rstrip("/")
    if _use_openai_api(cfg.base_url):
        url = base + "/embeddings"
        payload: dict[str, t.Any] = {"model": cfg.model, "input": text}
    else:
        url = base + "/api/embeddings"
        payload = {"model": cfg.model, "prompt": text}
    return url, json.dumps(payload).encode("utf-8")


def _extract_vector(obj: t.Any) -> list[float] | None:
    """Extract the embedding from an Ollama or OpenAI-compatible response."""
    try:
        if isinstance(obj, dict) and "embedding" in obj:  # Ollama /api/embeddings
            raw = obj["embedding"]
        elif isinstance(obj, dict):  # OpenAI-compatible /embeddings
            raw = obj["data"][0]["embedding"]
        else:
            return None
        if isinstance(raw, list) and raw:
            return [float(x) for x in raw]
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return None


def _post_blocking(cfg: _EmbedderConfig, text: str) -> list[float] | None:
    """Perform the (blocking) HTTP request and parse the vector.  Never raises."""
    url, body = _request_spec(cfg, text)
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as resp:  # noqa: S310
            raw = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.debug("embedder request failed: %s", e)
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        log.debug("embedder returned invalid JSON: %s", e)
        return None
    return _extract_vector(obj)


def embed(text: str) -> list[float] | None:
    """Return the embedding of ``text`` or ``None`` on any failure.

    Uses a strict timeout and refuses non-loopback hosts when ``local_only`` is
    set (the default).
    """
    try:
        if not text or not text.strip():
            return None
        cfg = _config()
        if cfg.local_only and not _is_loopback(cfg.base_url):
            log.warning("embedder disabled: local_only=true rejects base_url %r", cfg.base_url)
            return None
        return _post_blocking(cfg, text)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("embedder failed: %s", e)
        return None
