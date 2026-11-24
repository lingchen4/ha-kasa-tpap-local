"""Validation of device addresses.

Only literal IP addresses inside the LAN ranges below are accepted. Host
names are refused on purpose: a name can later resolve to a different,
possibly public, address (DNS rebinding) and receive the login exchange.

The check is an allowlist, not ``ipaddress.is_private``, which also accepts
documentation, benchmarking and IETF ranges, and IPv6 transition addresses
(6to4 ``2002::/16``, Teredo ``2001::/32``) that embed public IPv4 addresses.
"""

from __future__ import annotations

import ipaddress

from .exceptions import InvalidHostError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

LAN_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv6Network("fc00::/7"),  # unique local addresses
)


def is_lan_address(address: IPAddress) -> bool:
    """Return whether the address is a unicast address in a LAN range."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in LAN_NETWORKS)


def validate_device_host(host: str) -> str:
    """Return the normalised address, or raise InvalidHostError."""
    candidate = (host or "").strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if "%" in candidate:
        raise InvalidHostError("Scoped IPv6 addresses are not supported")
    try:
        address: IPAddress = ipaddress.ip_address(candidate)
    except ValueError as err:
        raise InvalidHostError("The device address must be an IP address") from err
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if not is_lan_address(address):
        raise InvalidHostError(
            "The device address must be a private LAN address "
            "(10/8, 172.16/12, 192.168/16 or fc00::/7)"
        )
    return str(address)
