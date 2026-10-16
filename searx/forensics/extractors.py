# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure, offline indicator extractors for the forensics pillar.

Regex-only and dependency-free: given a blob of text (a scraped page, a paste, a
log excerpt), :py:func:`extract` returns the artefacts worth pivoting on --
emails, URLs, addresses, hashes, keys, crypto wallets, onion services.  Nothing
here makes a request; the caller decides what to do with the findings.

Every function is **fail-safe**: unexpected input yields an empty result rather
than raising.
"""

__all__ = ["EXTRACTORS", "extract", "list_extractors"]

import re
import typing as t

#: Named regex registry.  Adding a key here automatically exposes it via ``extract``.
EXTRACTORS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    "url": re.compile(r"https?://[^\s\"'<>()\[\]{}]+", re.IGNORECASE),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "ipv6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
    "onion": re.compile(r"\b[a-z2-7]{56}\.onion\b", re.IGNORECASE),
    "md5": re.compile(r"\b[A-Fa-f0-9]{32}\b"),
    "sha1": re.compile(r"\b[A-Fa-f0-9]{40}\b"),
    "sha256": re.compile(r"\b[A-Fa-f0-9]{64}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "btc_address": re.compile(r"\b(?:bc1[a-z0-9]{25,39}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"),
    "eth_address": re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    "aws_arn": re.compile(r"\barn:aws:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[^\s\"']+\b"),
}


def list_extractors() -> list[str]:
    """Return the names of the available extractors."""
    return list(EXTRACTORS)


def extract(text: str, names: t.Iterable[str] | None = None) -> dict[str, list[str]]:
    """Extract indicators from ``text``.

    ``names`` optionally restricts the run to a subset of :py:data:`EXTRACTORS`
    keys.  Results are de-duplicated and ordered by first appearance.
    """
    result: dict[str, list[str]] = {}
    try:
        if not isinstance(text, str) or not text:
            return result
        selected: list[str] = list(EXTRACTORS) if names is None else [n for n in names if n in EXTRACTORS]
        for name in selected:
            pattern = EXTRACTORS[name]
            seen: set[str] = set()
            matches: list[str] = []
            for match in pattern.findall(text):
                value = match if isinstance(match, str) else str(match)
                if value and value not in seen:
                    seen.add(value)
                    matches.append(value)
            if matches:
                result[name] = matches
        return result
    except Exception:  # pylint: disable=broad-except
        return result


def _extract_safe(text: str, names: t.Iterable[str] | None = None) -> dict[str, list[str]]:
    """Alias kept for symmetry; identical to :py:func:`extract`."""
    return extract(text, names)
