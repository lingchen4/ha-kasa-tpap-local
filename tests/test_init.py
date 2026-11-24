"""Tests of setup, the switch, polling, lifecycle and diagnostics."""

from __future__ import annotations

import dataclasses
from collections.abc import Generator
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.kasa_tpap import async_remove_config_entry_device
from custom_components.kasa_tpap.client import (
    AutoOffState,
    ChildState,
    DeviceInfo,
    EnergyState,
    LedState,
)
from custom_components.kasa_tpap.const import CONF_SCAN_INTERVAL, DOMAIN
from custom_components.kasa_tpap.exceptions import (
    TpapAuthError,
    TpapConfirmationError,
    TpapConnectionError,
    TpapDeviceError,
    TpapDeviceLockedError,
)

MAC = "74:fe:ce:12:34:56"
ENTITY_ID = "switch.porch"
INFO = DeviceInfo(
    device_id="device-id",
    mac=MAC,
    model="HS200",
    hardware_version="5.26",
    firmware_version="1.4.0 Build 260910 Rel.183540",
    nickname="porch",
    is_on=False,
    rssi=-32,
    device_type="SMART.KASASWITCH",
)
DATA = {
    CONF_HOST: "192.168.50.65",
    CONF_USERNAME: "user@example.invalid",
    CONF_PASSWORD: "not-a-real-password",
}


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Replace the TPAP client used by the integration."""
    client = MagicMock()
    client.host = DATA[CONF_HOST]
    client.has_session = True
    client.async_get_device_info = AsyncMock(return_value=INFO)
    client.async_set_device_on = AsyncMock()
    client.async_set_brightness = AsyncMock()
    client.async_set_child_on = AsyncMock()
    client.async_get_components = AsyncMock(return_value=frozenset({"device"}))
    client.async_get_energy = AsyncMock()
    client.async_get_children = AsyncMock(return_value=())
    client.async_get_led = AsyncMock(
        return_value=LedState(rule="always", raw={"led_rule": "always"})
    )
    client.async_set_led = AsyncMock()
    client.async_get_auto_off = AsyncMock(
        return_value=AutoOffState(enabled=False, delay_min=120)
    )
    client.async_set_auto_off = AsyncMock()
    client.async_reboot = AsyncMock()
    client.async_close = AsyncMock()
    with patch("custom_components.kasa_tpap.TpapClient", return_value=client):
        yield client


@pytest.fixture
def http_sessions() -> Generator[list[aiohttp.ClientSession]]:
    """Record the HTTP sessions the integration creates."""
    created: list[aiohttp.ClientSession] = []
    from custom_components.kasa_tpap import create_http_session as real

    def _create() -> aiohttp.ClientSession:
        session = real()
        created.append(session)
        return session

    with patch("custom_components.kasa_tpap.create_http_session", side_effect=_create):
        yield created


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured porch switch."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MAC,
        title="porch",
        data=dict(DATA),
        options={CONF_SCAN_INTERVAL: 30},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_creates_switch_and_device(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_client: MagicMock,
    http_sessions: list[aiohttp.ClientSession],
) -> None:
    """The switch and its device carry the device's identity."""
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == STATE_OFF

    entity = er.async_get(hass).async_get(ENTITY_ID)
    assert entity is not None
    assert entity.unique_id == MAC

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, MAC), entry.entry_id
    )
    assert device is not None
    assert device.manufacturer == "TP-Link"
    assert device.model == "HS200"
    assert device.hw_version == "5.26"
    assert device.sw_version == "1.4.0 Build 260910 Rel.183540"
    # No MAC connection: never merged with the official TP-Link device.
    assert device.connections == set()


async def test_turn_on_off_updates_immediately(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A command is followed by a read of the device's real state."""
    await _setup(hass, entry)

    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, is_on=True
    )
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )
    mock_client.async_set_device_on.assert_awaited_with(True)
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    mock_client.async_get_device_info.return_value = INFO
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )
    mock_client.async_set_device_on.assert_awaited_with(False)
    assert hass.states.get(ENTITY_ID).state == STATE_OFF


