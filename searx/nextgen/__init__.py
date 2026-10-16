# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared contract for the Phase C "Distributed Cognitive Singularity" pillars.

This package is intentionally a **thin foundation**, not a monolithic core: it
only provides the pieces every pillar must agree on — the schema version, the
hierarchical kill-switch, stable identifier helpers and key normalization.  Each
pillar (``searx/graph``, ``searx/anticipate``, ``searx/sandbox``, ``searx/mesh``)
is an independent package that depends on this contract plus the existing
``searx/memory``, ``searx/resultcache`` and ``searx/metrics`` subsystems, never
on each other.

Everything here is **fail-safe**: any error degrades to "disabled" / empty and is
never propagated into the search flow.
"""

__all__ = [
    "NEXTGEN_SCHEMA_VERSION",
    "PILLARS",
    "is_enabled",
    "node_id",
    "edge_id",
    "normalize_key",
    "sanitize_messages",
]

from .contract import NEXTGEN_SCHEMA_VERSION, PILLARS, edge_id, is_enabled, node_id, normalize_key
from .llm import sanitize_messages
