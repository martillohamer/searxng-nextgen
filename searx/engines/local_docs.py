# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search a *local* technical documentation service or an *internal repository*.

This engine talks to a self-hosted documentation search service (for example a
MkDocs/Sphinx site exposing a JSON search endpoint, an internal wiki API or any
repository gateway) reachable from the SearXNG instance.  It never reaches the
public Internet unless the configured ``base_url`` points there.

Configuration
=============

Add an entry to the ``engines`` list in ``settings.yml``:

.. code:: yaml

  - name: local docs
    engine: local_docs
    shortcut: ldocs
    categories: [it, science]
    base_url: http://docs.internal.lan
    search_path: /api/search
    api_key: ""
    max_results: 10
    disabled: true

Implementations
===============

"""

import typing as t
from urllib.parse import urlencode

from searx.result_types import EngineResults
from searx.network import raise_for_httperror

if t.TYPE_CHECKING:
    from searx.extended_types import SXNG_Response
    from searx.search.processors import OnlineParams


engine_type = "online"
categories = ["it", "science"]
disabled = True
timeout = 3.0
paging = True

about = {
    "website": None,
    "wikidata_id": None,
    "official_api_documentation": None,
    "use_official_api": False,
    "require_api_key": True,
    "results": "JSON",
    "description": "Search a local technical documentation service or an internal repository.",
}

base_url: str = ""
"""Base URL of the local documentation service (e.g. ``http://docs.internal.lan``)."""

search_path: str = "/api/search"
"""Path of the JSON search endpoint on :py:obj:`base_url`."""

api_key: str = ""
"""Optional bearer token sent in the ``Authorization`` header."""

max_results: int = 10
"""Maximum number of results requested per page."""

_no_result_for_http_status: tuple[int, ...] = (204, 404)
"""HTTP statuses that simply mean "no results" (tolerated, not an error)."""


def setup(engine_settings: dict[str, t.Any]) -> bool:
    """Validate and normalize the engine configuration.

    The SearXNG loader applies the raw ``engine_settings`` to the module globals
    *before* calling :py:obj:`setup`, so the normalized values are written back
    to the module globals explicitly (see ``demo_online.py`` for the idiom).

    For more details see :py:obj:`searx.enginelib.Engine.setup`."""
    global base_url, search_path  # pylint: disable=global-statement

    if not engine_settings.get("base_url"):
        logger.error("missing local_docs configuration: base_url is required")
        return False

    base_url = str(engine_settings["base_url"]).rstrip("/")

    path = str(engine_settings.get("search_path") or "/api/search").strip() or "/api/search"
    if not path.startswith("/"):
        path = "/" + path
    search_path = path

    engine_settings["base_url"] = base_url
    engine_settings["search_path"] = search_path
    return True


def _endpoint() -> str:
    """Return the normalized ``base_url + search_path`` endpoint."""
    base = base_url.rstrip("/")
    path = search_path if search_path.startswith("/") else "/" + search_path
    return f"{base}{path}"


def request(query: str, params: "OnlineParams") -> None:
    """Build the JSON search request against the local documentation service."""
    if not base_url:
        # No configuration: leave ``params['url']`` empty so the processor skips
        # this engine instead of performing a bogus request.
        return

    args = urlencode(
        {
            "q": query,
            "page": params["pageno"],
            "limit": max_results,
            "language": params.get("language") or "",
        }
    )
    params["url"] = f"{_endpoint()}?{args}"
    params["method"] = "GET"
    params["raise_for_httperror"] = False

    headers: dict[str, str] = params.setdefault("headers", {})
    headers["Accept"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"


def _extract_items(data: t.Any) -> list[dict[str, t.Any]]:
    """Return the list of result items from the different payload shapes."""
    if isinstance(data, dict):
        for key in ("results", "hits", "items", "documents", "data"):
            items = data.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def response(resp: "SXNG_Response") -> EngineResults:
    """Parse the JSON payload tolerantly; never raise on malformed content."""
    res = EngineResults()

    if resp.status_code in _no_result_for_http_status:
        return res

    raise_for_httperror(resp)

    if not resp.text:
        return res

    try:
        data = resp.json()
    except Exception as e:  # pylint: disable=broad-except
        logger.debug("local_docs: could not decode JSON response: %s", e)
        return res

    for item in _extract_items(data):
        try:
            url = item.get("url") or item.get("href") or item.get("link") or ""
            if not url:
                continue
            res.add(
                res.types.MainResult(
                    url=url,
                    title=item.get("title") or item.get("name") or url,
                    content=item.get("content") or item.get("snippet") or item.get("summary") or "",
                )
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.debug("local_docs: skipping malformed result item: %s", e)

    return res
