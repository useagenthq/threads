"""`A2A-Version` (pinned spec section 5.5). We speak 1.0 and nothing else: a 0.3 peer is refused
in both directions. Clients MUST send the header and MAY send it as a query parameter instead, so
inbound we read the header first and then the parameter. Both absent means 0.3 by the spec's own
rule, which is VersionNotSupportedError, not a default to 1.0."""

import re
from typing import Final

from threads.a2a.protocol.errors import A2aFault, fault

A2A_VERSION: Final = "1.0"
VERSION_HEADER: Final = "A2A-Version"
EXTENSIONS_HEADER: Final = "A2A-Extensions"

A2A_JSON: Final = "application/a2a+json"
"""The content type the HTTP+JSON binding prefers; `application/json` is accepted inbound."""

_NUMBER: Final = re.compile(r"^\d+$")
_DOTTED: Final = 2
"""A version is compared as Major.Minor, so two dotted numbers are all we read."""


def major_minor(version: str) -> str:
    """`1.0.1` to `1.0`; anything that is not two or three dotted numbers stays as it came."""
    parts = version.split(".")
    if len(parts) >= _DOTTED and _NUMBER.match(parts[0]) and _NUMBER.match(parts[1]):
        return f"{parts[0]}.{parts[1]}"
    return version


def check_version(header: str | None, query: str | None) -> A2aFault | None:
    """The version a request declares, checked against ours. Only `Major.Minor` is compared, as
    the spec requires, so `1.0.1` is 1.0 and `1.0` with surrounding space is still 1.0."""
    # A header that is present but empty is absent, so the query parameter is still read: the spec
    # lets a client send the version as a parameter INSTEAD of the header.
    raw = (header or "").strip() or (query or "").strip()
    if raw == "":
        return fault(
            "VersionNotSupportedError",
            f"no {VERSION_HEADER}, which the specification reads as 0.3; "
            f"this agent speaks {A2A_VERSION}",
        )
    if major_minor(raw) == A2A_VERSION:
        return None
    return fault(
        "VersionNotSupportedError",
        f"{VERSION_HEADER} {raw} is not supported; this agent speaks {A2A_VERSION}",
    )


def speaks_1_0(protocol_version: str) -> bool:
    """A card interface's version, for picking the one we can speak."""
    return major_minor(protocol_version) == A2A_VERSION
