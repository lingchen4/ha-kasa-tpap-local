"""Diagnostics for Kasa TPAP Local, with every identifying value redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import KasaTpapConfigEntry

TO_REDACT = {
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_USERNAME,
    "unique_id",
    "title",
    "device_id",
    "mac",
    "nickname",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KasaTpapConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "device": async_redact_data(
            asdict(coordinator.data) if coordinator.data else {}, TO_REDACT
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None,
            "last_exception": type(coordinator.last_exception).__name__
            if coordinator.last_exception
            else None,
        },
        "components": sorted(coordinator.components),
        "client": {"has_session": coordinator.client.has_session},
    }
