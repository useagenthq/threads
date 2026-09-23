"""The host web tools' SSRF guard: http and https only, and every address a
host resolves to must be public. The connection then goes to the address that was checked, so a
second DNS answer can't swap in a private one."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final
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
    if scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return None
    return scheme, parts.hostname.lower().rstrip("."), port or (443 if scheme == "https" else 80)


_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_DENIED: Final = (
    ipaddress.ip_network("192.88.99.0/24"),  # deprecated 6to4 relay anycast
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
    ipaddress.ip_network("5f00::/16"),  # SRv6 SIDs
)


def _carried(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one carries: mapped, compatible, NAT64 or 6to4."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    low = int(ip) & 0xFFFFFFFF
    if (int(ip) >> 32 == 0 and low > 1) or ip in _NAT64:
        return ipaddress.IPv4Address(low)
    return None


def blocked(address: str) -> bool:
    """Private, loopback, link-local (the metadata address among them), ULA, reserved,
    multicast and unspecified addresses are all not global. An IPv6 address carrying IPv4
    (mapped, compatible, NAT64, 6to4) is judged as that IPv4 address; Teredo and the other
    special IPv6 prefixes are not global."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and (inner := _carried(ip)) is not None:
        ip = inner
    return not ip.is_global or ip.is_multicast or any(ip in net for net in _DENIED)


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