async def test_command_failure_raises(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A failed command surfaces as a Home Assistant error."""
    await _setup(hass, entry)
    mock_client.async_set_device_on.side_effect = TpapConnectionError("unreachable")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
        )


async def test_physical_change_unavailable_and_recovery(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Polling picks up wall-switch changes, outages and recovery."""
    await _setup(hass, entry)
    coordinator = entry.runtime_data

    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, is_on=True
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON

    mock_client.async_get_device_info.side_effect = TpapConnectionError("gone")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    # Exponential back-off while unreachable
    assert coordinator.update_interval == timedelta(seconds=60)

    mock_client.async_get_device_info.side_effect = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=181))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_ON
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Rejected credentials stop polling and ask the user."""
    await _setup(hass, entry)
    mock_client.async_get_device_info.side_effect = TpapAuthError("rejected")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_lockout_waits_without_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A device lockout backs polling off instead of asking for credentials."""
    await _setup(hass, entry)
    mock_client.async_get_device_info.side_effect = TpapDeviceLockedError(
        "locked", locked_minutes=20
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval == timedelta(minutes=20)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_setup_retry_when_unreachable(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_client: MagicMock,
    http_sessions: list[aiohttp.ClientSession],
) -> None:
    """An unreachable device retries setup and leaks no HTTP session."""
    mock_client.async_get_device_info.side_effect = TpapConnectionError("gone")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert http_sessions
    assert http_sessions[0].closed


async def test_reload_and_unload_close_sessions(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_client: MagicMock,
    http_sessions: list[aiohttp.ClientSession],
) -> None:
    """Reload replaces the session; unload closes it."""
    await _setup(hass, entry)
    first = http_sessions[0]

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert first.closed
    assert len(http_sessions) == 2

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert all(session.closed for session in http_sessions)
    mock_client.async_close.assert_awaited()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def test_options_change_reloads_interval(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A new polling interval applies after the automatic reload."""
    await _setup(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.update_interval == timedelta(seconds=120)


async def test_device_removal(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Stale devices can be removed; the active one is removed with the entry."""
    await _setup(hass, entry)
    registry = dr.async_get(hass)
    active = registry.async_get_device_by_identifier((DOMAIN, MAC), entry.entry_id)
    assert active is not None
    assert not await async_remove_config_entry_device(hass, entry, active)

    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "00:00:00:00:00:01")}
    )
    assert await async_remove_config_entry_device(hass, entry, stale)


async def test_two_devices(hass: HomeAssistant, mock_client: MagicMock) -> None:
    """Several entries run side by side."""
    for index, mac in enumerate((MAC, "74:fe:ce:00:00:02")):
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=mac,
            data={**DATA, CONF_HOST: f"192.168.50.{70 + index}"},
        ).add_to_hass(hass)
    # Setting up the integration sets up every entry of the domain.
    first = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 2
    assert all(e.state is ConfigEntryState.LOADED for e in entries)
    assert len(hass.states.async_entity_ids(SWITCH_DOMAIN)) == 2


