"""Sensors: power and energy (energy-monitoring plugs), and diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

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
    """Add energy sensors where measured, and diagnostic sensors."""
    coordinator = entry.runtime_data
    info = coordinator.data.info
    entities: list[SensorEntity] = []
    if coordinator.has_energy and info.is_on is not None:
        entities.extend(KasaTpapSensor(coordinator, d) for d in SENSORS)
    if info.rssi is not None:
        entities.append(KasaTpapSignalSensor(coordinator))
    if info.is_on is not None and info.on_time is not None:
        entities.append(KasaTpapOnSinceSensor(coordinator))
    async_add_entities(entities)


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


class KasaTpapSignalSensor(KasaTpapEntity, SensorEntity):
    """Wi-Fi signal strength reported by the device (from get_device_info)."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "rssi"

    def __init__(self, coordinator: KasaTpapCoordinator) -> None:
        """Create the entity."""
        super().__init__(coordinator, "rssi")

    @property
    def native_value(self) -> int | None:
        """Return the RSSI in dBm."""
        return self.coordinator.data.info.rssi


# Changes smaller than this are device-side jitter, as in python-kasa.
ON_SINCE_JITTER = timedelta(seconds=5)


class KasaTpapOnSinceSensor(KasaTpapEntity, SensorEntity):
    """When the relay was last turned on; unknown while it is off."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "on_since"

    def __init__(self, coordinator: KasaTpapCoordinator) -> None:
        """Create the entity."""
        super().__init__(coordinator, "on_since")
        self._on_since: datetime | None = None
        self._update_value()

    def _update_value(self) -> None:
        info = self.coordinator.data.info
        if not info.is_on or info.on_time is None:
            self._on_since = None
            return
        on_since = dt_util.utcnow() - timedelta(seconds=info.on_time)
        if self._on_since is None or abs(on_since - self._on_since) > ON_SINCE_JITTER:
            self._on_since = on_since.replace(microsecond=0)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_value()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> datetime | None:
        """Return the time the relay was turned on."""
        return self._on_since
