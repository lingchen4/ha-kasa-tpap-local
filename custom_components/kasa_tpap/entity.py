"""Base entity for Kasa TPAP Local."""

from __future__ import annotations

from homeassistant.const import CONF_NAME
from homeassistant.helpers.device_registry import DeviceInfo as HADeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import KasaTpapCoordinator


def device_identifier(coordinator: KasaTpapCoordinator) -> str:
    """Return the stable device id (the MAC, set as the entry's unique id)."""
    return coordinator.config_entry.unique_id or coordinator.data.info.mac


class KasaTpapEntity(CoordinatorEntity[KasaTpapCoordinator]):
    """An entity of the device itself.

    The device is registered with this integration's identifier only, not a
    MAC "connection", so it is never merged with a device of the official
    TP-Link integration.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: KasaTpapCoordinator, key: str | None) -> None:
        """Set the unique id and device information."""
        super().__init__(coordinator)
        identifier = device_identifier(coordinator)
        info = coordinator.data.info
        entry = coordinator.config_entry
        self._attr_unique_id = identifier if key is None else f"{identifier}_{key}"
        self._attr_device_info = HADeviceInfo(
            identifiers={(DOMAIN, identifier)},
            manufacturer=MANUFACTURER,
            model=info.model,
            model_id=info.device_type,
            hw_version=info.hardware_version,
            sw_version=info.firmware_version,
            name=entry.data.get(CONF_NAME) or info.nickname or info.model,
        )
