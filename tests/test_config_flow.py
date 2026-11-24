"""Tests of the config, reauth, reconfigure and options flows."""

from __future__ import annotations

import dataclasses
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import probatio as vol
import pytest
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.kasa_tpap.client import DeviceInfo
from custom_components.kasa_tpap.const import CONF_SCAN_INTERVAL, DOMAIN
from custom_components.kasa_tpap.discovery import DiscoveredDevice
from custom_components.kasa_tpap.exceptions import (
    TpapAuthError,
    TpapConfirmationError,
    TpapConnectionError,
    TpapDeviceLockedError,
    TpapDiscoveryError,
    TpapProtocolError,
    TpapTimeoutError,
    TpapUnsupportedError,
)

MAC = "74:fe:ce:12:34:56"
INFO = DeviceInfo(
    device_id="device-id",
    mac=MAC,
    model="HS200",
    hardware_version="5.26",
    firmware_version="1.4.0 Build 260910 Rel.183540",
    nickname="porch",
    is_on=False,
    rssi=-32,
)
USER_INPUT = {
    CONF_HOST: "192.168.50.65",
    CONF_USERNAME: "user@example.invalid",
    CONF_PASSWORD: "not-a-real-password",
}


async def _start_manual(hass: HomeAssistant) -> Any:
    """Open the flow and pick "Enter the IP address manually"."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["scan", "manual"]
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )


@pytest.fixture
def validate() -> Generator[AsyncMock]:
    """Replace the real login used by the flows."""
    with patch(
        "custom_components.kasa_tpap.config_flow.async_validate_device",
        AsyncMock(return_value=INFO),
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def no_setup() -> Generator[None]:
    """Do not set entries up after the flow creates them."""
    with patch("custom_components.kasa_tpap.async_setup_entry", return_value=True):
        yield


async def test_user_flow(hass: HomeAssistant, validate: AsyncMock) -> None:
    """A valid device creates an entry keyed by its MAC."""
    result = await _start_manual(hass)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_NAME: " Porch light ", CONF_SCAN_INTERVAL: 45},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Porch light"
    assert result["result"].unique_id == MAC
    assert result["data"] == {**USER_INPUT, CONF_NAME: "Porch light"}
    assert result["options"] == {CONF_SCAN_INTERVAL: 45}
    validate.assert_awaited_once_with(
        "192.168.50.65", USER_INPUT[CONF_USERNAME], USER_INPUT[CONF_PASSWORD]
    )


async def test_user_flow_uses_device_nickname(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """Without a display name the device's own name is the title."""
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["title"] == "porch"
    assert CONF_NAME not in result["data"]
    assert result["options"] == {CONF_SCAN_INTERVAL: 30}


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (TpapAuthError("x"), "invalid_auth"),
        (TpapDeviceLockedError("x"), "device_locked"),
        (TpapTimeoutError("x"), "timeout"),
        (TpapConnectionError("x"), "cannot_connect"),
        (TpapDiscoveryError("x"), "tpap_discovery_failed"),
        (TpapUnsupportedError("x"), "unsupported_protocol"),
        (TpapConfirmationError("x"), "confirmation_failed"),
        (TpapProtocolError("x"), "protocol_error"),
        (RuntimeError("x"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recover(
    hass: HomeAssistant, validate: AsyncMock, error: Exception, key: str
) -> None:
    """Each failure has its own message; fixing it creates the entry."""
    validate.side_effect = error
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": key}

    validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "host", ["8.8.8.8", "127.0.0.1", "porch.local", "224.0.0.1", "169.254.1.1"]
)
async def test_user_flow_rejects_non_lan_hosts(
    hass: HomeAssistant, validate: AsyncMock, host: str
) -> None:
    """Public, loopback, named, multicast and link-local targets are refused."""
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: host}
    )
    assert result["errors"] == {CONF_HOST: "invalid_host"}
    validate.assert_not_awaited()


