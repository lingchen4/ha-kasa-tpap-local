"""Kasa TPAP Local: local control of TP-Link Kasa devices that use TPAP."""

from __future__ import annotations

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .client import TpapClient
from .const import DOMAIN, MANUFACTURER
from .coordinator import KasaTpapCoordinator

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

type KasaTpapConfigEntry = ConfigEntry[KasaTpapCoordinator]


def create_http_session(limit: int = 2) -> aiohttp.ClientSession:
    """Return a dedicated HTTP session for one device, owned by its entry.

    Home Assistant's shared session must not be closed by integrations, so
    each entry owns one and closes it on unload. Cookies are never stored,
    proxy settings from the environment are ignored (``trust_env=False``), and
    connections are not kept alive, since the device's small HTTP server drops
    idle connections. ``limit`` caps parallel connections (raised only for
    the user-started network scan).
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(force_close=True, limit=limit),
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
    )


async def async_setup_entry(hass: HomeAssistant, entry: KasaTpapConfigEntry) -> bool:
    """Set up one device."""
    http_session = create_http_session()
    try:
        client = TpapClient(
            entry.data[CONF_HOST],
            entry.data[CONF_USERNAME],
            entry.data[CONF_PASSWORD],
            http_session,
        )
        coordinator = KasaTpapCoordinator(hass, entry, client)
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await http_session.close()
        raise

    async def _async_close() -> None:
        await client.async_close()
        await http_session.close()

    entry.async_on_unload(_async_close)
    entry.runtime_data = coordinator
    # Register the device itself: power-strip outlets refer to it as their
    # parent even when the strip has no entity of its own.
    info = coordinator.data.info
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.unique_id or info.mac)},
        manufacturer=MANUFACTURER,
        model=info.model,
        model_id=info.device_type,
        hw_version=info.hardware_version,
        sw_version=info.firmware_version,
        name=entry.data.get(CONF_NAME) or info.nickname or info.model,
    )
    coordinator.device_id = device.id
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KasaTpapConfigEntry) -> bool:
    """Unload a device; the on-unload callbacks close its HTTP session."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: KasaTpapConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing stale devices (e.g. an outlet no longer reported).

    The device itself and its current outlets are removed with the entry.
    """
    current = {(DOMAIN, entry.unique_id)}
    if (coordinator := getattr(entry, "runtime_data", None)) is not None:
        current |= {
            (DOMAIN, f"{entry.unique_id}_{child.device_id}")
            for child in coordinator.data.children
        }
    return not current & device.identifiers
