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


def _nets(*cidrs: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(c) for c in cidrs)


# spec/schema/README.md, SSRF guard: the shared lists, pinned by conformance/vectors/ssrf.json.
_V4_DENIED: Final = _nets(
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/3",
)
_V6_GLOBAL: Final = ipaddress.ip_network("2000::/3")
_V6_DENIED: Final = _nets("2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20")
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")


def blocked(address: str) -> bool:
    """IPv4 outside the denied ranges is public. IPv6 is public only inside 2000::/3 minus
    Teredo and the IETF assignments, documentation, 6to4 and 3fff::/20; IPv4-mapped and NAT64
    addresses are judged as the IPv4 they carry. Anything unparseable is blocked."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        else:
            return ip not in _V6_GLOBAL or any(ip in net for net in _V6_DENIED)
    return any(ip in net for net in _V4_DENIED)


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
