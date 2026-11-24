"""Light entity for Kasa dimmers (devices with the ``brightness`` component)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import ATTR_BRIGHTNESS, ColorMode
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
    """Add the light of a dimmer."""
    coordinator = entry.runtime_data
    if coordinator.has_brightness and coordinator.data.info.is_on is not None:
        async_add_entities([KasaTpapDimmer(coordinator)])


def _to_device(brightness: int) -> int:
    """Home Assistant 0-255 to device 1-100 %."""
    return max(1, min(100, round(brightness * 100 / 255)))


class KasaTpapDimmer(KasaTpapEntity, LightEntity):
    """A dimmer switch such as the KS225 or HS220."""

    _attr_name = None
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: KasaTpapCoordinator) -> None:
        """Create the entity."""
        super().__init__(coordinator, None)

    @property
    def is_on(self) -> bool | None:
        """Return whether the light is on."""
        return self.coordinator.data.info.is_on

    @property
    def brightness(self) -> int | None:
        """Return the brightness on Home Assistant's 0-255 scale."""
        level = self.coordinator.data.info.brightness
        return None if level is None else round(level * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, optionally at a brightness."""
        client = self.coordinator.client
        if (brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            level = _to_device(int(brightness))
            await self.coordinator.async_command(
                lambda: client.async_set_brightness(level)
            )
        else:
            await self.coordinator.async_command(
                lambda: client.async_set_device_on(True)
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        client = self.coordinator.client
        await self.coordinator.async_command(lambda: client.async_set_device_on(False))
