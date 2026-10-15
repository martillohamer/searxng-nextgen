# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pillar 1 - Executable Search Cards / Sandbox (Phase C).

Summaries (and, in the future, other results) may carry fenced code blocks -
*search cards*.  This pillar detects them (:py:mod:`searx.sandbox.cards`) and,
**only on an explicit user action**, executes the Python ones in an isolated
subprocess (:py:mod:`searx.sandbox.runner`).

Public API::

    from searx.sandbox import is_enabled, detect_cards, execute_card

    cards = detect_cards(summary_text)      # passive, always safe
    result = execute_card(cards[0].code)    # None when the pillar is disabled

The pillar is **opt-in** (``search.nextgen.enabled`` + ``search.nextgen.sandbox.enabled``),
**fail-safe** (every entry point degrades to a no-op / ``None``) and **never auto-runs**
a card: execution requires :py:func:`execute_card`, invoked by the UI/route layer.

Security honesty: the runner is convenience isolation, not an adversarial sandbox;
see :py:mod:`searx.sandbox.runner` for the exact guarantees.
"""

__all__ = ["is_enabled", "detect_cards", "execute_card", "reset", "Card", "ExecutionResult"]

import typing as t

from searx import get_setting, logger
from searx.nextgen import is_enabled as _nextgen_enabled

from .cards import Card, detect_cards, is_supported_language
from .runner import ExecutionResult, run

log = logger.getChild("searx.sandbox")

_DEFAULT_TIMEOUT: float = 5.0
_DEFAULT_MEMORY_MB: int = 128


def is_enabled() -> bool:
    """Return ``True`` when the sandbox pillar is enabled."""
    try:
        return _nextgen_enabled("sandbox")
    except Exception as e:  # pylint: disable=broad-except
        log.debug("sandbox is_enabled failed: %s", e)
        return False


def _get(path: str, default: t.Any) -> t.Any:
    try:
        return get_setting(path, default)
    except Exception:  # pylint: disable=broad-except
        return default


def execute_card(code: str, language: str = "python") -> ExecutionResult | None:
    """Execute ``code`` explicitly.  Returns ``None`` when the pillar is disabled."""
    try:
        if not is_enabled():
            return None
        if not is_supported_language(language):
            return ExecutionResult(False, "", "", -1, 0, False, "unsupported language")
        try:
            timeout = float(_get("search.nextgen.sandbox.timeout", _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = _DEFAULT_TIMEOUT
        try:
            memory = int(_get("search.nextgen.sandbox.memory_limit_mb", _DEFAULT_MEMORY_MB) or _DEFAULT_MEMORY_MB)
        except (TypeError, ValueError):
            memory = _DEFAULT_MEMORY_MB
        return run(code, language=language, timeout=timeout, memory_limit_mb=memory)
    except Exception as e:  # pylint: disable=broad-except
        log.debug("sandbox execute_card failed: %s", e)
        return ExecutionResult(False, "", "", -1, 0, False, str(e) or "execution error")


def reset() -> None:
    """Symmetry hook with the other pillars; the sandbox holds no state."""
    return None
