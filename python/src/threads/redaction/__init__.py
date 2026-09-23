"""Secret redaction (spec/schema/README.md "Secret redaction", C5).

Every credential the host resolves is registered; nothing recorded holds one. Event data is
redacted where the writer stores it (`store/lines.py`); text beside events is redacted where it
is written; bytes that must stay byte-exact are refused (`contains_secret`).
"""

from threads.redaction.registry import forget_secrets, register
from threads.redaction.stored import (
    SecretInProviderOutputError,
    StreamRedactor,
    contains_secret,
    redact_bytes,
)
from threads.redaction.text import redact_json, redact_secrets

__all__ = [
    "SecretInProviderOutputError",
    "StreamRedactor",
    "contains_secret",
    "forget_secrets",
    "redact_bytes",
    "redact_json",
    "redact_secrets",
    "register",
]
