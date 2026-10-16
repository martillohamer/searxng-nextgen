# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stdlib HTML parsing helpers for the deepweb pillar.

Deepweb/hidden-service pages are frequently old, malformed or table-based, so
this parser deliberately favours robustness over fidelity: it extracts the
title, visible text and links from whatever HTML it is handed, skipping
``<script>``/``<style>`` bodies, and never raises.

Pure and network-free -- usable on any already-fetched page.
"""

__all__ = ["extract_links", "extract_title", "parse_page", "to_text"]

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
_BLOCK_TAGS = frozenset(
    {
        "br", "p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
        "article", "section", "ul", "ol", "table", "header", "footer", "main", "nav",
        "pre", "blockquote",
    }
)


def _collapse(text: str) -> str:
    """Collapse runs of whitespace into single spaces / blank lines."""
    try:
        lines = [_WHITESPACE.sub(" ", line).strip() for line in str(text or "").split("\n")]
        return _BLANKS.sub("\n\n", "\n".join(lines)).strip()
    except Exception:  # pylint: disable=broad-except
        return ""


class _PageParser(HTMLParser):
    """Collect title, visible text and ``(href, text)`` links in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str = ""
        self._in_title: bool = False
        self._skip_depth: int = 0
        self._text: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._href: str = ""
        self._link_text: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        if name in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if name == "title":
            self._in_title = True
        elif name == "a" and not self._href:
            for key, value in attrs:
                if key.lower() == "href" and value:
                    self._href = value
                    self._link_text = ""
                    break
        if name in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self._text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if name == "title":
            self._in_title = False
        elif name == "a" and self._href:
            self._links.append((self._href, _collapse(self._link_text)))
            self._href = ""
            self._link_text = ""

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        self._text.append(data)
        if self._href:
            self._link_text += data

    def text(self) -> str:
        return _collapse("\n".join(self._text))

    def links(self) -> list[tuple[str, str]]:
        return list(self._links)


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    try:
        parser.feed(str(html or ""))
        parser.close()
    except Exception:  # pylint: disable=broad-except
        pass
    return parser


def extract_title(html: str) -> str:
    """Return the document ``<title>`` (collapsed) or ``""``."""
    return _collapse(_parse(html).title)


def to_text(html: str) -> str:
    """Return the visible text of ``html`` (scripts/styles skipped)."""
    return _parse(html).text()


def extract_links(html: str, base_url: str = "") -> list[dict[str, str]]:
    """Return ``[{href, text, url}]`` with ``url`` resolved against ``base_url``."""
    out: list[dict[str, str]] = []
    try:
        for href, text in _parse(html).links():
            out.append({"href": href, "text": text, "url": urljoin(base_url, href) if base_url else href})
    except Exception:  # pylint: disable=broad-except
        return out
    return out


def parse_page(html: str, url: str = "") -> dict[str, object]:
    """Return ``{url, title, text, links}`` for an already-fetched page."""
    parser = _parse(html)
    links = [{"href": href, "text": text, "url": urljoin(url, href) if url else href} for href, text in parser.links()]
    return {
        "url": str(url or ""),
        "title": _collapse(parser.title),
        "text": parser.text(),
        "links": links,
    }
