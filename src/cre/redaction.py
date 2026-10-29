"""Best-effort secret redaction for evidence before storage or model use.

Regex-based redaction cannot catch every secret format. It covers common keys, tokens,
password-style key/value pairs, connection-string passwords and sensitive dict keys.
"""

import re
from typing import Any

from cre.evidence import EVIDENCE_ADAPTER, Evidence

REDACTED = "[REDACTED]"

_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s:/@]*:)([^\s@/]+)(@)")
_KEY_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+"),
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]+=*")
_KEY_VALUE = re.compile(
    r"""(?ix)
    (?P<key>["']?(?:password|passwd|pwd|secret|token|api[_-]?key)["']?\s*[:=]\s*)
    (?:(?P<red>\[REDACTED\])|"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"',;&}\]]+))
    """
)
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "dsn",
    "connection_string",
    "database_url",
)


def _key_value_repl(match: re.Match[str]) -> str:
    key = match.group("key")
    if match.group("red") is not None:
        return match.group(0)
    if match.group("dq") is not None:
        return f'{key}"{REDACTED}"'
    if match.group("sq") is not None:
        return f"{key}'{REDACTED}'"
    return f"{key}{REDACTED}"


def redact_text(text: str) -> str:
    result = _URL_USERINFO.sub(rf"\g<1>{REDACTED}\g<3>", text)
    for pattern in _KEY_PATTERNS:
        result = pattern.sub(REDACTED, result)
    result = _BEARER.sub(rf"\g<1>{REDACTED}", result)
    return _KEY_VALUE.sub(_key_value_repl, result)


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_").replace(".", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (REDACTED if item is not None and is_sensitive_key(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_evidence(item: Evidence) -> Evidence:
    return EVIDENCE_ADAPTER.validate_python(redact(item.model_dump(mode="json")))
