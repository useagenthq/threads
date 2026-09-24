"""Principal keys: the normalized `issuer/tenant/subject` string, as TypeScript's principalKey."""

from threads._generated.events_v1 import Principal


def key_part(text: str) -> str:
    """One part of a principal key or memory scope: `%` then `/` escaped, so ("a/b", "c") and
    ("a", "b/c") never share a key. A part with neither character is unchanged."""
    return text.replace("%", "%25").replace("/", "%2F")


def principal_key(principal: Principal) -> str:
    return "/".join(key_part(p) for p in (principal.issuer, principal.tenant, principal.subject))
