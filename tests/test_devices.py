"""Client tests for the supported device types, and discovery."""

from __future__ import annotations

from collections.abc import Callable

import aiohttp
import pytest

from custom_components.kasa_tpap import discovery
from custom_components.kasa_tpap.client import ChildState, TpapClient
from custom_components.kasa_tpap.exceptions import (
    TpapDeviceError,
    TpapProtocolError,
    TpapUnsupportedError,
)

from .device_simulator import SimulatedDevice, child

MakeClient = Callable[..., TpapClient]


async def test_wall_switch(device: SimulatedDevice, make_client: MakeClient) -> None:
    """HS200: on/off only."""
    client = make_client()
    assert await client.async_get_components() == frozenset({"device", "led"})
    info = await client.async_get_device_info()
    assert info.device_type == "SMART.KASASWITCH"
    assert info.is_on is False
    assert info.brightness is None


async def test_plug_with_energy_monitoring(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """KP125M/EP25: get_energy_usage reports mW and Wh."""
    device.model, device.device_type = "EP25", "SMART.KASAPLUG"
    device.components = ["device", "energy_monitoring"]
    device.energy = {"current_power": 26219, "today_energy": 29, "month_energy": 910}
    client = make_client()
    assert "energy_monitoring" in await client.async_get_components()
    energy = await client.async_get_energy()
    assert energy.power_w == pytest.approx(26.219)
    assert energy.today_kwh == pytest.approx(0.029)
    assert energy.month_kwh == pytest.approx(0.91)


async def test_energy_falls_back_to_get_current_power(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Firmware without current_power in get_energy_usage (W fallback)."""
    device.energy = {"today_energy": 5}
    device.current_power_w = 12
    energy = await make_client().async_get_energy()
    assert energy.power_w == 12
    assert device.calls["request"] == 2


async def test_dimmer(device: SimulatedDevice, make_client: MakeClient) -> None:
    """KS225/HS220: brightness in percent, set together with device_on."""
    device.model, device.components, device.brightness = "KS225", ["brightness"], 25
    client = make_client()
    assert (await client.async_get_device_info()).brightness == 25
    await client.async_set_brightness(80)
    assert device.is_on is True
    assert device.brightness == 80
    assert device.requests[-1]["params"] == {"device_on": True, "brightness": 80}
    with pytest.raises(ValueError):
        await client.async_set_brightness(0)


async def test_power_strip(device: SimulatedDevice, make_client: MakeClient) -> None:
    """EP40M/P300: outlets are listed in pages and switched via control_child."""
    device.model, device.device_type = "EP40M", "SMART.KASAPLUG"
    device.is_on = None
    device.components = ["device", "control_child", "child_device"]
    device.children = [
        child("child-1", 1, b"Lamp"),
        child("child-2", 2, b"Fan", on=True),
        child("child-3", 3, b"TV"),
    ]
    client = make_client()
    assert (await client.async_get_device_info()).is_on is None
    children = await client.async_get_children()
    assert children == (
        ChildState("child-1", "Lamp", False, 1),
        ChildState("child-2", "Fan", True, 2),
        ChildState("child-3", "TV", False, 3),
    )
    assert device.calls["request"] == 3  # info + two pages

    await client.async_set_child_on("child-3", True)
    assert device.children[2]["device_on"] is True
    sent = device.requests[-1]
    assert sent["method"] == "control_child"
    assert sent["params"]["requestData"] == {
        "method": "set_device_info",
        "params": {"device_on": True},
    }
    with pytest.raises(TpapDeviceError):
        await client.async_set_child_on("missing", True)


async def test_legacy_protocol_is_unsupported(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A device refusing component_nego does not speak the SMART protocol."""
    device.components = []

    async def _refuse() -> None:
        raise TpapDeviceError("x", error_code=-1)

    client = make_client()
    client.async_request = lambda *a, **k: _refuse()  # type: ignore[method-assign]
    with pytest.raises(TpapUnsupportedError):
        await client.async_get_components()


# ------------------------------------------------------------- discovery


def test_scan_targets() -> None:
    """Only enabled adapters, LAN ranges, at most a /24, never HA itself."""
    adapters = [
        {
            "enabled": True,
            "ipv4": [{"address": "192.168.50.10", "network_prefix": 24}],
        },
        {"enabled": True, "ipv4": [{"address": "10.1.2.3", "network_prefix": 16}]},
        {"enabled": False, "ipv4": [{"address": "172.16.0.5", "network_prefix": 24}]},
        {"enabled": True, "ipv4": [{"address": "8.8.4.4", "network_prefix": 24}]},
        {"enabled": True, "ipv4": [{"address": "bogus", "network_prefix": 24}]},
    ]
    targets = discovery.scan_targets(adapters)
    assert "192.168.50.65" in targets
    assert "192.168.50.10" not in targets
    assert "10.1.2.200" in targets
    assert "10.1.3.1" not in targets  # /16 narrowed to the /24 around HA
    assert not any(t.startswith(("172.16.", "8.8.")) for t in targets)
    assert len(targets) == 253 + 253


async def test_probe_and_scan(
    device: SimulatedDevice, allow_loopback: None, socket_enabled: None
) -> None:
    """A probe returns the MAC of a TPAP device and sends no credentials."""
    port = device.port
    async with aiohttp.ClientSession() as session:
        found = await discovery.async_probe(session, "127.0.0.1", port=port)
        assert found == discovery.DiscoveredDevice("127.0.0.1", "74:fe:ce:12:34:56")
        assert device.calls["pake_register"] == 0

        # Nothing listening there: not a device
        assert await discovery.async_probe(session, "127.0.0.1", 0.5, 1) is None

        device.discover_reply = "<html><body><center>200 OK</center></body></html>"
        assert await discovery.async_probe(session, "127.0.0.1", port=port) is None

        device.discover_reply = None
        results = await discovery.async_scan(session, ["127.0.0.1"] * 3, port=port)
        assert [d.mac for d in results] == ["74:fe:ce:12:34:56"]


async def test_probe_does_not_follow_redirects(
    device: SimulatedDevice, allow_loopback: None, socket_enabled: None
) -> None:
    """A redirecting host is not a device."""
    device.redirect_to = "http://127.0.0.1:1/"
    async with aiohttp.ClientSession() as session:
        client = TpapClient("127.0.0.1", "", "", session, port=device.port)
        with pytest.raises(TpapProtocolError, match="redirect"):
            await client.async_discover()
