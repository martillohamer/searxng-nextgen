# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deepweb pillar (Phase C - Martillohamer Sear Edition).

Access to Tor hidden services through a dependency-free SOCKS5 client
(:mod:`searx.deepweb.tor_client`) plus stdlib HTML parsing
(:mod:`searx.deepweb.parsers`), tied together by the :py:func:`fetch` and
:py:func:`search` facades.

Everything is opt-in (``search.nextgen.martillohamer_sear.enabled``), Tor-only
(the client defaults to ``socks5h://127.0.0.1:9050``) and fail-safe: a missing
or locked Tor never raises out of this module.

This pillar performs **outbound network I/O** (unlike ``forensics``).  The
stealth pillar suppresses this app's *own* persistence, not third-party
traffic -- callers who need anonymity must run Tor and keep ``stealth_mode`` on.
"""

__all__ = [
    "DEFAULT_TOR_PROXY",
    "ENGINE_TEMPLATES",
    "Response",
    "TorClientError",
    "configured_engines",
    "fetch",
    "is_enabled",
    "search",
    "tor_available",
]

import typing as t
from urllib.parse import quote_plus, urlsplit

from searx import get_setting, logger

from .parsers import parse_page
from .tor_client import DEFAULT_TOR_PROXY, Response, TorClientError, get, is_tor_available

log = logger.getChild("searx.deepweb")

ENGINE_TEMPLATES: dict[str, str] = {
    # Clearnet Tor search index.
    "ahmia": "https://ahmia.fi/search/?q={query}",
    # Hidden search engines (addresses are well-known but may rotate).
    "torch": "http://xmh57jrknzkhv6y3ls3ubitzfqnkrwxhopf5aygthi7d6rplyvk3noyd.onion/search?query={query}",
    "haystak": "http://haystak5njsmn2hqkewecpaxetahtwhsbsa64jom2k22z5afxhnpxfid.onion/search?q={query}",
}

_RESULT_LIMIT_DEFAULT: int = 20


def is_enabled() -> bool:
    """Return ``True`` when the deepweb pillar is switched on."""
    try:
        if not bool(get_setting("search.nextgen.enabled", False)):
            return False
        return bool(get_setting("search.nextgen.martillohamer_sear.enabled", False))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("deepweb is_enabled failed: %s", e)
        return False


def configured_engines() -> list[str]:
    """Return the configured deepweb engine names (known templates only)."""
    try:
        names = get_setting("search.nextgen.martillohamer_sear.deepweb_engines", [])
    except Exception as e:  # pylint: disable=broad-except
        log.debug("deepweb configured_engines failed: %s", e)
        return []
    return [str(name) for name in (names or []) if str(name) in ENGINE_TEMPLATES]


def _settings() -> tuple[str, float]:
    """Return ``(proxy, timeout)`` from settings with safe fallbacks."""
    proxy = DEFAULT_TOR_PROXY
    timeout = 15.0
    try:
        proxy = str(get_setting("search.nextgen.martillohamer_sear.tor_proxy", DEFAULT_TOR_PROXY) or DEFAULT_TOR_PROXY)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("deepweb proxy setting failed: %s", e)
    try:
        timeout = float(get_setting("search.nextgen.martillohamer_sear.timeout", 15) or 15)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("deepweb timeout setting failed: %s", e)
    return proxy, timeout


def tor_available(timeout: float = 2.0) -> bool:
    """Return ``True`` when the configured SOCKS proxy accepts a connection."""
    proxy, _ = _settings()
    try:
        return bool(is_tor_available(proxy, timeout))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("deepweb tor_available failed: %s", e)
        return False


def fetch(url: str, timeout: float | None = None, proxy: str | None = None) -> dict[str, t.Any]:
    """Fetch ``url`` over Tor and return a parsed page dict.

    Result::

        {"ok": bool, "url": str, "status": int, "title": str, "text": str,
         "links": [...], "error": str | None}

    Never raises; a failure is reported through ``ok``/``error``.
    """
    result: dict[str, t.Any] = {
        "ok": False,
        "url": str(url or ""),
        "status": 0,
        "title": "",
        "text": "",
        "links": [],
        "error": None,
    }
    if not str(url or "").strip():
        result["error"] = "empty url"
        return result
    default_proxy, default_timeout = _settings()
    use_proxy = default_proxy if proxy is None else proxy
    use_timeout = float(timeout) if timeout is not None else default_timeout
    try:
        response = get(url, proxy=use_proxy, timeout=use_timeout)
        result["status"] = response.status
        page = parse_page(response.text, url)
        result["title"] = page["title"]
        result["text"] = page["text"]
        result["links"] = page["links"]
        result["ok"] = response.status < 400
        if not result["ok"]:
            result["error"] = f"HTTP {response.status}"
    except TorClientError as e:
        result["error"] = str(e)
    except Exception as e:  # pylint: disable=broad-except
        result["error"] = f"{type(e).__name__}: {e}"
        log.debug("deepweb fetch failed for %s: %s", url, e)
    return result


def _engine_search_url(engine: str, query: str) -> str:
    template = ENGINE_TEMPLATES.get(engine, "")
    return template.replace("{query}", quote_plus(str(query or ""))) if template else ""


def _extract_results(page: dict[str, t.Any], engine: str, limit: int) -> list[dict[str, str]]:
    """Turn a parsed search page into a de-duplicated result list."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    engine_host = urlsplit(str(page.get("url") or "")).hostname or ""
    for link in page.get("links", []) or []:
        href = str(link.get("url") or "")
        if not href.lower().startswith(("http://", "https://")):
            continue
        host = urlsplit(href).hostname or ""
        if host and host == engine_host:
            continue
        if href in seen:
            continue
        seen.add(href)
        rows.append({"engine": engine, "title": str(link.get("text") or href), "url": href})
        if len(rows) >= limit:
            break
    return rows


def search(
    query: str,
    engines: t.Iterable[str] | None = None,
    timeout: float | None = None,
    max_results: int = _RESULT_LIMIT_DEFAULT,
) -> dict[str, t.Any]:
    """Search the configured deepweb engines for ``query`` over Tor.

    Result::

        {"query": str, "enabled": bool, "engines": [names...],
         "results": [{engine, title, url}...], "result_count": int,
         "errors": {engine: message}, "tor_available": bool}

    Never raises; per-engine failures are collected in ``errors``.
    """
    report: dict[str, t.Any] = {
        "query": str(query or ""),
        "enabled": is_enabled(),
        "engines": [],
        "results": [],
        "result_count": 0,
        "errors": {},
        "tor_available": False,
    }
    selected = [str(name) for name in (engines if engines is not None else configured_engines())]
    selected = [name for name in selected if name in ENGINE_TEMPLATES]
    report["engines"] = selected
    if not selected:
        return report
    proxy, default_timeout = _settings()
    use_timeout = float(timeout) if timeout is not None else default_timeout
    report["tor_available"] = is_tor_available(proxy, min(2.0, use_timeout))

    limit = max(1, int(max_results))
    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    for engine in selected:
        url = _engine_search_url(engine, query)
        page = fetch(url, timeout=use_timeout, proxy=proxy)
        if not page.get("ok"):
            report["errors"][engine] = page.get("error") or "fetch failed"
            continue
        for row in _extract_results(page, engine, limit):
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            collected.append(row)
            if len(collected) >= limit:
                break
        if len(collected) >= limit:
            break
    report["results"] = collected
    report["result_count"] = len(collected)
    return report
