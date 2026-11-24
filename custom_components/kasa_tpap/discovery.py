"""Finding TPAP devices on the local network, without UDP and without credentials.

Two mechanisms:

* DHCP: Home Assistant's DHCP watcher reports devices with TP-Link MAC
  prefixes (see ``manifest.json``); the config flow probes that one address.
* Scan: when the user picks "Scan network", every address of Home
  Assistant's own IPv4 LAN subnets (at most a /24 per interface) receives one
  HTTP ``login``/``discover`` request on port 80.

A probe sends no credentials and nothing secret; it is the same request the
TP-Link app sends, and only devices answering with a TPAP login that this
integration supports are reported.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp

from .client import DEFAULT_PORT, TpapClient
from .exceptions import TpapError
from .network import is_lan_address

_LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT = 2.0
SCAN_CONCURRENCY = 32
# Networks wider than this are only scanned around Home Assistant's address.
MIN_SCAN_PREFIX = 24
MAX_SCAN_HOSTS = 1024


@dataclass(frozen=True, slots=True)
class DiscoveredDevice:
    """A device that answered the TPAP discover."""

    host: str
    mac: str


async def async_probe(
    session: aiohttp.ClientSession,
    host: str,
    probe_timeout: float = PROBE_TIMEOUT,
    port: int = DEFAULT_PORT,
) -> DiscoveredDevice | None:
    """Return the device at ``host`` if it speaks a supported TPAP login."""
    try:
        client = TpapClient(
            host, "", "", session, port=port, request_timeout=probe_timeout
        )
        mac = await client.async_discover()
    except TpapError:
        return None
    return DiscoveredDevice(host=client.host, mac=mac) if mac else None


def scan_targets(adapters: Iterable[Mapping[str, Any]]) -> list[str]:
    """Return the addresses to probe for Home Assistant's network adapters.

    Only enabled adapters and private LAN subnets are used; a subnet wider
    than /24 is narrowed to the /24 around Home Assistant's own address.
    """
    targets: dict[str, None] = {}
    for adapter in adapters:
        if not adapter.get("enabled"):
            continue
        for entry in adapter.get("ipv4") or []:
            try:
                own = ipaddress.IPv4Address(entry["address"])
                prefix = max(int(entry["network_prefix"]), MIN_SCAN_PREFIX)
                network = ipaddress.IPv4Network(f"{own}/{prefix}", strict=False)
            except (KeyError, TypeError, ValueError):
                continue
            if not is_lan_address(own):
                continue
            for host in network.hosts():
                if host != own:
                    targets.setdefault(str(host))
    return list(targets)[:MAX_SCAN_HOSTS]


async def async_scan(
    session: aiohttp.ClientSession, hosts: list[str], port: int = DEFAULT_PORT
) -> list[DiscoveredDevice]:
    """Probe the hosts concurrently; return the TPAP devices found."""
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _probe(host: str) -> DiscoveredDevice | None:
        async with semaphore:
            return await async_probe(session, host, port=port)

    results = await asyncio.gather(*(_probe(h) for h in hosts))
    found = {d.mac: d for d in results if d is not None}
    _LOGGER.debug("Scan probed %d addresses, found %d", len(hosts), len(found))
    return sorted(found.values(), key=lambda d: ipaddress.ip_address(d.host))
