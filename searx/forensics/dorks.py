# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline OSINT dork catalog for the forensics pillar.

Pure, dependency-free and **network-free**: this module only *builds* the dork
strings a human or an authorised engine adapter may later submit.  Nothing here
performs a request, so it is safe to import and unit-test.

The catalog is intentionally conservative and maps onto public search operators
(``site:``, ``filetype:``, ``inurl:`` ...).  :py:func:`build_dorks` renders every
template with a user query; :py:func:`dork_url` yields a ready-to-share search URL.
"""

__all__ = ["CATEGORIES", "DORK_CATALOG", "build_dorks", "dork_url", "list_categories"]

import typing as t
from urllib.parse import quote_plus

#: Ordered category ids (also the accepted ``categories=`` values).
CATEGORIES: tuple[str, ...] = (
    "documents",
    "credentials",
    "directories",
    "infrastructure",
    "code",
    "social",
    "archives",
)

#: Curated dorks.  Each entry is ``(id, category, engine, template, description)``
#: where ``template`` contains a single ``{q}`` placeholder.
DORK_CATALOG: tuple[tuple[str, str, str, str, str], ...] = (
    ("doc-pdf", "documents", "google", '"{q}" filetype:pdf', "Indexed PDF for the term"),
    ("doc-office", "documents", "google", '"{q}" (filetype:docx OR filetype:xlsx)', "Office documents"),
    ("doc-slides", "documents", "google", '"{q}" filetype:ppt', "Presentations"),
    ("doc-text", "documents", "bing", '"{q}" filetype:txt', "Plain-text notes / logs"),
    ("cred-env", "credentials", "google", '"DB_PASSWORD" "{q}" filetype:env', "Exposed .env files"),
    ("cred-keys", "credentials", "google", '"{q}" (api_key OR apikey OR secret)', "Mentioned secrets"),
    ("cred-paste", "credentials", "duckduckgo", "site:pastebin.com {q}", "Pastebin dumps"),
    ("cred-config", "credentials", "google", '"{q}" filetype:cfg OR filetype:ini', "Config files"),
    ("dir-index", "directories", "google", 'intitle:"index of" "{q}"', "Open directory listings"),
    ("dir-backup", "directories", "google", '"{q}" (backup OR .bak OR .old)', "Backup artefacts"),
    ("dir-listing", "directories", "bing", 'intitle:"index of" {q}', "Generic open indexes"),
    ("infra-ip", "infrastructure", "google", '"{q}" inurl:admin', "Admin panels"),
    ("infra-grafana", "infrastructure", "shodan", '"{q}"', "Shodan host/service view"),
    ("infra-ssh", "infrastructure", "shodan", 'port:22 "{q}"', "Exposed SSH"),
    ("infra-rdp", "infrastructure", "shodan", 'port:3389 "{q}"', "Exposed RDP"),
    ("code-github", "code", "github", '"{q}"', "GitHub code search"),
    ("code-gitlab", "code", "gitlab", '"{q}"', "GitLab search"),
    ("code-stack", "code", "stackoverflow", '"{q}"', "Stack Overflow threads"),
    ("social-twitter", "social", "twitter", '"{q}"', "Twitter/X posts"),
    ("social-reddit", "social", "reddit", '"{q}"', "Reddit threads"),
    ("social-linkedin", "social", "google", 'site:linkedin.com "{q}"', "LinkedIn profiles"),
    ("arch-wayback", "archives", "wayback", '"{q}"', "Wayback Machine snapshots"),
    ("arch-cache", "archives", "google", 'cache:"{q}"', "Search-engine cache"),
    ("arch-paste", "archives", "duckduckgo", 'site:archive.org "{q}"', "Internet Archive items"),
)

#: Default web host used by :py:func:`dork_url` when the engine is unknown.
_ENGINE_URLS: dict[str, str] = {
    "google": "https://www.google.com/search",
    "bing": "https://www.bing.com/search",
    "duckduckgo": "https://duckduckgo.com/",
    "github": "https://github.com/search",
    "gitlab": "https://gitlab.com/search",
    "stackoverflow": "https://stackoverflow.com/search",
    "reddit": "https://www.reddit.com/search/",
    "shodan": "https://www.shodan.io/search",
    "wayback": "https://web.archive.org/web/*/",
    "twitter": "https://twitter.com/search",
}


def list_categories() -> list[str]:
    """Return the known dork category ids."""
    return list(CATEGORIES)


def _render(template: str, query: str) -> str:
    """Render one dork template with ``query`` (never raises)."""
    try:
        return template.format(q=str(query or "").strip())
    except Exception:  # pylint: disable=broad-except
        return str(template or "")


def build_dorks(query: str, categories: t.Iterable[str] | None = None) -> list[dict[str, str]]:
    """Return the dorks for ``query``, optionally filtered to ``categories``.

    Each item is ``{"id", "category", "engine", "query", "description"}`` where the
    the ``query`` field is the fully rendered dork string.
    """
    try:
        wanted: set[str] | None = None
        if categories is not None:
            wanted = {str(c).strip().lower() for c in categories if str(c).strip()}
        out: list[dict[str, str]] = []
        for dork_id, category, engine, template, description in DORK_CATALOG:
            if wanted is not None and category not in wanted:
                continue
            out.append(
                {
                    "id": dork_id,
                    "category": category,
                    "engine": engine,
                    "query": _render(template, query),
                    "description": description,
                }
            )
        return out
    except Exception:  # pylint: disable=broad-except
        return []


def dork_url(dork: dict[str, str]) -> str:
    """Return a shareable search URL for a built dork, or ``""`` if unusable."""
    try:
        engine = str(dork.get("engine", "")).lower()
        query = str(dork.get("query", ""))
        if not query:
            return ""
        base = _ENGINE_URLS.get(engine)
        if base is None:
            return dork.get("query", "")
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}q={quote_plus(query)}"
    except Exception:  # pylint: disable=broad-except
        return ""
