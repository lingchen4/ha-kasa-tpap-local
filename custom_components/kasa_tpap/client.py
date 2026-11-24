"""Local TPAP client for Kasa devices.

The login and session handling are derived from ``TpapTransport`` in
python-kasa's ``kasa/transports/tpaptransport.py`` at ZeliardM/python-kasa
commit f4dff399016a9daa47316ffb209f6b74a989b6dc (GPL-3.0-or-later), reduced
to the path a Kasa switch uses:

1. In-band ``discover`` over HTTP on the configured address (no UDP).
2. ``pake_register`` and ``pake_share`` (SPAKE2+ with the TP-Link account
   password, passcode type ``userpw``), checking the device's confirmation.
3. Requests encrypted with AES-128-CCM to ``/stok=<id>/ds``.

TLS modes, device attestation (DAC), camera and robot logins and the
MAC-derived default passcode are not implemented and are refused explicitly.
See ``vendor/tpap/UPSTREAM.md`` for every difference from upstream.

Nothing secret is logged: not the credentials, the session id, the keys, nor
request or response payloads.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import functools
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
from cryptography.exceptions import InvalidTag
from yarl import URL

from .exceptions import (
    TpapAuthError,
    TpapConfirmationError,
    TpapConnectionError,
    TpapDeviceError,
    TpapDeviceLockedError,
    TpapDiscoveryError,
    TpapError,
    TpapProtocolError,
    TpapTimeoutError,
    TpapUnsupportedError,
)
from .network import validate_device_host
from .vendor.tpap import crypto
from .vendor.tpap.session import (
    CIPHER_AES_128_CCM,
    SessionExhaustedError,
    TpapEncryptionSession,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 80
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_RETRY_BACKOFF = 0.5
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 256 * 1024
# Upstream's bound: the count comes from an unauthenticated reply and drives
# PBKDF2, so an unbounded value would be a pre-authentication DoS.
MAX_PAKE_ITERATIONS = 100_000
DEFAULT_PAKE_ITERATIONS = 10_000
# Minutes to refuse logins when the device reports a lockout without a time,
# and the most a (possibly spoofed, unauthenticated) lockout reply can impose.
DEFAULT_LOCKOUT_MINUTES = 30
MAX_LOCKOUT_MINUTES = 24 * 60
# Bounds for power strips' child lists.
MAX_CHILD_PAGES = 10
MAX_CHILDREN = 64

# The only SPAKE2+ suite and session cipher offered; the device must pick them.
OFFERED_CIPHER_SUITE = 1
PASSCODE_TYPE_USERPW = "userpw"
PAKE_USERPW = 2

# Device error codes, from python-kasa's SmartErrorCode.
ERROR_SUCCESS = 0
ERROR_DEVICE_BLOCKED = -40404
# Codes on a /ds request meaning the device dropped the session (upstream's
# SESSION_LOST_ERROR_CODES).
SESSION_LOST_ERROR_CODES = frozenset({-2402, -2101, -40420, -40421, -40401})
# SMART_AUTHENTICATION_ERRORS plus STAT_ACCESS_ERROR (-2203), the TPAP answer
# to a wrong password.
AUTH_ERROR_CODES = frozenset({-1501, 1111, -1005, 1100, 1003, -40412, -2203})
# SMART_RETRYABLE_ERRORS.
RETRYABLE_ERROR_CODES = frozenset({1002, 1112, -1001, 9999, -40401, -40413})

# Printable ASCII without characters that would change the URL's meaning.
_STOK_RE = re.compile(r"^[!-~]{1,256}$")
_STOK_FORBIDDEN = frozenset("/?#%\\")
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
_REGISTER_USERNAME = crypto.md5_hex("admin")


class _SessionLostError(TpapError):
    """Internal: the device dropped the session; log in again and resend."""


def _as_int(value: Any) -> int | None:
    """Return an int from a JSON number or numeric string, never from a bool."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def normalize_mac(value: Any) -> str | None:
    """Return ``aa:bb:cc:dd:ee:ff`` for a MAC in any common notation."""
    if not isinstance(value, str):
        return None
    compact = value.replace(":", "").replace("-", "").replace(".", "")
    if not _MAC_RE.match(compact):
        return None
    compact = compact.lower()
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2))


