"""Config flow for Kasa TPAP Local."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import probatio as vol  # Home Assistant's voluptuous-compatible validator
from homeassistant.components import network
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from . import create_http_session
from .client import DeviceInfo, TpapClient, normalize_mac
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .discovery import (
    SCAN_CONCURRENCY,
    DiscoveredDevice,
    async_probe,
    async_scan,
    scan_targets,
)
from .exceptions import (
    InvalidHostError,
    TpapAuthError,
    TpapConfirmationError,
    TpapConnectionError,
    TpapDeviceLockedError,
    TpapDiscoveryError,
    TpapError,
    TpapTimeoutError,
    TpapUnsupportedError,
)
from .network import validate_device_host

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"
# A configured device's address is changed from DHCP at most this often.
DHCP_UPDATE_INTERVAL = 3600.0
_DHCP_ATTEMPTS: dict[str, float] = {}

_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)
_EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL,
        max=MAX_SCAN_INTERVAL,
        step=1,
        mode=NumberSelectorMode.BOX,
        unit_of_measurement="s",
    )
)


async def async_validate_device(host: str, username: str, password: str) -> DeviceInfo:
    """Log in for real, check the request protocol, read the device information.

    Raises TpapError subclasses; the HTTP session is always closed.
    """
    http_session = create_http_session()
    try:
        client = TpapClient(host, username, password, http_session)
        try:
            await client.async_get_components()
            return await client.async_get_device_info()
        finally:
            await client.async_close()
    finally:
        await http_session.close()


async def _async_try_validate(
    host: str, username: str, password: str, errors: dict[str, str]
) -> DeviceInfo | None:
    """Validate, mapping failures onto translated form errors."""
    try:
        return await async_validate_device(host, username, password)
    except InvalidHostError:
        errors[CONF_HOST] = "invalid_host"
    except TpapDeviceLockedError:
        errors["base"] = "device_locked"
    except TpapAuthError:
        errors["base"] = "invalid_auth"
    except TpapTimeoutError:
        errors["base"] = "timeout"
    except TpapConnectionError:
        errors["base"] = "cannot_connect"
    except TpapDiscoveryError:
        errors["base"] = "tpap_discovery_failed"
    except TpapUnsupportedError:
        errors["base"] = "unsupported_protocol"
    except TpapConfirmationError:
        errors["base"] = "confirmation_failed"
    except TpapError as err:
        _LOGGER.warning("TPAP error while validating %s: %s", host, err)
        errors["base"] = "protocol_error"
    except Exception:
        # A traceback holds no local variables, and this integration never
        # puts credentials into exception messages.
        _LOGGER.exception("Unexpected error while validating %s", host)
        errors["base"] = "unknown"
    return None


class KasaTpapConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a device found by scan or DHCP, or entered by IP address."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovered: dict[str, DiscoveredDevice] = {}
        self._host: str | None = None
        self._scan_task: asyncio.Task[list[DiscoveredDevice]] | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KasaTpapOptionsFlow:
        """Return the options flow."""
        return KasaTpapOptionsFlow()

    # ------------------------------------------------------------ helpers

    def _saved_accounts(self) -> dict[str, str]:
        """TP-Link accounts stored by this integration's entries.

        Entries whose credentials are known to be wrong (reauthentication
        pending, setup error) are skipped; a working (loaded) entry wins over
        others for the same email.
        """
        accounts: dict[str, str] = {}
        entries = sorted(
            self._async_current_entries(include_ignore=False),
            key=lambda e: e.state is ConfigEntryState.LOADED,
        )
        for entry in entries:
            if entry.state is ConfigEntryState.SETUP_ERROR or any(
                entry.async_get_active_flows(self.hass, {SOURCE_REAUTH})
            ):
                continue
            username = entry.data.get(CONF_USERNAME)
            password = entry.data.get(CONF_PASSWORD)
            if username and password:
                accounts[username] = password
        return accounts

    def _account_schema(self, suggested: Mapping[str, Any]) -> dict[Any, Any]:
        """Email (pre-filled from a saved account) and password fields."""
        saved = self._saved_accounts()
        default_user = suggested.get(CONF_USERNAME) or next(reversed(saved), "")
        return {
            vol.Required(CONF_USERNAME, default=default_user): _EMAIL_SELECTOR,
            # Optional when an account is saved: empty reuses its password.
            (vol.Optional if saved else vol.Required)(
                CONF_PASSWORD
            ): _PASSWORD_SELECTOR,
        }

    @staticmethod
    def _extra_schema(suggested: Mapping[str, Any]) -> dict[Any, Any]:
        return {
            vol.Optional(CONF_NAME, default=suggested.get(CONF_NAME, "")): str,
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=suggested.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): _INTERVAL_SELECTOR,
        }

    def _resolve_password(
        self, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> str | None:
        password = user_input.get(CONF_PASSWORD) or self._saved_accounts().get(
            user_input[CONF_USERNAME]
        )
        if not password:
            errors[CONF_PASSWORD] = "password_required"
        return password

    async def _async_create(
        self, host: str, user_input: Mapping[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Validate with a real login and create the entry, or fill errors."""
        password = self._resolve_password(user_input, errors)
        if password is None:
            return None
        username = user_input[CONF_USERNAME]
        info = await _async_try_validate(host, username, password, errors)
        if info is None:
            return None
        await self.async_set_unique_id(info.mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        name = (user_input.get(CONF_NAME) or "").strip()
        data = {CONF_HOST: host, CONF_USERNAME: username, CONF_PASSWORD: password}
        if name:
            data[CONF_NAME] = name
        return self.async_create_entry(
            title=name or info.nickname or info.model,
            data=data,
            options={
                CONF_SCAN_INTERVAL: int(
                    user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                )
            },
        )

    # --------------------------------------------------------------- user

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between scanning the network and entering an address."""
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual"])

    async def _async_scan_network(self) -> list[DiscoveredDevice]:
        """Probe Home Assistant's LAN; never raises."""
        try:
            hosts = scan_targets(await network.async_get_adapters(self.hass))
            session = create_http_session(limit=SCAN_CONCURRENCY)
            try:
                return await async_scan(session, hosts)
            finally:
                await session.close()
        except Exception:
            _LOGGER.exception("Network scan failed")
            return []

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan Home Assistant's LAN for TPAP devices, showing progress."""
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(
                self._async_scan_network(), eager_start=False
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan", progress_action="scan", progress_task=self._scan_task
            )
        configured = {
            e.unique_id for e in self._async_current_entries(include_ignore=False)
        }
        self._discovered = {
            d.host: d for d in self._scan_task.result() if d.mac not in configured
        }
        self._scan_task = None
        if not self._discovered:
            return self.async_show_progress_done(next_step_id="scan_none")
        return self.async_show_progress_done(next_step_id="pick")

    async def async_step_scan_none(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Nothing was found."""
        return self.async_abort(reason="no_devices_found")

    async def async_step_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a scanned device and enter its account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device = self._discovered.get(user_input[CONF_DEVICE])
            if device is None:
                errors[CONF_DEVICE] = "invalid_host"
            elif result := await self._async_create(device.host, user_input, errors):
                return result

        suggested = user_input or {}
        options = [
            SelectOptionDict(value=d.host, label=f"{d.host} ({d.mac})")
            for d in self._discovered.values()
        ]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEVICE,
                        default=suggested.get(CONF_DEVICE, options[0]["value"]),
                    ): SelectSelector(SelectSelectorConfig(options=options)),
                    **self._account_schema(suggested),
                    **self._extra_schema(suggested),
                }
            ),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter the address and the account."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                host = validate_device_host(user_input[CONF_HOST])
            except InvalidHostError:
                errors[CONF_HOST] = "invalid_host"
            else:
                self._async_abort_entries_match({CONF_HOST: host})
                if result := await self._async_create(host, user_input, errors):
                    return result

        suggested = user_input or {}
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=suggested.get(CONF_HOST, "")): str,
                    **self._account_schema(suggested),
                    **self._extra_schema(suggested),
                }
            ),
            errors=errors,
        )

    # --------------------------------------------------------------- DHCP

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """A TP-Link MAC appeared on the network (or changed address).

        New devices are probed with the TPAP discover (no credentials) and
        offered only if they answer as TPAP devices; KLAP/AES devices of the
        official integration are ignored.

        DHCP and the discover answer are unauthenticated, so a configured
        device's address is changed only if the device is unreachable at its
        stored address, at most once an hour per device, and only after a
        full login (with PAKE confirmation) at the new address succeeds.
        """
        mac = normalize_mac(discovery_info.macaddress)
        try:
            host = validate_device_host(discovery_info.ip)
        except InvalidHostError:
            return self.async_abort(reason="not_tpap_device")
        if mac is None:
            return self.async_abort(reason="not_tpap_device")
        await self.async_set_unique_id(mac)

        entry = next(
            (
                e
                for e in self._async_current_entries(include_ignore=True)
                if e.unique_id == mac
            ),
            None,
        )
        if entry is not None:
            return await self._async_dhcp_moved(entry, host)

        session = create_http_session()
        try:
            device = await async_probe(session, host)
        finally:
            await session.close()
        if device is None or device.mac != mac:
            return self.async_abort(reason="not_tpap_device")

        self._abort_if_unique_id_configured()
        self._host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_discovery_confirm()

    async def _async_dhcp_moved(
        self, entry: ConfigEntry, host: str
    ) -> ConfigFlowResult:
        """Follow a configured device to a new address, if it is verified."""
        if entry.source == SOURCE_IGNORE or entry.data.get(CONF_HOST) == host:
            return self.async_abort(reason="already_configured")
        coordinator = getattr(entry, "runtime_data", None)
        if (
            entry.state is ConfigEntryState.LOADED
            and coordinator is not None
            and coordinator.last_update_success
        ):
            # Working at its stored address: a different address is suspect.
            return self.async_abort(reason="already_configured")
        now = time.monotonic()
        if now - _DHCP_ATTEMPTS.get(entry.entry_id, -DHCP_UPDATE_INTERVAL) < (
            DHCP_UPDATE_INTERVAL
        ):
            return self.async_abort(reason="already_configured")
        _DHCP_ATTEMPTS[entry.entry_id] = now
        try:
            info = await async_validate_device(
                host, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
            )
        except TpapError as err:
            _LOGGER.debug("Not moving %s to %s: %s", entry.title, host, err)
            return self.async_abort(reason="already_configured")
        if info.mac != entry.unique_id:
            return self.async_abort(reason="already_configured")
        _LOGGER.info("Device %s moved to %s; address updated", entry.title, host)
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST: host}
        )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="already_configured")

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account of a discovered device."""
        if self._host is None:
            return self.async_abort(reason="not_tpap_device")
        errors: dict[str, str] = {}
        if user_input is not None and (
            result := await self._async_create(self._host, user_input, errors)
        ):
            return result
        suggested = user_input or {}
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema(
                {**self._account_schema(suggested), **self._extra_schema(suggested)}
            ),
            errors=errors,
            description_placeholders={"host": self._host},
        )

    # --------------------------------------------------- reauth/reconfigure

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the device rejected the account."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the account again.

        Other devices stored with the same email and the same old password
        are updated too, so a changed TP-Link password is entered only once:
        each of them is first logged in with the new password, and only those
        that accept it are changed.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            info = await _async_try_validate(
                entry.data[CONF_HOST], username, password, errors
            )
            if info is not None:
                await self.async_set_unique_id(info.mac)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                if username == entry.data[CONF_USERNAME]:
                    await self._async_share_password(entry, password)
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_USERNAME: username, CONF_PASSWORD: password},
                )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=entry.data[CONF_USERNAME]
                ): _EMAIL_SELECTOR,
                vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    async def _async_share_password(self, entry: ConfigEntry, password: str) -> None:
        """Give the new password to sibling entries that verify it."""
        old = (entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
        siblings = [
            other
            for other in self._async_current_entries(include_ignore=False)
            if other.entry_id != entry.entry_id
            and (other.data.get(CONF_USERNAME), other.data.get(CONF_PASSWORD)) == old
        ]

        async def _verify(other: ConfigEntry) -> bool:
            try:
                info = await async_validate_device(
                    other.data[CONF_HOST], old[0], password
                )
            except TpapError:
                return False
            return info.mac == other.unique_id

        results = await asyncio.gather(*(_verify(o) for o in siblings))
        for other, verified in zip(siblings, results, strict=True):
            if verified:
                self.hass.config_entries.async_update_entry(
                    other, data={**other.data, CONF_PASSWORD: password}
                )
                self.hass.config_entries.async_schedule_reload(other.entry_id)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or the account of an existing device.

        Leave the password empty to keep the stored one.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                host = validate_device_host(user_input[CONF_HOST])
            except InvalidHostError:
                errors[CONF_HOST] = "invalid_host"
            else:
                password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
                info = await _async_try_validate(
                    host, user_input[CONF_USERNAME], password, errors
                )
                if info is not None:
                    await self.async_set_unique_id(info.mac)
                    self._abort_if_unique_id_mismatch(reason="wrong_device")
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: host,
                            CONF_USERNAME: user_input[CONF_USERNAME],
                            CONF_PASSWORD: password,
                        },
                    )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(
                    CONF_USERNAME, default=entry.data[CONF_USERNAME]
                ): _EMAIL_SELECTOR,
                vol.Optional(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )


class KasaTpapOptionsFlow(OptionsFlowWithReload):
    """Change the polling interval; the entry reloads to apply it."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_SCAN_INTERVAL, default=current): _INTERVAL_SELECTOR}
            ),
        )
