# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable "search card" detection (Phase C - Pillar 1).

Parses fenced code blocks from summaries / results and classifies each supported
card as a single *evaluable expression* or a general *block*.  Detection is
**purely passive**: it never executes anything and may run even when the sandbox
pillar is disabled.
"""

__all__ = ["Card", "detect_cards", "is_supported_language"]

import ast
import re
from dataclasses import dataclass

from searx import logger

log = logger.getChild("searx.sandbox.cards")

#: Languages this pillar can execute (Phase C: Python only).
_SUPPORTED: frozenset[str] = frozenset({"python", "py"})

#: A fenced code block; the info string is captured between the backticks and the newline.
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+.-]*)[^\n]*\n(.*?)```", re.DOTALL)

_DEFAULT_MAX_CARDS: int = 8


@dataclass(frozen=True)
class Card:
    """A detected, executable card."""

    language: str
    code: str
    kind: str  # "expression" | "block"
    index: int


def is_supported_language(language: str) -> bool:
    """Return whether ``language`` (a fence info string) is executable."""
    try:
        return str(language or "").strip().lower() in _SUPPORTED
    except Exception:  # pylint: disable=broad-except
        return False


def _classify(code: str) -> str:
    """Return ``"expression"`` when ``code`` parses as a single expression."""
    try:
        ast.parse(code, mode="eval")
        return "expression"
    except SyntaxError:
        return "block"
    except Exception:  # pylint: disable=broad-except
        return "block"


def detect_cards(text: str, max_cards: int = _DEFAULT_MAX_CARDS) -> list[Card]:
    """Return the executable cards found in ``text`` (never raises)."""
    try:
        if not text:
            return []
        try:
            limit = max(0, int(max_cards))
        except (TypeError, ValueError):
            limit = _DEFAULT_MAX_CARDS
        cards: list[Card] = []
        for match in _FENCE_RE.finditer(text):
            if len(cards) >= limit:
                break
            language = (match.group(1) or "").strip().lower()
            if not is_supported_language(language):
                continue
            code = match.group(2) or ""
            if not code.strip():
                continue
            cards.append(Card(language=language, code=code, kind=_classify(code), index=len(cards)))
        return cards
    except Exception as e:  # pylint: disable=broad-except
        log.debug("sandbox cards: detect_cards failed: %s", e)
        return []
