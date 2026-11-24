"""Constants for Kasa TPAP Local."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "kasa_tpap"
MANUFACTURER: Final = "TP-Link"

CONF_SCAN_INTERVAL: Final = "scan_interval"
DEFAULT_SCAN_INTERVAL: Final = 30
MIN_SCAN_INTERVAL: Final = 3
MAX_SCAN_INTERVAL: Final = 300

# Upper bound of the polling back-off while the device is unreachable.
MAX_BACKOFF_INTERVAL: Final = timedelta(minutes=5)

# Settings that rarely change (LED, auto-off) are re-read at most this often,
# and right after they are changed from Home Assistant.
SETTINGS_REFRESH_INTERVAL: Final = timedelta(seconds=60)
