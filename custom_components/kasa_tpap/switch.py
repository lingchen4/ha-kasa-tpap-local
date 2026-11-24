"""Switches: the relay of plugs and wall switches, and power-strip outlets."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import ChildDeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import KasaTpapConfigEntry
from .const import DOMAIN
from .coordinator import KasaTpapCoordinator
from .entity import KasaTpapEntity, device_identifier

# Commands are serialised by the client; HA need not limit them further.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaTpapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the relay switch and/or one switch per outlet."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = []
    # Dimmers get a light entity instead; strips report no parent relay.
    if coordinator.data.info.is_on is not None and not coordinator.has_brightness:
        entities.append(KasaTpapSwitch(coordinator))
    if (parent_device_id := coordinator.device_id) is not None:
        entities.extend(
            KasaTpapOutletSwitch(coordinator, parent_device_id, child.device_id, i)
            for i, child in enumerate(
                sorted(coordinator.data.children, key=lambda c: c.position or 0), 1
            )
        )
    async_add_entities(entities)


class KasaTpapSwitch(KasaTpapEntity, SwitchEntity):
    """The relay of a Kasa plug or wall switch.

    Identified by the device MAC, so it survives IP address changes.
    """

    _attr_name = None  # the device name is the entity name

    def __init__(self, coordinator: KasaTpapCoordinator) -> None:
        """Create the entity."""
        super().__init__(coordinator, None)

    @property
    def available(self) -> bool:
        """Unavailable if the last poll failed or no state was reported."""
        return super().available and self.coordinator.data.info.is_on is not None

    @property
    def is_on(self) -> bool | None:
        """Return the relay state from the last poll or command."""
        return self.coordinator.data.info.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the relay on."""
        client = self.coordinator.client
        await self.coordinator.async_command(lambda: client.async_set_device_on(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the relay off."""
        client = self.coordinator.client
        await self.coordinator.async_command(lambda: client.async_set_device_on(False))


class KasaTpapOutletSwitch(CoordinatorEntity[KasaTpapCoordinator], SwitchEntity):
    """One outlet of a power strip, registered as a sub-device of the strip."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: KasaTpapCoordinator,
        parent_device_id: str,
        child_id: str,
        index: int,
    ) -> None:
        """Create the entity."""
        super().__init__(coordinator)
        self._child_id = child_id
        parent = device_identifier(coordinator)
        child = coordinator.data.child(child_id)
        self._attr_unique_id = f"{parent}_{child_id}"
        self._attr_device_info = ChildDeviceInfo(
            identifiers={(DOMAIN, f"{parent}_{child_id}")},
            name=(child.nickname if child else None) or f"Outlet {index}",
            parent_device_id=parent_device_id,
        )

    @property
    def available(self) -> bool:
        """Unavailable if the strip no longer reports this outlet."""
        return (
            super().available
            and self.coordinator.data.child(self._child_id) is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Return the outlet state."""
        child = self.coordinator.data.child(self._child_id)
        return child.is_on if child else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the outlet on."""
        client, child_id = self.coordinator.client, self._child_id
        await self.coordinator.async_command(
            lambda: client.async_set_child_on(child_id, True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the outlet off."""
        client, child_id = self.coordinator.client, self._child_id
        await self.coordinator.async_command(
            lambda: client.async_set_child_on(child_id, False)
        )
