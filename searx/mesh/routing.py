# SPDX-License-Identifier: AGPL-3.0-or-later
"""Routing and result aggregation for the P2P mesh (Phase C - Pillar 4).

Two pure functions with no I/O so the federation logic can be reasoned about and
tested in isolation:

* :py:func:`select` picks up to ``fanout`` healthy peers from the registry, using
  one of three strategies - ``latency`` (fastest observed first), ``round_robin``
  (stable order rotated by an explicit ``offset``) or ``capability`` (peers that
  advertise the required capabilities first, then by latency).
* :py:func:`aggregate` merges the per-peer responses into one ranked result list:
  results are interleaved round-robin across peers, de-duplicated by normalized
  URL, and each surviving result records the peers that returned it (``sources``).
"""

__all__ = ["select", "aggregate"]

import hashlib
import typing as t

from searx import logger

from .node import NodeProfile

log = logger.getChild("searx.mesh.routing")

STRATEGIES: tuple[str, ...] = ("latency", "round_robin", "capability")
DEFAULT_STRATEGY: str = "latency"
DEFAULT_MAX_RESULTS: int = 20


def _latency_key(node: NodeProfile) -> float:
    value = node.latency_ema
    return value if value > 0 else 0.0


def _order(
    nodes: list[NodeProfile], strategy: str, required_capabilities: frozenset[str], offset: int
) -> list[NodeProfile]:
    if strategy == "round_robin":
        ordered = sorted(nodes, key=lambda node: node.node_id)
        if ordered:
            start = int(offset) % len(ordered)
            ordered = ordered[start:] + ordered[:start]
        return ordered
    if strategy == "capability":

        def key(node: NodeProfile) -> tuple[int, float, str]:
            missing = 0 if required_capabilities and required_capabilities.issubset(node.capabilities) else 1
            return (missing, _latency_key(node), node.node_id)

        return sorted(nodes, key=key)
    return sorted(nodes, key=lambda node: (_latency_key(node), node.node_id))


def select(
    nodes: t.Iterable[NodeProfile],
    strategy: str = DEFAULT_STRATEGY,
    fanout: int = 1,
    *,
    required_capabilities: t.Iterable[str] = (),
    offset: int = 0,
) -> list[NodeProfile]:
    """Return up to ``fanout`` peers ordered by ``strategy`` (fail-safe: ``[]``)."""
    try:
        chosen = str(strategy or DEFAULT_STRATEGY).lower()
        if chosen not in STRATEGIES:
            chosen = DEFAULT_STRATEGY
        try:
            count = max(1, int(fanout))
        except (TypeError, ValueError):
            count = 1
        required = frozenset(str(c) for c in (required_capabilities or ()) if str(c))
        ordered = _order(list(nodes or ()), chosen, required, int(offset))
        return ordered[:count]
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh routing: select failed: %s", e)
        return []


def _norm_url(url: t.Any) -> str:
    text = str(url or "").strip().lower()
    if not text:
        return ""
    text = text.split("#", 1)[0]
    if len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _dedup_key(result: dict) -> str:
    url = _norm_url(result.get("url", ""))
    basis = url or repr(sorted(result.items()))[:512]
    return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()


def aggregate(
    responses: t.Iterable[t.Mapping[str, t.Any]], *, max_results: int = DEFAULT_MAX_RESULTS
) -> dict[str, t.Any]:
    """Merge per-peer responses into ``{results, count, peers}``; fail-safe."""
    try:
        limit = max(1, int(max_results))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_RESULTS
    peers: list[str] = []
    queues: list[tuple[str, list[dict]]] = []
    try:
        for response in responses or ():
            if not isinstance(response, dict):
                continue
            node_id = str(response.get("node_id", "") or "")
            raw = response.get("results") or []
            if not isinstance(raw, list):
                continue
            if node_id:
                peers.append(node_id)
            queues.append((node_id, [item for item in raw if isinstance(item, dict)]))
    except Exception as e:  # pylint: disable=broad-except
        log.debug("mesh routing: aggregate read failed: %s", e)
        return {"results": [], "count": 0, "peers": []}

    merged: list[dict] = []
    index: dict[str, dict] = {}
    cursor = 0
    while queues and len(merged) < limit:
        progressed = False
        for node_id, queue in queues:
            if cursor >= len(queue):
                continue
            progressed = True
            item = queue[cursor]
            key = _dedup_key(item)
            existing = index.get(key)
            if existing is None:
                copy = dict(item)
                copy["sources"] = [node_id] if node_id else []
                index[key] = copy
                merged.append(copy)
                if len(merged) >= limit:
                    break
            elif node_id and node_id not in existing["sources"]:
                existing["sources"].append(node_id)
        if not progressed:
            break
        cursor += 1
    return {"results": merged, "count": len(merged), "peers": peers}
