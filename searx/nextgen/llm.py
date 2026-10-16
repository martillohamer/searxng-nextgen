# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-safe sanitizer for LLM chat payloads.

Several chat APIs reject a request whose ``messages`` list ends on an
assistant/model turn (``Requests ending with a model turn are not supported``).
:py:func:`sanitize_messages` returns a **new** list that is guaranteed to be
non-empty and to end on a ``user`` turn, so any caller can hand its payload to
the API without re-checking.

The helper is dependency-free (stdlib only) and **fail-safe**: any unexpected
input degrades to a single neutral ``user`` message instead of raising.

Note that a leading ``assistant``/``system`` turn is preserved: only *trailing*
turns that are not ``user`` are dropped.
"""

__all__ = ["ASSISTANT_ROLES", "USER_ROLE", "sanitize_messages"]

import typing as t

#: Canonical role of a human turn.
USER_ROLE: str = "user"

#: Model/assistant roles (OpenAI uses ``assistant``, Gemini uses ``model``).
ASSISTANT_ROLES: frozenset[str] = frozenset({"assistant", "model"})

_DEFAULT_USER_CONTENT: str = "Continue."


def _role(message: t.Mapping[str, t.Any]) -> str:
    """Return the normalized role of ``message`` or ``""`` (never raises)."""
    try:
        return str(message.get("role", "") or "").strip().lower()
    except Exception:  # pylint: disable=broad-except
        return ""


def sanitize_messages(messages: t.Any, default_user_content: str = _DEFAULT_USER_CONTENT) -> list[dict[str, t.Any]]:
    """Return a chat message list that ends on a ``user`` turn.

    Only well-formed ``dict`` entries that carry a non-empty ``role`` are kept;
    trailing turns whose role is not ``user`` (``assistant``/``model``/``system``/
    ``tool`` ...) are removed.  An empty or unusable input degrades to a single
    neutral ``user`` message.  The input list is never mutated.
    """
    try:
        content = str(default_user_content or _DEFAULT_USER_CONTENT)
    except Exception:  # pylint: disable=broad-except
        content = _DEFAULT_USER_CONTENT
    fallback: list[dict[str, t.Any]] = [{"role": USER_ROLE, "content": content}]
    try:
        if not isinstance(messages, (list, tuple)):
            return fallback
        cleaned: list[dict[str, t.Any]] = [
            t.cast("dict[str, t.Any]", message) for message in messages if isinstance(message, dict) and _role(message)
        ]
        while cleaned and _role(cleaned[-1]) != USER_ROLE:
            cleaned.pop()
        return cleaned if cleaned else fallback
    except Exception as e:  # pylint: disable=broad-except
        del e
        return fallback
