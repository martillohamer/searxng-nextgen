# SPDX-License-Identifier: AGPL-3.0-or-later
"""Summarizer backends.

- :py:obj:`DisabledSummarizer` is the safe default: it never produces a summary.
- :py:obj:`OfflineSummarizer` talks to a local inference service (Ollama or a
  llama.cpp OpenAI-compatible server) with a strict timeout.  Any error, timeout
  or unavailable service results in ``None`` (fail-safe).
"""

__all__ = ["DisabledSummarizer", "OfflineSummarizer", "BACKENDS"]

import asyncio
import json
import logging
import typing as t
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .base import Summarizer, SummarizerConfig

if t.TYPE_CHECKING:
    from searx.result_types import MainResult, LegacyResult

log: logging.Logger = logging.getLogger("searx.summarizer.backends")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class DisabledSummarizer(Summarizer):
    """Backend that never generates a summary.  This is the safe default."""

    name = "disabled"

    async def summarize(
        self, results: "list[MainResult | LegacyResult]", query: str, context: str | None = None
    ) -> str | None:
        return None


class OfflineSummarizer(Summarizer):
    """Backend that queries a local inference service.

    By default it targets the Ollama ``/api/generate`` endpoint.  If
    :py:obj:`SummarizerConfig.base_url` contains a ``/v1`` path segment, an
    OpenAI-compatible ``/chat/completions`` endpoint (llama.cpp server) is used
    instead.

    When :py:obj:`SummarizerConfig.local_only` is set (the default), the
    ``base_url`` host must be a loopback address; otherwise the backend disables
    itself and returns ``None``.
    """

    name = "offline"

    def __init__(self, config: SummarizerConfig) -> None:
        super().__init__(config)
        self._allowed: bool = self._check_local_only()

    def _check_local_only(self) -> bool:
        if not self.config.local_only:
            return True
        try:
            host = urlparse(self.config.base_url).hostname or ""
        except Exception:  # pylint: disable=broad-except
            return False
        return host.lower() in _LOOPBACK_HOSTS

    @property
    def _use_openai_api(self) -> bool:
        return "/v1" in (urlparse(self.config.base_url).path or "")

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if self._use_openai_api:
            return base + "/chat/completions"
        return base + "/api/generate"

    def _payload(self, prompt: str) -> bytes:
        if self._use_openai_api:
            body = {
                "model": self.config.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "max_tokens": self.config.max_tokens,
                "temperature": self.config.temperature,
            }
        else:
            body = {
                "model": self.config.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "num_predict": self.config.max_tokens,
                    "temperature": self.config.temperature,
                },
            }
        return json.dumps(body).encode("utf-8")

    @staticmethod
    def _extract_text(obj: t.Any) -> str | None:
        try:
            if "response" in obj:  # Ollama /api/generate
                text = obj.get("response")
            else:  # OpenAI-compatible /chat/completions
                text = obj["choices"][0]["message"]["content"]
            if isinstance(text, str) and text.strip():
                return text.strip()
        except (KeyError, IndexError, TypeError):
            return None
        return None

    def _post_blocking(self, prompt: str) -> str | None:
        """Perform the (blocking) HTTP request.  Runs in a worker thread."""
        data = self._payload(prompt)
        request = urllib.request.Request(
            self._endpoint(),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as resp:  # noqa: S310
                body = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            log.debug("OfflineSummarizer request failed: %s", e)
            return None
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            log.debug("OfflineSummarizer returned invalid JSON: %s", e)
            return None
        return self._extract_text(obj)

    async def summarize(
        self, results: "list[MainResult | LegacyResult]", query: str, context: str | None = None
    ) -> str | None:
        if not self._allowed:
            log.warning(
                "OfflineSummarizer disabled: local_only=true rejects base_url %r",
                self.config.base_url,
            )
            return None
        if not results:
            return None
        prompt = self.build_prompt(results, query, context)
        try:
            return await asyncio.to_thread(self._post_blocking, prompt)
        except Exception as e:  # pylint: disable=broad-except
            log.debug("OfflineSummarizer failed: %s", e)
            return None


BACKENDS: "dict[str, type[Summarizer]]" = {
    "disabled": DisabledSummarizer,
    "offline": OfflineSummarizer,
}
