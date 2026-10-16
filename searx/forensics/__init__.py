# SPDX-License-Identifier: AGPL-3.0-or-later
"""Forensics pillar (Phase C - Martillohamer Sear Edition).

Offline investigative toolkit: an OSINT **dork** catalog (:mod:`searx.forensics.dorks`)
and regex **indicator extractors** (:mod:`searx.forensics.extractors`), tied
together by the :py:func:`investigate` facade.

Everything is pure and network-free -- building dorks and extracting indicators
from supplied text never touches the network, so the pillar is trivially safe to
enable and to unit-test.  All public functions are fail-safe.
"""

__all__ = [
    "build_dorks",
    "dork_url",
    "extract",
    "investigate",
    "is_enabled",
    "list_categories",
    "list_extractors",
]

import typing as t

from searx import get_setting, logger

from .dorks import build_dorks, dork_url, list_categories
from .extractors import extract, list_extractors

log = logger.getChild("searx.forensics")


def is_enabled() -> bool:
    """Return ``True`` when the forensics pillar is switched on."""
    try:
        if not bool(get_setting("search.nextgen.enabled", False)):
            return False
        return bool(get_setting("search.nextgen.martillohamer_sear.enabled", False))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("forensics is_enabled failed: %s", e)
        return False


def _pivot(kind: str, value: str) -> dict[str, str]:
    """Return a pivot suggestion for one indicator value."""
    suggestions: dict[str, str] = {
        "onion": f"http://{value}",
        "email": f'"{value}"',
        "ipv4": f"ip:{value}",
        "ipv6": f"ip:{value}",
        "url": value,
    }
    return {"kind": kind, "value": value, "suggestion": suggestions.get(kind, value)}


def investigate(
    query: str,
    text: str = "",
    dork_categories: t.Iterable[str] | None = None,
    indicator_names: t.Iterable[str] | None = None,
) -> dict[str, t.Any]:
    """Investigate ``query`` offline: build dorks and mine ``text`` for indicators.

    ``text`` is any already-collected content (scrape/log/paste).  The result is a
    plain serialisable dict::

        {
          "query": str,
          "enabled": bool,
          "dork_count": int,
          "dorks": [ {id, category, engine, query, description, url}, ... ],
          "indicators": {name: [values...], ...},
          "pivots": [ {kind, value, suggestion}, ... ],
          "indicator_count": int,
        }
    """
    report: dict[str, t.Any] = {
        "query": str(query or ""),
        "enabled": is_enabled(),
        "dork_count": 0,
        "dorks": [],
        "indicators": {},
        "pivots": [],
        "indicator_count": 0,
    }
    try:
        dorks = build_dorks(query, dork_categories)
        for dork in dorks:
            dork["url"] = dork_url(dork)
        report["dorks"] = dorks
        report["dork_count"] = len(dorks)

        indicators = extract(text, indicator_names)
        report["indicators"] = indicators
        pivot_kinds = ("onion", "url", "email", "ipv4", "ipv6")
        pivots: list[dict[str, str]] = []
        for kind in pivot_kinds:
            for value in indicators.get(kind, []):
                pivots.append(_pivot(kind, value))
        report["pivots"] = pivots
        report["indicator_count"] = sum(len(values) for values in indicators.values())
        return report
    except Exception as e:  # pylint: disable=broad-except
        log.debug("forensics investigate failed: %s", e)
        return report
