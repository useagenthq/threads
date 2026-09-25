"""The connection string holds a password (DATABASE_URL): no error may carry it. A malformed one is
invalid_config naming nothing of it; the driver's connect errors are scrubbed of the string and
its password, and never chained to psycopg's own."""

import re
from itertools import pairwise
from urllib.parse import unquote, urlsplit

_PASSWORD = re.compile(r"password\s*=\s*('(?:\\.|[^'])*'|\S+)")


def scrub(text: str, dsn: str) -> str:
    """`text` without `dsn` or any password in it."""
    for secret in sorted(_secrets(dsn), key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return text


def _secrets(dsn: str) -> set[str]:
    found = {dsn}
    for match in _PASSWORD.finditer(dsn):
        value = match.group(1)
        found |= {value, value.strip("'")}
    words = dsn.split()
    for before, after in pairwise(words):
        if before == "password":  # a malformed `password value`
            found.add(after)
    try:
        password = urlsplit(dsn).password
    except ValueError:
        password = None
    if password:
        found |= {password, unquote(password)}
    return {s for s in found if s}