async def test_duplicate_device(hass: HomeAssistant, validate: AsyncMock) -> None:
    """The same device (MAC) cannot be added twice, even at a new address."""
    MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT)).add_to_hass(
        hass
    )
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "192.168.50.99"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_duplicate_host_aborts_before_login(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """A configured address is refused without logging in again."""
    MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT)).add_to_hass(
        hass
    )
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["reason"] == "already_configured"
    validate.assert_not_awaited()


async def test_reauth(hass: HomeAssistant, validate: AsyncMock) -> None:
    """New credentials are validated and stored."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT))
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    validate.side_effect = TpapAuthError("x")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "user@example.invalid", CONF_PASSWORD: "still-wrong"},
    )
    assert result["errors"] == {"base": "invalid_auth"}

    validate.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "user@example.invalid", CONF_PASSWORD: "new-password"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_reauth_wrong_device(hass: HomeAssistant, validate: AsyncMock) -> None:
    """Credentials proving a different device are refused."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT))
    entry.add_to_hass(hass)
    validate.return_value = dataclasses.replace(INFO, mac="74:fe:ce:99:99:99")
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "user@example.invalid", CONF_PASSWORD: "pw"},
    )
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]


async def test_reconfigure(hass: HomeAssistant, validate: AsyncMock) -> None:
    """The address and password can change for the same device."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT))
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "8.8.8.8"}
    )
    assert result["errors"] == {CONF_HOST: "invalid_host"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_HOST: "192.168.50.80", CONF_PASSWORD: "changed"},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.168.50.80"
    assert entry.data[CONF_PASSWORD] == "changed"


async def test_options_flow(hass: HomeAssistant) -> None:
    """The polling interval can be changed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data=dict(USER_INPUT),
        options={CONF_SCAN_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 90}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 90}


async def test_validate_device_closes_its_session(socket_enabled: None) -> None:
    """The flow's real validation closes the HTTP session even on failure."""
    from custom_components.kasa_tpap import config_flow

    sessions = []
    real = config_flow.create_http_session

    def _create():
        session = real()
        sessions.append(session)
        return session

    with (
        patch.object(config_flow, "create_http_session", side_effect=_create),
        patch(
            "custom_components.kasa_tpap.client.TpapClient.async_get_components",
            AsyncMock(side_effect=TpapConnectionError("gone")),
        ),
        pytest.raises(TpapConnectionError),
    ):
        await config_flow.async_validate_device(
            "192.168.50.65", "user@example.invalid", "pw"
        )
    assert sessions
    assert sessions[0].closed


# --------------------------------------------------------- saved account


async def test_saved_account_is_reused(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """A second device can reuse the stored account without retyping it."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="74:fe:ce:00:00:01", data=dict(USER_INPUT)
    ).add_to_hass(hass)
    result = await _start_manual(hass)
    schema = result["data_schema"].schema
    username_key = next(k for k in schema if k == CONF_USERNAME)
    assert username_key.default() == USER_INPUT[CONF_USERNAME]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "192.168.50.66", CONF_USERNAME: USER_INPUT[CONF_USERNAME]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]
    validate.assert_awaited_once_with(
        "192.168.50.66", USER_INPUT[CONF_USERNAME], USER_INPUT[CONF_PASSWORD]
    )


async def test_password_required_for_unknown_account(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """An empty password only works for an account already saved."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="74:fe:ce:00:00:01", data=dict(USER_INPUT)
    ).add_to_hass(hass)
    result = await _start_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "192.168.50.66", CONF_USERNAME: "other@example.invalid"},
    )
    assert result["errors"] == {CONF_PASSWORD: "password_required"}
    validate.assert_not_awaited()


