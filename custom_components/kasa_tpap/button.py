"""Restart button."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KasaTpapConfigEntry
from .coordinator import KasaTpapCoordinator
from .entity import KasaTpapEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaTpapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the restart button."""
    async_add_entities([KasaTpapRestartButton(entry.runtime_data)])


class KasaTpapRestartButton(KasaTpapEntity, ButtonEntity):
    """Restart the device; it is offline for about 30 seconds and reconnects."""

    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: KasaTpapCoordinator) -> None:
        """Create the entity."""
        super().__init__(coordinator, "restart")

    async def async_press(self) -> None:
        """Send the restart request (no read-back: the device is restarting)."""
        client = self.coordinator.client
        await self.coordinator.async_command(client.async_reboot, read_back=False)
