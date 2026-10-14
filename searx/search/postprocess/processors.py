# SPDX-License-Identifier: AGPL-3.0-or-later
"""Initial post-processors implementations."""

__all__ = ["DeduplicateAdvanced", "FilterResults"]

import logging
import re
import typing as t
from urllib.parse import unquote, urlparse, urlunparse

from searx import get_setting

from .base import PostProcessor

if t.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.results import ResultContainer
    from searx.result_types import MainResult, LegacyResult

log: logging.Logger = logging.getLogger("searx.postprocess.processors")


def _result_field(result: "MainResult | LegacyResult", name: str, default: str = "") -> str:
    """Read a field from a :py:obj:`MainResult` or a :py:obj:`LegacyResult`."""
    if isinstance(result, dict):
        value = result.get(name, default)
    else:
        value = getattr(result, name, default)
    return value if value is not None else default


class DeduplicateAdvanced(PostProcessor):
    """Advanced deduplication based on URL normalization and title similarity."""

    name = "DeduplicateAdvanced"
    priority = 10

    def _normalize_url(self, url: str) -> str:
        try:
            parsed = urlparse((url or "").strip())
            scheme = (parsed.scheme or "").lower()
            netloc = (parsed.netloc or "").lower()
            path = unquote(parsed.path or "")
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            query = parsed.query or ""
            return urlunparse((scheme, netloc, path, "", query, ""))
        except Exception:  # pylint: disable=broad-except
            return (url or "").strip().lower()

    def _titles_similar(self, title1: str, title2: str) -> bool:
        try:
            a = re.sub(r"\W+", " ", (title1 or "").lower()).strip()
            b = re.sub(r"\W+", " ", (title2 or "").lower()).strip()
            if not a or not b:
                return False
            if a == b:
                return True
            return a in b or b in a
        except Exception:  # pylint: disable=broad-except
            return False

    def process(self, container: "ResultContainer", search: "SearchWithPlugins") -> None:
        seen_urls: set[str] = set()
        seen_titles: list[str] = []

        def _is_duplicate(result: "MainResult | LegacyResult") -> bool:
            url = _result_field(result, "url")
            norm = self._normalize_url(url)
            if norm:
                if norm in seen_urls:
                    return True
                seen_urls.add(norm)
                return False

            # no URL: fall back to a normalized title comparison
            title = _result_field(result, "title").lower()[:120]
            if not title:
                return False
            for seen in seen_titles:
                if self._titles_similar(title, seen):
                    return True
            seen_titles.append(title)
            return False

        removed = container.filter_main_results(_is_duplicate)
        if removed:
            log.debug("DeduplicateAdvanced removed %s duplicate result(s)", removed)


class FilterResults(PostProcessor):
    """Basic result filtering by excluded domains."""

    name = "FilterResults"
    priority = 20

    def _excluded_domains(self) -> set[str]:
        excluded: set[str] = set()
        raw = get_setting("search.postprocess.processors.FilterResults.filters.excluded_domains", [])
        if not raw:
            return excluded
        for domain in raw:
            domain_str = (domain or "").lower().strip()
            if domain_str:
                excluded.add(domain_str)
        return excluded

    def process(self, container: "ResultContainer", search: "SearchWithPlugins") -> None:
        try:
            excluded = self._excluded_domains()
        except Exception as e:  # pylint: disable=broad-except
            log.debug("FilterResults: failed to load excluded domains: %s", e)
            return
        if not excluded:
            return

        def _is_excluded(result: "MainResult | LegacyResult") -> bool:
            url = _result_field(result, "url")
            if not url:
                return False
            try:
                netloc = (urlparse(url).netloc or "").lower()
            except Exception:  # pylint: disable=broad-except
                return False
            return any(netloc == domain or netloc.endswith("." + domain) for domain in excluded)

        removed = container.filter_main_results(_is_excluded)
        if removed:
            log.debug("FilterResults removed %s result(s) by excluded domains", removed)