def _parse_json_object(body: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(body.decode())
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _error_code(reply: dict[str, Any]) -> int | None:
    return _as_int(reply.get("error_code", reply.get("err_code")))


def _b64decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise TpapProtocolError(f"TPAP register response missing {field}")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as err:
        raise TpapProtocolError(f"TPAP register response has invalid {field}") from err


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """The device fields the integration uses, from ``get_device_info``."""

    device_id: str
    mac: str
    model: str
    hardware_version: str | None
    firmware_version: str | None
    nickname: str | None
    is_on: bool | None  # None on power strips, whose outlets are children
    rssi: int | None
    device_type: str | None = None
    brightness: int | None = None  # 0-100 on dimmers

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> DeviceInfo:
        """Parse and validate a ``get_device_info`` result."""
        mac = normalize_mac(result.get("mac"))
        device_on = result.get("device_on")
        model = result.get("model")
        device_id = result.get("device_id")
        if (
            mac is None
            or not isinstance(model, str)
            or (device_on is not None and not isinstance(device_on, bool))
        ):
            raise TpapProtocolError("get_device_info returned an unexpected shape")

        def text(key: str) -> str | None:
            value = result.get(key)
            return value if isinstance(value, str) and value else None

        brightness = _as_int(result.get("brightness"))
        return cls(
            device_id=device_id if isinstance(device_id, str) else "",
            mac=mac,
            model=model,
            hardware_version=text("hw_ver"),
            firmware_version=text("fw_ver"),
            nickname=_decode_name(text("nickname")),
            is_on=device_on,
            rssi=_as_int(result.get("rssi")),
            device_type=text("type"),
            brightness=brightness
            if brightness is not None and 0 <= brightness <= 100
            else None,
        )


@dataclass(frozen=True, slots=True)
class EnergyState:
    """Readings of an energy-monitoring plug."""

    power_w: float | None
    today_kwh: float | None
    month_kwh: float | None


@dataclass(frozen=True, slots=True)
class ChildState:
    """One outlet of a power strip."""

    device_id: str
    nickname: str | None
    is_on: bool
    position: int | None

    @classmethod
    def from_result(cls, result: Any) -> ChildState | None:
        """Parse one ``child_device_list`` entry, or None if malformed."""
        if not isinstance(result, dict):
            return None
        device_id = result.get("device_id")
        device_on = result.get("device_on")
        if (
            not isinstance(device_id, str)
            or not 0 < len(device_id) <= 128
            or not isinstance(device_on, bool)
        ):
            return None
        nickname = result.get("nickname")
        return cls(
            device_id=device_id,
            nickname=_decode_name(nickname if isinstance(nickname, str) else None),
            is_on=device_on,
            position=_as_int(result.get("position")),
        )


def _decode_name(value: str | None) -> str | None:
    """Device names are base64; some firmware sends clear text."""
    if not value:
        return None
    with contextlib.suppress(binascii.Error, ValueError):
        return base64.b64decode(value, validate=True).decode()
    return value


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class _LoginParameters:
    """What the in-band discover announced."""

    port: int
    mac_hex: str  # 12 hex digits, or "" if the device sent none


@dataclass(frozen=True, slots=True)
class _ShareValues:
    params: dict[str, str]
    expected_dev_confirm: bytes
    shared_key: bytes


def _compute_share(
    *,
    username: str,
    password: str,
    mac_hex: str,
    extra_crypt: dict[str, Any] | None,
    salt: bytes,
    iterations: int,
    user_random: bytes,
    dev_random: bytes,
    dev_share: bytes,
) -> _ShareValues:
    """Run the CPU-heavy SPAKE2+ step; called in an executor thread."""
    try:
        credentials = crypto.build_credentials(extra_crypt, username, password, mac_hex)
        user_share, user_confirm, dev_confirm, shared_key = crypto.spake2p_share(
            credentials.encode(),
            suite_type=OFFERED_CIPHER_SUITE,
            salt=salt,
            iterations=iterations,
            user_random=user_random,
            dev_random=dev_random,
            dev_share=dev_share,
        )
    except TpapProtocolError:
        raise
    # Malformed device data (bad point, odd extra_crypt) must surface as a
    # protocol error, never as an unexpected exception.
    except Exception as err:  # noqa: BLE001
        raise TpapProtocolError(
            f"Invalid TPAP register response ({type(err).__name__})"
        ) from None
    return _ShareValues(
        params={
            "sub_method": "pake_share",
            "user_share": base64.b64encode(user_share).decode(),
            "user_confirm": base64.b64encode(user_confirm).decode(),
        },
        expected_dev_confirm=base64.b64encode(dev_confirm),
        shared_key=shared_key,
    )


class TpapClient:
    """One device, one session, one request at a time."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        *,
        port: int = DEFAULT_PORT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        """Create the client; nothing is sent until the first request."""
        self._host = validate_device_host(host)
        self._username = username
        self._password = password
        self._http = session
        self._port = port
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._retry_backoff = retry_backoff
        self._lock = asyncio.Lock()
        self._login_params: _LoginParameters | None = None
        self._session: TpapEncryptionSession | None = None
        self._ds_url: URL | None = None
        self._locked_until: float | None = None
        # Set once the device rejects the credentials. No further login is
        # attempted with them: each one would count toward the lockout.
        self._auth_failed = False
        # Set once the device fails the PAKE confirmation: whatever answers
        # may not be the device, so no further login is sent to it.
        self._confirmation_failed = False
        self._terminal_uuid = base64.b64encode(secrets.token_bytes(16)).decode()

    def __repr__(self) -> str:
        """Identify the client without credentials."""
        return f"<TpapClient host={self._host}>"

    @property
    def host(self) -> str:
        """The validated device address."""
        return self._host

    @property
    def has_session(self) -> bool:
        """Whether an authenticated session is currently held."""
        return self._session is not None

    async def async_get_device_info(self) -> DeviceInfo:
        """Return the parsed device information."""
        return DeviceInfo.from_result(await self.async_request("get_device_info"))

    async def async_set_device_on(self, on: bool) -> None:
        """Switch the relay (or light) on or off."""
        await self.async_request("set_device_info", {"device_on": on})

    async def async_set_brightness(self, brightness: int) -> None:
        """Turn a dimmer on at a brightness of 1-100 %."""
        if not 1 <= brightness <= 100:
            raise ValueError("brightness must be 1-100")
        await self.async_request(
            "set_device_info", {"device_on": True, "brightness": brightness}
        )

    async def async_get_components(self) -> frozenset[str]:
        """Return the component ids the device announces (``component_nego``).

        Raises TpapUnsupportedError for devices that do not speak the SMART
        request protocol (for example legacy IOT-protocol Kasa devices).
        """
        try:
            result = await self.async_request("component_nego")
        except TpapDeviceError as err:
            raise TpapUnsupportedError(
                f"{self._host} does not support the SMART request protocol"
            ) from err
        components = result.get("component_list")
        if not isinstance(components, list):
            raise TpapUnsupportedError(
                f"{self._host} does not support the SMART request protocol"
            )
        return frozenset(
            c["id"]
            for c in components
            if isinstance(c, dict) and isinstance(c.get("id"), str)
        )

    async def async_get_energy(self) -> EnergyState:
        """Return power (W) and energy (kWh) of an energy-monitoring plug."""
        usage = await self.async_request("get_energy_usage")
        # get_energy_usage reports mW; get_current_power, a fallback for
        # firmware that leaves it out, reports W (as in python-kasa).
        power_mw = _number(usage.get("current_power"))
        power_w = power_mw / 1000 if power_mw is not None else None
        if power_w is None:
            # Optional on some firmware: a refusal leaves the power unknown.
            with contextlib.suppress(TpapDeviceError):
                power = await self.async_request("get_current_power")
                power_w = _number(power.get("current_power"))
        today_wh = _number(usage.get("today_energy"))
        month_wh = _number(usage.get("month_energy"))
        return EnergyState(
            power_w=power_w,
            today_kwh=today_wh / 1000 if today_wh is not None else None,
            month_kwh=month_wh / 1000 if month_wh is not None else None,
        )

    async def async_get_children(self) -> tuple[ChildState, ...]:
        """Return the outlets of a power strip (paginated list)."""
        children: dict[str, ChildState] = {}
        start = 0
        for _ in range(MAX_CHILD_PAGES):
            page = await self.async_request(
                "get_child_device_list", {"start_index": start}
            )
            entries = page.get("child_device_list")
            if not isinstance(entries, list) or not entries:
                break
            for entry in entries:
                if (child := ChildState.from_result(entry)) is not None:
                    children.setdefault(child.device_id, child)
            start += len(entries)
            total = _as_int(page.get("sum"))
            if total is None or start >= min(total, MAX_CHILDREN):
                break
        return tuple(list(children.values())[:MAX_CHILDREN])

    async def async_set_child_on(self, child_id: str, on: bool) -> None:
        """Switch one outlet of a power strip."""
        result = await self.async_request(
            "control_child",
            {
                "device_id": child_id,
                "requestData": {
                    "method": "set_device_info",
                    "params": {"device_on": on},
                },
            },
        )
        response = result.get("responseData")
        code = _error_code(response) if isinstance(response, dict) else None
        if code != ERROR_SUCCESS:
            raise TpapDeviceError(
                f"{self._host} refused to switch an outlet (error {code})",
                error_code=code if code is not None else -1,
            )

    async def async_discover(self) -> str | None:
        """Probe the address with the in-band TPAP discover (no credentials).

        Returns the device MAC (``aa:bb:...``) or None. Raises TpapError when
        the address does not answer as a supported TPAP device. Sent once,
        without retries.
        """
        async with self._lock:
            self._login_params = await self._discover()
        mac_hex = self._login_params.mac_hex
        return normalize_mac(mac_hex) if mac_hex else None

    async def async_close(self) -> None:
        """Forget the session; the HTTP session belongs to the caller."""
        async with self._lock:
            self._drop_session()
            self._login_params = None

    async def async_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one request, logging in first if needed.

        Requests are serialised: TPAP sequence numbers must not interleave.
        Connection errors are retried with exponential backoff, a dropped
        session is renewed once, and authentication errors are never retried
        because every refused login counts toward the device's lockout.
        """
        async with self._lock:
            if self._confirmation_failed:
                raise TpapConfirmationError(
                    f"{self._host} failed the PAKE confirmation earlier; "
                    "check the address and reconfigure the integration"
                )
            if self._auth_failed:
                raise TpapAuthError(
                    f"{self._host} rejected the stored credentials; "
                    "re-authenticate the integration"
                )
            renewed = False
            attempt = 0
            while True:
                try:
                    if self._session is None:
                        await self._login()
                    return await self._send(method, params)
                except _SessionLostError as err:
                    self._drop_session()
                    if renewed:
                        raise TpapProtocolError(
                            f"{self._host} dropped a freshly established session"
                        ) from err
                    renewed = True
                    _LOGGER.debug(
                        "Session with %s expired, logging in again", self._host
                    )
                except TpapDeviceLockedError:
                    raise
                except TpapConfirmationError:
                    self._confirmation_failed = True
                    raise
                except TpapAuthError:
                    self._auth_failed = True
                    raise
                except TpapConnectionError as err:
                    attempt += 1
                    if not err.retryable or attempt >= MAX_ATTEMPTS:
                        raise
                    delay = self._retry_backoff * 2 ** (attempt - 1)
                    _LOGGER.debug(
                        "Request to %s failed (attempt %d/%d), retrying in %.1fs",
                        self._host,
                        attempt,
                        MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)

    # ----------------------------------------------------------------- HTTP

    def _base_url(self, port: int) -> URL:
        return URL.build(scheme="http", host=self._host, port=port, path="/")

    async def _post(
        self,
        url: URL,
        *,
        json_body: dict[str, Any] | None = None,
        data: bytes | None = None,
    ) -> tuple[int, bytes]:
        """POST to the device; never follow redirects, cap the body size."""
        if url.host != self._host:  # defence in depth, URLs are built here
            raise TpapProtocolError("Refusing to contact a host other than the device")
        headers = {
            "Content-Type": "application/json"
            if json_body is not None
            else "application/octet-stream"
        }
        payload = (
            json.dumps(json_body, separators=(",", ":")).encode()
            if json_body is not None
            else data
        )
        try:
            async with self._http.post(
                url,
                data=payload,
                headers=headers,
                allow_redirects=False,
                timeout=self._timeout,
            ) as response:
                if 300 <= response.status < 400:
                    raise TpapProtocolError(
                        f"{self._host} answered with an HTTP redirect, which is refused"
                    )
                body = bytearray()
                while chunk := await response.content.read(64 * 1024):
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise TpapProtocolError(f"Response from {self._host} too large")
                return response.status, bytes(body)
        except TimeoutError:
            raise TpapTimeoutError(f"Timed out waiting for {self._host}") from None
        except aiohttp.ClientError as err:
            # The message is built here: aiohttp's can include the request URL,
            # which carries the session id.
            raise TpapConnectionError(
                f"Cannot communicate with {self._host} ({type(err).__name__})"
            ) from None

    # ---------------------------------------------------------------- login

    def _drop_session(self) -> None:
        self._session = None
        self._ds_url = None

    def _check_lockout(self) -> None:
        if self._locked_until is None:
            return
        remaining = self._locked_until - time.monotonic()
        if remaining <= 0:
            self._locked_until = None
            return
        raise TpapDeviceLockedError(
            f"{self._host} is locked after failed logins; not retrying for "
            f"{int(remaining // 60) + 1} more minutes",
            locked_minutes=int(remaining // 60) + 1,
        )

    def _raise_if_locked(self, reply: dict[str, Any], step: str) -> None:
        error_info = reply.get("error_info")
        if not isinstance(error_info, dict):
            return
        locked_minutes = _as_int(error_info.get("lockedMinute"))
        remain_attempts = _as_int(error_info.get("remainAttempts"))
        if (locked_minutes or 0) > 0 or remain_attempts == 0:
            minutes = min(
                locked_minutes
                if locked_minutes and locked_minutes > 0
                else DEFAULT_LOCKOUT_MINUTES,
                MAX_LOCKOUT_MINUTES,
            )
            self._locked_until = time.monotonic() + minutes * 60
            self._drop_session()
            raise TpapDeviceLockedError(
                f"TPAP {step} for {self._host}: device locked after failed logins, "
                f"retry in {minutes} minutes",
                locked_minutes=minutes,
            )

    async def _discover(self) -> _LoginParameters:
        """Ask the device for its TPAP login values (in-band, over HTTP)."""
        status, body = await self._post(
            self._base_url(self._port),
            json_body={"method": "login", "params": {"sub_method": "discover"}},
        )
        reply = _parse_json_object(body)
        if status != 200 or reply is None:
            raise TpapDiscoveryError(
                f"{self._host} did not answer the TPAP discover (HTTP {status}); "
                "Third-Party Compatibility may be enabled"
            )
        self._raise_if_locked(reply, "discover")
        code = _error_code(reply)
        if code != ERROR_SUCCESS:
            raise TpapDiscoveryError(
                f"{self._host} refused the TPAP discover (error {code}); "
                "Third-Party Compatibility may be enabled"
            )
        result = reply.get("result")
        tpap = result.get("tpap") if isinstance(result, dict) else None
        if not isinstance(result, dict) or not isinstance(tpap, dict):
            raise TpapDiscoveryError(
                f"{self._host} does not announce TPAP; "
                "Third-Party Compatibility may be enabled"
            )

        tls_raw = tpap.get("tls")
        tls = 0 if tls_raw is None else _as_int(tls_raw)
        if tls != 0:
            raise TpapUnsupportedError(
                f"{self._host} requires TPAP over TLS (mode {tls}), not supported"
            )
        pake_raw = tpap.get("pake")
        pake = (
            {v for v in map(_as_int, pake_raw) if v is not None}
            if isinstance(pake_raw, list)
            else set()
        )
        if PAKE_USERPW not in pake:
            raise TpapUnsupportedError(
                f"{self._host} does not accept the account password login "
                f"(pake {sorted(pake)})"
            )
        port = _as_int(tpap.get("port")) or self._port
        if not 0 < port < 65536:
            raise TpapProtocolError(f"{self._host} announced an invalid TPAP port")
        mac = normalize_mac(result.get("mac"))
        _LOGGER.debug(
            "TPAP discover for %s: tls %s, port %s, pake %s, dac %s",
            self._host,
            tls,
            port,
            sorted(pake),
            _as_int(tpap.get("dac")) == 1,
        )
        return _LoginParameters(port=port, mac_hex=mac.replace(":", "") if mac else "")

    async def _login_step(
        self, port: int, params: dict[str, Any], step: str
    ) -> dict[str, Any]:
        status, body = await self._post(
            self._base_url(port), json_body={"method": "login", "params": params}
        )
        reply = _parse_json_object(body)
        if status != 200 or reply is None:
            raise TpapProtocolError(
                f"TPAP {step} for {self._host} failed: HTTP {status}"
            )
        self._raise_if_locked(reply, step)
        code = _error_code(reply)
        if code != ERROR_SUCCESS:
            if code == ERROR_DEVICE_BLOCKED:
                self._locked_until = time.monotonic() + DEFAULT_LOCKOUT_MINUTES * 60
                raise TpapDeviceLockedError(
                    f"TPAP {step} for {self._host}: device blocked logins",
                    locked_minutes=DEFAULT_LOCKOUT_MINUTES,
                )
            if code in AUTH_ERROR_CODES or step == "pake_share":
                # Any refused pake_share counts toward the lockout: never
                # treat it as transient.
                raise TpapAuthError(
                    f"TPAP {step} for {self._host}: credentials rejected (error {code})"
                )
            if code in RETRYABLE_ERROR_CODES:
                raise TpapConnectionError(
                    f"TPAP {step} for {self._host}: transient error {code}"
                )
            raise TpapProtocolError(
                f"TPAP {step} for {self._host} failed: error {code}"
            )
        result = reply.get("result")
        if not isinstance(result, dict):
            raise TpapProtocolError(f"TPAP {step} response missing result object")
        return result

    async def _login(self) -> None:
        """Discover (once), then pake_register and pake_share."""
        self._check_lockout()
        self._drop_session()
        if self._login_params is None:
            self._login_params = await self._discover()
        login = self._login_params

        user_random = secrets.token_bytes(32)
        register = await self._login_step(
            login.port,
            {
                "sub_method": "pake_register",
                "username": _REGISTER_USERNAME,
                "user_random": base64.b64encode(user_random).decode(),
                "cipher_suites": [OFFERED_CIPHER_SUITE],
                "encryption": [CIPHER_AES_128_CCM],
                "passcode_type": PASSCODE_TYPE_USERPW,
            },
            "pake_register",
        )
        _LOGGER.debug("pake_register with %s succeeded", self._host)

        if _as_int(register.get("cipher_suites")) != OFFERED_CIPHER_SUITE:
            raise TpapUnsupportedError(
                "Device chose a SPAKE2+ suite that was not offered"
            )
        encryption = str(register.get("encryption") or "").lower().replace("-", "_")
        if encryption != CIPHER_AES_128_CCM:
            raise TpapUnsupportedError(
                "Device chose a session cipher that was not offered"
            )
        iterations_raw = register.get("iterations")
        iterations = (
            DEFAULT_PAKE_ITERATIONS
            if iterations_raw is None
            else _as_int(iterations_raw)
        )
        if iterations is None or not 0 < iterations <= MAX_PAKE_ITERATIONS:
            raise TpapProtocolError("TPAP register response has invalid iterations")
        extra_crypt = register.get("extra_crypt")

        share_values = await asyncio.get_running_loop().run_in_executor(
            None,
            functools.partial(
                _compute_share,
                username=self._username,
                password=self._password,
                mac_hex=login.mac_hex,
                extra_crypt=extra_crypt if isinstance(extra_crypt, dict) else None,
                salt=_b64decode(register.get("dev_salt"), "dev_salt"),
                iterations=iterations,
                user_random=user_random,
                dev_random=_b64decode(register.get("dev_random"), "dev_random"),
                dev_share=_b64decode(register.get("dev_share"), "dev_share"),
            ),
        )

        try:
            share = await self._login_step(
                login.port, share_values.params, "pake_share"
            )
        except TpapConnectionError as err:
            # The device may have counted this attempt; do not resend now.
            raise type(err)(str(err), retryable=False) from None
        dev_confirm = share.get("dev_confirm")
        if not isinstance(dev_confirm, str) or not dev_confirm:
            raise TpapProtocolError("TPAP share response missing dev_confirm")
        # base64 is case-sensitive: compare exactly, in constant time.
        if not hmac.compare_digest(
            dev_confirm.encode(), share_values.expected_dev_confirm
        ):
            raise TpapConfirmationError(
                f"{self._host} failed the PAKE confirmation; session refused"
            )
        _LOGGER.debug("pake_share confirmation from %s verified", self._host)

        stok = share.get("sessionId") or share.get("stok")
        if (
            not isinstance(stok, str)
            or not _STOK_RE.match(stok)
            or _STOK_FORBIDDEN.intersection(stok)
        ):
            raise TpapProtocolError("TPAP share response has an invalid session id")
        start_seq = _as_int(share.get("start_seq"))
        if start_seq is None:
            raise TpapProtocolError("TPAP share response missing start_seq")

        self._session = TpapEncryptionSession(share_values.shared_key, start_seq)
        self._ds_url = self._base_url(login.port).with_path(f"/stok={stok}/ds")
        _LOGGER.debug("TPAP session with %s established", self._host)

    # -------------------------------------------------------------- request

    async def _send(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        session, ds_url = self._session, self._ds_url
        if session is None or ds_url is None:
            raise TpapProtocolError("TPAP session is not established")
        request: dict[str, Any] = {
            "method": method,
            "request_time_milis": round(time.time() * 1000),
            "terminal_uuid": self._terminal_uuid,
        }
        if params:
            request["params"] = params
        try:
            payload, seq = session.encrypt(
                json.dumps(request, separators=(",", ":")).encode()
            )
        except SessionExhaustedError as err:
            raise _SessionLostError("sequence exhausted") from err

        status, body = await self._post(ds_url, data=payload)
        plain_reply = _parse_json_object(body)
        if plain_reply is not None or status != 200:
            # A real answer is always encrypted. A plain reply can only report
            # an error; it is never trusted as data, or anyone on the network
            # path could answer for the device.
            code = _error_code(plain_reply) if plain_reply is not None else None
            if code in SESSION_LOST_ERROR_CODES:
                raise _SessionLostError(str(code))
            self._drop_session()
            if status != 200:
                raise TpapConnectionError(f"{self._host} answered HTTP {status}")
            raise TpapProtocolError(
                f"{self._host} sent an unencrypted reply (error {code})"
            )

        try:
            plaintext = session.decrypt(body, seq)
        except InvalidTag:
            self._drop_session()
            raise TpapProtocolError(
                f"Response from {self._host} failed authentication"
            ) from None
        except TpapProtocolError:
            self._drop_session()
            raise

        reply = _parse_json_object(plaintext)
        if reply is None:
            raise TpapProtocolError(f"{self._host} sent an invalid response body")
        code = _error_code(reply)
        if code == ERROR_SUCCESS:
            result = reply.get("result")
            return result if isinstance(result, dict) else {}
        if code in SESSION_LOST_ERROR_CODES:
            raise _SessionLostError(str(code))
        if code in AUTH_ERROR_CODES:
            self._drop_session()
            raise TpapAuthError(f"{self._host} rejected the session (error {code})")
        raise TpapDeviceError(
            f"{self._host} returned error {code} for {method}", error_code=code or -1
        )
