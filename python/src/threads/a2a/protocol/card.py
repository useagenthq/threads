"""Reading an agent card's security schemes. The proto's `oneof scheme` is keyed by the set field's
own name in JSON (`{"httpAuthSecurityScheme": {...}}`), which is the form the pinned spec's own
sample card uses."""

from typing import Final, Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import SecurityScheme

type SchemeKind = Literal[
    "apiKeySecurityScheme",
    "httpAuthSecurityScheme",
    "oauth2SecurityScheme",
    "openIdConnectSecurityScheme",
    "mtlsSecurityScheme",
]

SCHEME_KEYS: Final[tuple[SchemeKind, ...]] = (
    "apiKeySecurityScheme",
    "httpAuthSecurityScheme",
    "oauth2SecurityScheme",
    "openIdConnectSecurityScheme",
    "mtlsSecurityScheme",
)


def scheme_kind(scheme: SecurityScheme) -> SchemeKind | None:
    """The one scheme a card entry declares; None when it declares none or several."""
    found: list[SchemeKind] = [
        key for key in SCHEME_KEYS if getattr(scheme, key, MISSING) is not MISSING
    ]
    return found[0] if len(found) == 1 else None


def is_bearer(scheme: SecurityScheme) -> bool:
    """Whether this entry asks for an HTTP `bearer` token, the only credential we send."""
    http = scheme.httpAuthSecurityScheme
    if scheme_kind(scheme) != "httpAuthSecurityScheme" or http is MISSING:
        return False
    return http.scheme.lower() == "bearer"