async def test_diagnostics_are_redacted(
    hass: HomeAssistant,
    hass_client,
    entry: MockConfigEntry,
    mock_client: MagicMock,
) -> None:
    """No credential or identifier appears in diagnostics."""
    await _setup(hass, entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    text = str(diagnostics)
    for secret in (
        DATA[CONF_PASSWORD],
        DATA[CONF_USERNAME],
        DATA[CONF_HOST],
        MAC,
        "device-id",
        "porch",
    ):
        assert secret not in text
    assert diagnostics["device"]["info"]["model"] == "HS200"
    assert diagnostics["components"] == ["device"]
    assert diagnostics["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"


async def test_hs200_has_only_a_switch(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A plain switch gets one switch entity, no light or sensors."""
    await _setup(hass, entry)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert sorted(e.entity_id for e in entities) == [
        "button.porch_restart",
        "sensor.porch_wi_fi_signal",
        ENTITY_ID,
    ]
    mock_client.async_get_energy.assert_not_awaited()
    mock_client.async_get_children.assert_not_awaited()


async def test_dimmer_is_a_light(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Devices with the brightness component become a dimmable light."""
    mock_client.async_get_components.return_value = frozenset({"brightness"})
    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, model="KS225", brightness=40
    )
    await _setup(hass, entry)
    assert hass.states.get(ENTITY_ID) is None
    state = hass.states.get("light.porch")
    assert state is not None
    assert state.state == STATE_OFF

    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, model="KS225", brightness=50, is_on=True
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {ATTR_ENTITY_ID: "light.porch", "brightness": 128},
        blocking=True,
    )
    mock_client.async_set_brightness.assert_awaited_once_with(50)
    state = hass.states.get("light.porch")
    assert state.state == STATE_ON
    assert state.attributes["brightness"] == 128

    await hass.services.async_call(
        "light", "turn_off", {ATTR_ENTITY_ID: "light.porch"}, blocking=True
    )
    mock_client.async_set_device_on.assert_awaited_with(False)


async def test_energy_plug_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Energy-monitoring plugs get power and energy sensors."""
    mock_client.async_get_components.return_value = frozenset(
        {"device", "energy_monitoring"}
    )
    mock_client.async_get_energy.return_value = EnergyState(
        power_w=26.2, today_kwh=0.029, month_kwh=0.91
    )
    await _setup(hass, entry)
    assert hass.states.get(ENTITY_ID) is not None
    power = hass.states.get("sensor.porch_current_power")
    energy = hass.states.get("sensor.porch_today_s_energy")
    assert power is not None
    assert float(power.state) == pytest.approx(26.2)
    assert power.attributes["unit_of_measurement"] == "W"
    assert energy is not None
    assert float(energy.state) == pytest.approx(0.029)
    assert energy.attributes["unit_of_measurement"] == "kWh"


async def test_power_strip_outlets(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Each outlet is a switch on its own child device."""
    mock_client.async_get_components.return_value = frozenset(
        {"device", "control_child"}
    )
    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, model="EP40M", is_on=None
    )
    mock_client.async_get_children.return_value = (
        ChildState("child-2", "Fan", True, 2),
        ChildState("child-1", None, False, 1),
    )
    await _setup(hass, entry)
    assert hass.states.get(ENTITY_ID) is None  # the strip has no relay
    assert hass.states.get("switch.porch_fan").state == STATE_ON
    assert hass.states.get("switch.porch_outlet_1").state == STATE_OFF

    registry = dr.async_get(hass)
    parent = registry.async_get_device_by_identifier((DOMAIN, MAC), entry.entry_id)
    assert parent is not None
    entity = er.async_get(hass).async_get("switch.porch_fan")
    assert entity.unique_id == f"{MAC}_child-2"

    mock_client.async_get_children.return_value = (
        ChildState("child-2", "Fan", False, 2),
        ChildState("child-1", None, False, 1),
    )
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.porch_fan"},
        blocking=True,
    )
    mock_client.async_set_child_on.assert_awaited_once_with("child-2", False)
    assert hass.states.get("switch.porch_fan").state == STATE_OFF

    # An outlet the strip stops reporting becomes unavailable.
    mock_client.async_get_children.return_value = (
        ChildState("child-1", None, False, 1),
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get("switch.porch_fan").state == STATE_UNAVAILABLE


async def test_energy_refusal_does_not_fail_poll(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A refused energy request leaves the switch working."""
    mock_client.async_get_components.return_value = frozenset(
        {"device", "energy_monitoring"}
    )
    mock_client.async_get_energy.side_effect = TpapDeviceError("x", error_code=-1)
    await _setup(hass, entry)
    assert hass.states.get(ENTITY_ID).state == STATE_OFF
    assert hass.states.get("sensor.porch_current_power").state == "unknown"


async def test_strip_base_with_energy_component(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A strip base announcing energy_monitoring (P304M) is not read for energy."""
    mock_client.async_get_components.return_value = frozenset(
        {"device", "energy_monitoring", "control_child"}
    )
    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, is_on=None
    )
    mock_client.async_get_children.return_value = (
        ChildState("child-1", "Lamp", True, 1),
    )
    await _setup(hass, entry)
    mock_client.async_get_energy.assert_not_awaited()
    assert hass.states.get("switch.porch_lamp").state == STATE_ON
    assert hass.states.async_entity_ids("sensor") == ["sensor.porch_wi_fi_signal"]


async def test_command_ok_but_readback_fails(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A successful command is not reported as failed."""
    await _setup(hass, entry)
    mock_client.async_get_device_info.side_effect = TpapConnectionError("blip")
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )
    mock_client.async_set_device_on.assert_awaited_with(True)


async def test_confirmation_failure_stops_polling_without_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """A device failing the PAKE confirmation is unavailable, no reauth."""
    await _setup(hass, entry)
    mock_client.async_get_device_info.side_effect = TpapConfirmationError("x")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def _setup_full_hs200(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_components.return_value = frozenset(
        {"device", "led", "auto_off"}
    )
    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, is_on=True, on_time=600
    )
    await _setup(hass, entry)


async def test_led_and_auto_off_entities(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """HS200 settings appear as configuration entities and can be changed."""
    await _setup_full_hs200(hass, entry, mock_client)
    registry = er.async_get(hass)
    for entity_id in (
        "switch.porch_led_indicator",
        "switch.porch_auto_off",
        "number.porch_auto_off_delay",
    ):
        assert registry.async_get(entity_id).entity_category == EntityCategory.CONFIG
    assert hass.states.get("switch.porch_led_indicator").state == STATE_ON
    assert hass.states.get("switch.porch_auto_off").state == STATE_OFF
    assert hass.states.get("number.porch_auto_off_delay").state == "120"

    mock_client.async_get_led.return_value = LedState("never", {"led_rule": "never"})
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: "switch.porch_led_indicator"},
        blocking=True,
    )
    led_arg, enabled = mock_client.async_set_led.await_args.args
    assert led_arg.rule == "always"
    assert enabled is False
    assert hass.states.get("switch.porch_led_indicator").state == STATE_OFF

    mock_client.async_get_auto_off.return_value = AutoOffState(True, 120)
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: "switch.porch_auto_off"},
        blocking=True,
    )
    mock_client.async_set_auto_off.assert_awaited_with(True, 120)
    assert hass.states.get("switch.porch_auto_off").state == STATE_ON

    mock_client.async_get_auto_off.return_value = AutoOffState(True, 15)
    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: "number.porch_auto_off_delay", "value": 15},
        blocking=True,
    )
    mock_client.async_set_auto_off.assert_awaited_with(True, 15)
    assert hass.states.get("number.porch_auto_off_delay").state == "15"


async def test_settings_are_read_once_a_minute(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """With a 3 s interval, LED/auto-off are not re-read on every poll."""
    hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL: 3})
    await _setup_full_hs200(hass, entry, mock_client)
    assert entry.runtime_data.update_interval == timedelta(seconds=3)
    led_reads = mock_client.async_get_led.await_count
    info_reads = mock_client.async_get_device_info.await_count
    for _ in range(5):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
        await hass.async_block_till_done()
    assert mock_client.async_get_device_info.await_count > info_reads
    assert mock_client.async_get_led.await_count == led_reads


async def test_diagnostic_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """Wi-Fi signal and On since are diagnostic sensors."""
    await _setup_full_hs200(hass, entry, mock_client)
    registry = er.async_get(hass)
    signal = hass.states.get("sensor.porch_wi_fi_signal")
    assert signal.state == "-32"
    assert signal.attributes["unit_of_measurement"] == "dBm"
    on_since = hass.states.get("sensor.porch_on_since")
    assert on_since is not None
    since = dt_util.parse_datetime(on_since.state)
    assert abs((dt_util.utcnow() - since).total_seconds() - 600) < 5
    for entity_id in ("sensor.porch_wi_fi_signal", "sensor.porch_on_since"):
        assert (
            registry.async_get(entity_id).entity_category == EntityCategory.DIAGNOSTIC
        )

    mock_client.async_get_device_info.return_value = dataclasses.replace(
        INFO, is_on=False, on_time=0
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.porch_on_since").state == "unknown"


async def test_restart_button(
    hass: HomeAssistant, entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    """The restart button reboots without waiting for a read-back."""
    await _setup(hass, entry)
    reads = mock_client.async_get_device_info.await_count
    await hass.services.async_call(
        "button", "press", {ATTR_ENTITY_ID: "button.porch_restart"}, blocking=True
    )
    mock_client.async_reboot.assert_awaited_once()
    assert mock_client.async_get_device_info.await_count == reads
