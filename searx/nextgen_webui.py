# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web controllers bridging the Martillohamer Sear Edition pillars (stealth,
forensics, deepweb) to the SearXNG web application.

This module deliberately lives *outside* the pillar packages: it is the only
place where the web layer is allowed to reach into the pillars, and the pillars
never import it back.  Every helper is fail-safe -- a pillar failure degrades to
an empty result instead of a ``500`` response.
"""

import typing as t

from searx import get_setting, logger

__all__ = [
    "admin_enabled",
    "stealth_available",
    "stealth_requested",
    "toolbar_state",
    "available_dork_categories",
    "dorks_view",
    "deepweb_view",
]

log = logger.getChild("searx.nextgen_webui")

#: The umbrella pillar switch under ``search.nextgen``.
_PILLAR = "martillohamer_sear"


def _setting(path: str) -> bool:
    try:
        return bool(get_setting(path, False))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("nextgen setting read failed (%s): %s", path, e)
        return False


def admin_enabled() -> bool:
    """Return ``True`` when the global switch and the pillar switch are both on."""
    return _setting("search.nextgen.enabled") and _setting(f"search.nextgen.{_PILLAR}.enabled")


def stealth_available() -> bool:
    """Return ``True`` when the stealth pillar reports itself enabled."""
    if not admin_enabled():
        return False
    try:
        from searx import stealth

        return bool(stealth.is_enabled())
    except Exception as e:  # pylint: disable=broad-except
        log.debug("stealth unavailable: %s", e)
        return False


def _pref(preferences: t.Any, name: str) -> bool:
    try:
        return bool(preferences.get_value(name))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("preference read failed (%s): %s", name, e)
        return False


def stealth_requested(preferences: t.Any) -> bool:
    """Return ``True`` when this request asked for the amnesic ghost session."""
    return stealth_available() and _pref(preferences, "ghost_mode")


def toolbar_state(preferences: t.Any) -> dict[str, t.Any]:
    """Return the state used to render the ``/nextgen`` toolbar."""
    enabled = admin_enabled()
    return {
        "enabled": enabled,
        "ghost": _pref(preferences, "ghost_mode"),
        "ghost_available": stealth_available(),
        "dorks": enabled and _pref(preferences, "osint_dorks"),
        "deepweb": enabled and _pref(preferences, "deepweb"),
    }


def available_dork_categories() -> list[str]:
    """Return the dork categories exposed by the forensics pillar."""
    try:
        from searx import forensics

        return [str(name) for name in forensics.list_categories()]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("forensics categories unavailable: %s", e)
        return []


def _dork_report(query: str) -> dict[str, t.Any]:
    return {
        "query": query,
        "enabled": False,
        "dork_count": 0,
        "dorks": [],
        "indicators": {},
        "pivots": [],
        "indicator_count": 0,
        "categories": [],
        "error": "",
    }


def dorks_view(query: str, text: str, categories: list[str] | None = None) -> dict[str, t.Any]:
    """Run an offline OSINT/dork investigation, fail-safe."""
    query = str(query or "")
    report = _dork_report(query)
    if not admin_enabled():
        report["error"] = "disabled"
        return report
    try:
        from searx import forensics

        investigated = forensics.investigate(query, str(text or ""), categories)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("forensics investigate failed: %s", e)
        report["error"] = "unavailable"
        return report
    report = dict(investigated)
    report["categories"] = available_dork_categories()
    report["error"] = "" if report.get("enabled") else "disabled"
    return report


def deepweb_view(query: str) -> dict[str, t.Any]:
    """Run a synchronous Tor-backed deepweb search, fail-safe."""
    query = str(query or "")
    report: dict[str, t.Any] = {
        "query": query,
        "enabled": False,
        "engines": [],
        "results": [],
        "result_count": 0,
        "errors": {},
        "tor_available": False,
        "error": "",
    }
    if not admin_enabled():
        report["error"] = "disabled"
        return report
    try:
        from searx import deepweb

        report["engines"] = [str(name) for name in deepweb.configured_engines()]
        if not deepweb.tor_available():
            report["error"] = "tor_unavailable"
            return report
        report.update(deepweb.search(query))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("deepweb search failed: %s", e)
        report["error"] = "unavailable"
        return report
    report["error"] = ""
    return report
