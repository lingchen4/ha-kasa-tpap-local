"""Tests of the device address validation."""

from __future__ import annotations

import pytest

from custom_components.kasa_tpap.exceptions import InvalidHostError
from custom_components.kasa_tpap.network import validate_device_host


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("192.168.50.65", "192.168.50.65"),
        (" 10.0.0.7 ", "10.0.0.7"),
        ("172.16.1.1", "172.16.1.1"),
        ("fd00::1", "fd00::1"),
        ("[fd12:3456::2]", "fd12:3456::2"),
        ("::ffff:192.168.1.2", "192.168.1.2"),
    ],
)
def test_accepts_private_lan_addresses(host: str, expected: str) -> None:
    """Private unicast addresses are accepted and normalised."""
    assert validate_device_host(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        "",
        "porch.local",
        "example.com",
        "8.8.8.8",  # public
        "2001:4860:4860::8888",  # public IPv6
        "127.0.0.1",  # loopback
        "::1",
        "169.254.10.10",  # link-local
        "fe80::1",
        "fe80::1%eth0",
        "224.0.0.251",  # multicast
        "ff02::1",
        "0.0.0.0",  # noqa: S104
        "255.255.255.255",
        "240.0.0.1",  # reserved
        "100.64.0.1",  # carrier-grade NAT, not a LAN range
        "2002:808:808::1",  # 6to4, embeds public 8.8.8.8
        "2001::1",  # Teredo
        "2001:db8::1",  # documentation
        "192.0.0.1",  # IETF protocol assignments
        "192.0.2.1",  # documentation
        "198.18.0.1",  # benchmarking
        "192.168.1.1/24",
        "http://192.168.1.2",
    ],
)
def test_rejects_other_destinations(host: str) -> None:
    """Anything but a private LAN IP is refused."""
    with pytest.raises(InvalidHostError):
        validate_device_host(host)
