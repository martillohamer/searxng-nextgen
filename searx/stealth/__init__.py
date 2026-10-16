# SPDX-License-Identifier: AGPL-3.0-or-later
"""Amnesic/stealth pillar (Phase C - Martillohamer Sear Edition).

Exposes :py:class:`StealthContext` plus the process-wide activation/buffer/purge
primitives from :py:mod:`searx.stealth.context`.  See that module for the exact,
application-level scope of the guarantee.
"""

__all__ = [
    "ASSISTANT_ROLES",
    "StealthContext",
    "activate",
    "buffered",
    "deactivate",
    "drain",
    "is_active",
    "is_enabled",
    "purge",
    "register_purge",
    "reset",
    "suppress",
]

from .context import (
    StealthContext,
    activate,
    buffered,
    deactivate,
    drain,
    is_active,
    is_enabled,
    purge,
    register_purge,
    reset,
    suppress,
)

# Convenience re-export: the assistant/model role set, so callers wiring the
# wire-guard can classify a payload without importing searx.nextgen.llm.
from searx.nextgen.llm import ASSISTANT_ROLES
