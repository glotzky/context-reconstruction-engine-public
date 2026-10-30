"""Deterministic check that a claim's cited evidence can actually support it.

A claim marked ``verified`` must cite evidence IDs that exist (checked in ``hypotheses``). This
module adds two cheap, explainable checks in application code; the model never decides them:

* absence and negative-relation claims ("No release accompanied...", "...is unrelated to...")
  cannot be shown by evidence items that are present, so they are never ``verified``;
* identifiers the claim names (quoted strings, snake_case or dotted names, paths, CamelCase
  names, long hex IDs) must appear in at least one cited evidence item.

The check is conservative about what it extracts and says nothing about meaning: it can only
downgrade a claim to ``unverified``, never upgrade one. Reasons never echo model output.
"""

import json
import re
from collections.abc import Iterable

from cre.evidence import Evidence

_QUOTED = re.compile(r"`([^`\n]{2,80})`|\"([^\"\n]{2,80})\"|'([^'\n]{2,80})'")
_IDENTIFIER = re.compile(
    r"(?<![\w./])(?:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:[_./][A-Za-z0-9_]+)+"  # snake_case, dotted names, paths
    r"|[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+"  # CamelCase
    r")(?![\w])"
)
_HEX_ID = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_ABSENCE_START = re.compile(r"^\s*(no|none|nothing|never|neither)\b", re.IGNORECASE)
_ABSENCE_PHRASES = re.compile(
    r"\b(no evidence|no record|not shown|not recorded|not present|not available|not found|"
    r"none of|unrelated|independent of|not related|unaffected|never)\b",
    re.IGNORECASE,
)
_MAX_TOKENS = 12


def evidence_text(item: Evidence) -> str:
    """Searchable text of one evidence item: its JSON plus its unescaped string values."""
    data = item.model_dump(mode="json")
    parts = [json.dumps(data, sort_keys=True)]
    parts.extend(_strings(data))
    return "\n".join(parts).casefold()


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def named_tokens(claim: str) -> list[str]:
    """Identifiers and quoted strings the claim names, de-duplicated, in order, capped."""
    found: list[str] = []
    for match in _QUOTED.finditer(claim):
        found.append(next(g for g in match.groups() if g is not None))
    found.extend(m.group(0) for m in _IDENTIFIER.finditer(claim))
    found.extend(m.group(0) for m in _HEX_ID.finditer(claim))
    seen: set[str] = set()
    tokens: list[str] = []
    for token in found:
        key = token.strip().casefold()
        if "/" in key and not any(c in key for c in "_. "):
            continue  # "and/or", "read/write": prose, not an identifier
        if key and key not in seen:
            seen.add(key)
            tokens.append(key)
    return tokens[:_MAX_TOKENS]


def is_absence_claim(claim: str) -> bool:
    return bool(_ABSENCE_START.search(claim) or _ABSENCE_PHRASES.search(claim))


def unsupported_reason(claim: str, cited_texts: Iterable[str]) -> str | None:
    """Return a generic reason the cited evidence cannot support the claim, or None."""
    if is_absence_claim(claim):
        return "claim asserts an absence or a negative relation, which evidence items cannot show"
    haystack = "\n".join(cited_texts)
    missing = [t for t in named_tokens(claim) if t not in haystack]
    if missing:
        return (
            f"{len(missing)} identifier(s) named in the claim do not appear in the cited evidence"
        )
    return None
