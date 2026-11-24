"""Auto-off delay for devices with the ``auto_off`` component."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KasaTpapConfigEntry
from .client import MAX_AUTO_OFF_MINUTES
from .coordinator import KasaTpapCoordinator
from .entity import KasaTpapEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaTpapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the auto-off delay."""
    coordinator = entry.runtime_data
    if coordinator.data.auto_off is not None:
        async_add_entities([KasaTpapAutoOffDelay(coordinator)])


class KasaTpapAutoOffDelay(KasaTpapEntity, NumberEntity):
    """Minutes after which the relay turns itself off (when auto-off is on)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "auto_off_delay"
    _attr_native_min_value = 1
    _attr_native_max_value = MAX_AUTO_OFF_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: KasaTpapCoordinator) -> None:
        """Create the entity."""
        super().__init__(coordinator, "auto_off_delay")

    @property
    def available(self) -> bool:
        """Unavailable while the auto-off setting is unknown."""
        return super().available and self.coordinator.data.auto_off is not None

    @property
    def native_value(self) -> float | None:
        """Return the delay in minutes."""
        auto_off = self.coordinator.data.auto_off
        return auto_off.delay_min if auto_off else None

    async def async_set_native_value(self, value: float) -> None:
        """Change the delay, keeping auto-off enabled or disabled."""
        auto_off = self.coordinator.data.auto_off
        if auto_off is None:
            return
        client, enabled, delay = self.coordinator.client, auto_off.enabled, int(value)
        await self.coordinator.async_command(
            lambda: client.async_set_auto_off(enabled, delay), settings_changed=True
        )
