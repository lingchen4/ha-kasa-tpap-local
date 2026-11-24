"""Power and energy sensors for energy-monitoring plugs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KasaTpapConfigEntry
from .client import EnergyState
from .coordinator import KasaTpapCoordinator
from .entity import KasaTpapEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class KasaTpapSensorDescription(SensorEntityDescription):
    """Describes one energy reading."""

    value: Callable[[EnergyState], float | None]


SENSORS: tuple[KasaTpapSensorDescription, ...] = (
    KasaTpapSensorDescription(
        key="current_power",
        translation_key="current_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
        value=lambda e: e.power_w,
    ),
    KasaTpapSensorDescription(
        key="today_energy",
        translation_key="today_energy",
        device_class=SensorDeviceClass.ENERGY,
        # Resets at midnight; TOTAL_INCREASING handles the reset.
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
        value=lambda e: e.today_kwh,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KasaTpapConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for devices that measure energy."""
    coordinator = entry.runtime_data
    if coordinator.has_energy and coordinator.data.info.is_on is not None:
        async_add_entities(KasaTpapSensor(coordinator, d) for d in SENSORS)


class KasaTpapSensor(KasaTpapEntity, SensorEntity):
    """One energy reading."""

    entity_description: KasaTpapSensorDescription

    def __init__(
        self, coordinator: KasaTpapCoordinator, description: KasaTpapSensorDescription
    ) -> None:
        """Create the entity."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the reading from the last poll."""
        energy = self.coordinator.data.energy
        return None if energy is None else self.entity_description.value(energy)
