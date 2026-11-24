"""Polling coordinator for Kasa TPAP Local."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    AutoOffState,
    ChildState,
    DeviceInfo,
    EnergyState,
    LedState,
    TpapClient,
)
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_BACKOFF_INTERVAL,
    SETTINGS_REFRESH_INTERVAL,
)
from .exceptions import (
    TpapAuthError,
    TpapConfirmationError,
    TpapDeviceError,
    TpapDeviceLockedError,
    TpapError,
)

if TYPE_CHECKING:
    from . import KasaTpapConfigEntry

_LOGGER = logging.getLogger(__name__)

COMPONENT_BRIGHTNESS = "brightness"
COMPONENT_ENERGY = "energy_monitoring"
COMPONENT_CHILDREN = "control_child"
COMPONENT_LED = "led"
COMPONENT_AUTO_OFF = "auto_off"


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Everything one poll reads from the device."""

    info: DeviceInfo
    energy: EnergyState | None = None
    children: tuple[ChildState, ...] = ()
    led: LedState | None = None
    auto_off: AutoOffState | None = None

    def child(self, child_id: str) -> ChildState | None:
        """Return the outlet with this id, if the device still reports it."""
        return next((c for c in self.children if c.device_id == child_id), None)


class KasaTpapCoordinator(DataUpdateCoordinator[DeviceSnapshot]):
    """Poll one device; back off exponentially while it is unreachable.

    DataUpdateCoordinator runs one refresh at a time, so polls never overlap,
    and with ``always_update=False`` entities are only written on a change.
    The device's components are read once at setup and decide which
    entities exist: a switch (plugs, switches), a light (dimmers), power and
    energy sensors (energy-monitoring plugs), one switch per outlet (strips).
    """

    config_entry: KasaTpapConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: KasaTpapConfigEntry, client: TpapClient
    ) -> None:
        """Create the coordinator."""
        self.client = client
        self.components: frozenset[str] = frozenset()
        # Device-registry id of the device, set at setup; outlets refer to it.
        self.device_id: str | None = None
        # Monotonic time the slow-changing settings were last read.
        self._settings_read_at: float | None = None
        self._base_interval = timedelta(
            seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.host}",
            update_interval=self._base_interval,
            always_update=False,
        )

    @property
    def has_brightness(self) -> bool:
        """Whether the device is a dimmer."""
        return COMPONENT_BRIGHTNESS in self.components

    @property
    def has_energy(self) -> bool:
        """Whether the device measures power and energy."""
        return COMPONENT_ENERGY in self.components

    @property
    def has_led(self) -> bool:
        """Whether the device has a controllable status LED."""
        return COMPONENT_LED in self.components

    @property
    def has_auto_off(self) -> bool:
        """Whether the device has an auto-off timer for its relay."""
        return COMPONENT_AUTO_OFF in self.components

    @property
    def has_children(self) -> bool:
        """Whether the device is a power strip with switchable outlets."""
        return COMPONENT_CHILDREN in self.components

    async def _async_setup(self) -> None:
        try:
            self.components = await self.client.async_get_components()
        except TpapError as err:
            raise self._translate(err) from err

    async def _fetch(self) -> DeviceSnapshot:
        info = await self.client.async_get_device_info()
        energy: EnergyState | None = None
        # Like python-kasa, energy is only read from a unit with its own
        # relay (not from the base of a strip such as the P304M), and a
        # refused energy request does not fail the poll.
        if self.has_energy and info.is_on is not None:
            try:
                energy = await self.client.async_get_energy()
            except TpapDeviceError as err:
                _LOGGER.debug("Energy reading refused: %s", err)
        children = await self.client.async_get_children() if self.has_children else ()
        led, auto_off = await self._fetch_settings(info)
        return DeviceSnapshot(
            info=info, energy=energy, children=children, led=led, auto_off=auto_off
        )

    async def _fetch_settings(
        self, info: DeviceInfo
    ) -> tuple[LedState | None, AutoOffState | None]:
        """Read LED and auto-off, at most once per SETTINGS_REFRESH_INTERVAL.

        Between reads the previous values are reused, so short polling
        intervals do not multiply the requests the device has to answer. A
        refused request leaves that setting unknown without failing the poll.
        """
        previous = self.data
        now = time.monotonic()
        if (
            previous is not None
            and self._settings_read_at is not None
            and now - self._settings_read_at < SETTINGS_REFRESH_INTERVAL.total_seconds()
        ):
            return previous.led, previous.auto_off
        led: LedState | None = None
        auto_off: AutoOffState | None = None
        if self.has_led:
            try:
                led = await self.client.async_get_led()
            except TpapDeviceError as err:
                _LOGGER.debug("LED setting refused: %s", err)
        if self.has_auto_off and info.is_on is not None:
            try:
                auto_off = await self.client.async_get_auto_off()
            except TpapDeviceError as err:
                _LOGGER.debug("Auto-off setting refused: %s", err)
        self._settings_read_at = now
        return led, auto_off

    def _translate(self, err: TpapError) -> Exception:
        if isinstance(err, TpapDeviceLockedError):
            # Not a reason to ask for new credentials; wait the lockout out.
            minutes = err.locked_minutes or 30
            self.update_interval = max(self._base_interval, timedelta(minutes=minutes))
            return UpdateFailed(str(err))
        if isinstance(err, TpapConfirmationError):
            # The client refuses further logins; polling cannot recover it.
            return UpdateFailed(f"{err}. Reconfigure the integration.")
        if isinstance(err, TpapAuthError):
            # Stops polling and starts reauthentication, so a wrong password
            # is not retried against the device's lockout counter.
            return ConfigEntryAuthFailed(str(err))
        current = self.update_interval or self._base_interval
        self.update_interval = min(
            current * 2, max(MAX_BACKOFF_INTERVAL, self._base_interval)
        )
        return UpdateFailed(str(err))

    async def _async_update_data(self) -> DeviceSnapshot:
        try:
            snapshot = await self._fetch()
        except TpapError as err:
            raise self._translate(err) from err
        self.update_interval = self._base_interval
        return snapshot

    async def async_command(
        self,
        command: Callable[[], Awaitable[None]],
        *,
        settings_changed: bool = False,
        read_back: bool = True,
    ) -> None:
        """Run a command, then publish the state the device reports afterwards.

        ``settings_changed`` makes the read-back include LED and auto-off;
        ``read_back=False`` skips it (a rebooting device cannot answer).
        async_set_updated_data also reschedules the next poll.
        """
        try:
            await command()
        except TpapDeviceLockedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="device_locked"
            ) from err
        except TpapAuthError as err:
            # Raising ConfigEntryAuthFailed from a service call does not start
            # reauthentication, so start it here.
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except TpapError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if settings_changed:
            self._settings_read_at = None
        if not read_back:
            return
        try:
            snapshot = await self._fetch()
        except TpapError:
            # The command itself succeeded; let the next poll show its effect.
            await self.async_request_refresh()
            return
        self.update_interval = self._base_interval
        self.async_set_updated_data(snapshot)