async def test_reauth_updates_devices_sharing_the_account(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """A new TP-Link password is entered once for all devices of the account."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT))
    entry.add_to_hass(hass)
    sibling = MockConfigEntry(
        domain=DOMAIN,
        unique_id="74:fe:ce:00:00:02",
        data={**USER_INPUT, CONF_HOST: "192.168.50.70"},
    )
    sibling.add_to_hass(hass)
    stranger = MockConfigEntry(
        domain=DOMAIN,
        unique_id="74:fe:ce:00:00:03",
        data={**USER_INPUT, CONF_USERNAME: "other@example.invalid"},
    )
    stranger.add_to_hass(hass)

    unverified = MockConfigEntry(
        domain=DOMAIN,
        unique_id="74:fe:ce:00:00:04",
        data={**USER_INPUT, CONF_HOST: "192.168.50.71"},
    )
    unverified.add_to_hass(hass)
    macs = {
        USER_INPUT[CONF_HOST]: MAC,
        "192.168.50.70": "74:fe:ce:00:00:02",
    }

    async def _validate(host: str, username: str, password: str) -> DeviceInfo:
        if host not in macs:
            raise TpapAuthError("not synced yet")
        return dataclasses.replace(INFO, mac=macs[host])

    validate.side_effect = _validate
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USER_INPUT[CONF_USERNAME], CONF_PASSWORD: "new-password"},
    )
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"
    assert sibling.data[CONF_PASSWORD] == "new-password"  # verified first
    assert unverified.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]
    assert stranger.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]


async def test_reauth_with_other_account_is_not_shared(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """Switching one device to another account leaves the others alone."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT))
    entry.add_to_hass(hass)
    sibling = MockConfigEntry(
        domain=DOMAIN,
        unique_id="74:fe:ce:00:00:02",
        data={**USER_INPUT, CONF_HOST: "192.168.50.70"},
    )
    sibling.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: "other@example.invalid", CONF_PASSWORD: "pw2"},
    )
    assert entry.data[CONF_USERNAME] == "other@example.invalid"
    assert sibling.data == {**USER_INPUT, CONF_HOST: "192.168.50.70"}
    assert validate.await_count == 1


async def test_reconfigure_keeps_password_when_empty(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """Changing only the address does not require the password again."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT))
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "192.168.50.81", CONF_USERNAME: USER_INPUT[CONF_USERNAME]},
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == "192.168.50.81"
    assert entry.data[CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]


# -------------------------------------------------------------- discovery


def _probe(mac: str | None = MAC) -> AsyncMock:
    async def _fake(session: Any, host: str, *args: Any, **kwargs: Any) -> Any:
        return None if mac is None else DiscoveredDevice(host, mac)

    return patch(
        "custom_components.kasa_tpap.config_flow.async_probe", side_effect=_fake
    )


DHCP = DhcpServiceInfo(ip="192.168.50.65", hostname="hs200", macaddress="74fece123456")


async def test_dhcp_discovery_creates_entry(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """A TPAP device seen by DHCP is offered; the user only adds the account."""
    with _probe():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"] == {"host": "192.168.50.65"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_USERNAME: USER_INPUT[CONF_USERNAME], CONF_PASSWORD: "pw"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.50.65"


@pytest.mark.parametrize("probe_mac", [None, "74:fe:ce:99:99:99"])
async def test_dhcp_ignores_non_tpap_devices(
    hass: HomeAssistant, probe_mac: str | None
) -> None:
    """KLAP/AES devices (or a different device at the address) are ignored."""
    with _probe(probe_mac):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_tpap_device"


async def test_dhcp_rejects_non_lan_address(hass: HomeAssistant) -> None:
    """DHCP data pointing outside the LAN ranges is not probed."""
    with _probe() as probe:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_DHCP},
            data=DhcpServiceInfo(ip="8.8.8.8", hostname="x", macaddress="74fece123456"),
        )
    assert result["reason"] == "not_tpap_device"
    probe.assert_not_called()


@pytest.fixture(autouse=True)
def reset_dhcp_rate_limit() -> Generator[None]:
    """Each test starts without earlier DHCP address-change attempts."""
    from custom_components.kasa_tpap import config_flow

    config_flow._DHCP_ATTEMPTS.clear()
    yield
    config_flow._DHCP_ATTEMPTS.clear()


async def test_dhcp_follows_a_moved_device(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """An unreachable configured device is followed to its new address
    after a full login there succeeds, at most once an hour."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={**USER_INPUT, CONF_HOST: "192.168.50.20"},
    )
    entry.add_to_hass(hass)
    with _probe() as probe:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
        )
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.50.65"
    probe.assert_not_called()  # a real login is the check, not the probe
    validate.assert_awaited_once_with(
        "192.168.50.65", USER_INPUT[CONF_USERNAME], USER_INPUT[CONF_PASSWORD]
    )


