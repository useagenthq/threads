"""Secret redaction (spec/schema/README.md "Secret redaction", C5).

Every credential the host resolves is registered; no recorded content holds one. Event data is
redacted where the writer stores it (`store/lines.py`); text beside events is redacted where it
is written; bytes that must stay byte-exact are refused (`contains_secret`).
"""

from threads.redaction.registry import forget_secrets, generation, register
from threads.redaction.stored import (
    SecretInProviderOutputError,
    SecretInStoredBytesError,
    StreamRedactor,
    contains_secret,
    published,
    redact_bytes,
    unchanged_since,
)
from threads.redaction.text import redact_json, redact_secrets

__all__ = [
    "SecretInProviderOutputError",
    "SecretInStoredBytesError",
    "StreamRedactor",
    "contains_secret",
    "forget_secrets",
    "generation",
    "published",
    "redact_bytes",
    "redact_json",
    "redact_secrets",
    "register",
    "unchanged_since",
]
