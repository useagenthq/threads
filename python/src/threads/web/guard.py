"""The host web tools' SSRF guard: http and https only, and every address a
host resolves to must be public. The connection then goes to the address that was checked, so a
second DNS answer can't swap in a private one."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from threads.result import Err, Ok

type Resolve = Callable[[str, int], Awaitable[Sequence[str]]]
"""A host and port to every address it resolves to."""


@dataclass(frozen=True, slots=True)
class Target:
    """A vetted request target: the URL's parts and the checked address to connect to."""

    url: str
    scheme: str
    host: str
    port: int
    path: str
    """Path and query, as sent on the request line."""
    ip: str


def origin(url: str) -> tuple[str, str, int] | None:
    """Scheme, lowercase host and effective port, or None for a URL the guard refuses."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    return scheme, parts.hostname.lower(), port or (443 if scheme == "https" else 80)


def blocked(address: str) -> bool:
    """Private, loopback, link-local (the metadata address among them), ULA, reserved,
    multicast and unspecified addresses are all not global; an IPv4-mapped IPv6 address is
    judged as its IPv4 address."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


async def system_resolve(host: str, port: int) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def vet(url: str, resolve: Resolve) -> Ok[Target] | Err[str]:
    """The target, or why it is refused."""
    found = origin(url)
    if found is None:
        return Err(f"permission_denied: only http and https URLs are fetched: {url}")
    scheme, host, port = found
    try:
        addresses = await resolve(host, port)
    except OSError as error:
        return Err(f"not_found: {host} did not resolve: {error}")
    if not addresses:
        return Err(f"not_found: {host} did not resolve")
    denied = [a for a in addresses if blocked(a)]
    if denied:
        return Err(f"permission_denied: {host} resolves to a non-public address {denied[0]}")
    parts = urlsplit(url)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Ok(Target(url, scheme, host, port, path, addresses[0]))
