# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared contract for the Phase C pillars: version, kill-switch and identifiers.

Kill-switch model (hierarchical, both levels required)::

    search.nextgen.enabled            # global switch for all Phase C features
    search.nextgen.<pillar>.enabled   # per-pillar switch

:py:func:`is_enabled` with no argument returns the global switch; with a pillar
name it returns ``global AND pillar``.  A disabled pillar must behave exactly as
if its code did not exist.
"""

import hashlib
import typing as t

from searx import get_setting, logger

log = logger.getChild("searx.nextgen.contract")

__all__ = [
    "NEXTGEN_SCHEMA_VERSION",
    "PILLARS",
    "is_enabled",
    "node_id",
    "edge_id",
    "normalize_key",
]

#: Version of the on-disk schema shared by the Phase C pillars.
NEXTGEN_SCHEMA_VERSION: int = 1

#: Known Phase C pillars (used by :py:func:`is_enabled` and the settings schema).
PILLARS: frozenset[str] = frozenset({"graph", "anticipate", "sandbox", "mesh"})

_ID_LEN: int = 16
_SEP: bytes = b"\x1f"


def _get(path: str, default: t.Any) -> t.Any:
    """Read a dotted setting, never raising."""
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def is_enabled(pillar: str | None = None) -> bool:
    """Return whether Phase C (or a specific ``pillar``) is enabled.

    Without ``pillar``: returns the global ``search.nextgen.enabled`` switch.
    With ``pillar``: returns ``global AND search.nextgen.<pillar>.enabled``.
    Unknown pillar names are treated as disabled.
    """
    try:
        if not bool(_get("search.nextgen.enabled", False)):
            return False
        if pillar is None:
            return True
        if pillar not in PILLARS:
            return False
        return bool(_get(f"search.nextgen.{pillar}.enabled", False))
    except Exception:  # pylint: disable=broad-except
        return False


def normalize_key(value: t.Any) -> str:
    """Normalize a display name/key into a stable, comparable form."""
    try:
        return " ".join(str(value or "").strip().lower().split())
    except Exception:  # pylint: disable=broad-except
        return ""


def _hash(*parts: t.Any) -> str:
    digest = hashlib.sha1()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(_SEP)
    return digest.hexdigest()[:_ID_LEN]


def node_id(kind: str, key: str) -> str:
    """Deterministic, stable id for an entity node ``(kind, key)``."""
    return _hash("node", normalize_key(kind), normalize_key(key))


def edge_id(src_id: str, relation: str, dst_id: str) -> str:
    """Deterministic, stable id for a directed edge ``src -[relation]-> dst``."""
    return _hash("edge", src_id, normalize_key(relation), dst_id)