async def test_dhcp_move_is_not_applied_without_login(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """A spoofed DHCP packet cannot move the device, and is rate-limited."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={**USER_INPUT, CONF_HOST: "192.168.50.20"},
    )
    entry.add_to_hass(hass)
    validate.side_effect = TpapConfirmationError("impostor")
    for _ in range(3):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
        )
        assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.50.20"
    assert validate.await_count == 1  # one attempt per hour


async def test_dhcp_ignores_other_address_of_working_device(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """While the device works at its stored address, DHCP cannot move it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        data={**USER_INPUT, CONF_HOST: "192.168.50.20"},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = MagicMock(last_update_success=True)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
    )
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.168.50.20"
    validate.assert_not_awaited()


async def test_dhcp_same_address_is_not_probed(hass: HomeAssistant) -> None:
    """Routine DHCP renewals of a configured device cause no traffic."""
    MockConfigEntry(domain=DOMAIN, unique_id=MAC, data=dict(USER_INPUT)).add_to_hass(
        hass
    )
    with _probe() as probe:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_DHCP}, data=DHCP
        )
    assert result["reason"] == "already_configured"
    probe.assert_not_called()


def _scan(devices: list[DiscoveredDevice]) -> Any:
    return patch(
        "custom_components.kasa_tpap.config_flow.async_scan",
        AsyncMock(return_value=devices),
    )


async def _run_scan(hass: HomeAssistant, devices: list[DiscoveredDevice]) -> Any:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _scan(devices):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "scan"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert result["progress_action"] == "scan"
        await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(result["flow_id"])


async def test_scan_finds_and_adds_device(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """Scan lists unconfigured TPAP devices; picking one creates the entry."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="74:fe:ce:00:00:09", data=dict(USER_INPUT)
    ).add_to_hass(hass)
    result = await _run_scan(
        hass,
        [
            DiscoveredDevice("192.168.50.65", MAC),
            DiscoveredDevice("192.168.50.77", "74:fe:ce:00:00:09"),  # configured
        ],
    )
    assert result["step_id"] == "pick"
    options = result["data_schema"].schema["device"].config["options"]
    assert [o["value"] for o in options] == ["192.168.50.65"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"device": "192.168.50.65", CONF_USERNAME: USER_INPUT[CONF_USERNAME]},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == "192.168.50.65"
    assert result["data"][CONF_PASSWORD] == USER_INPUT[CONF_PASSWORD]  # saved


async def test_scan_finds_nothing(hass: HomeAssistant) -> None:
    """No device: the flow explains how to continue."""
    result = await _run_scan(hass, [])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_saved_password_skips_entries_awaiting_reauth(
    hass: HomeAssistant, validate: AsyncMock
) -> None:
    """A password known to be wrong (reauth pending) is not reused."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="74:fe:ce:00:00:01", data=dict(USER_INPUT)
    )
    entry.add_to_hass(hass)
    await entry.start_reauth_flow(hass)
    result = await _start_manual(hass)
    schema = result["data_schema"].schema
    password_key = next(k for k in schema if k == CONF_PASSWORD)
    username_key = next(k for k in schema if k == CONF_USERNAME)
    assert isinstance(password_key, vol.Required)  # no saved password offered
    assert username_key.default() == ""
