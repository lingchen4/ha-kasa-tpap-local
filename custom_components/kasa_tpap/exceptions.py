"""Exceptions raised by the Kasa TPAP Local client.

Messages never contain credentials, session ids, keys or payloads, so they are
safe to log and to show in the UI.
"""

from __future__ import annotations


class TpapError(Exception):
    """Base class for every error raised by the TPAP client."""


class TpapConnectionError(TpapError):
    """The device could not be reached or the connection broke.

    ``retryable`` is False when the request may already have counted toward
    the device's login lockout (a lost ``pake_share`` reply).
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        """Store whether the client may retry immediately."""
        super().__init__(message)
        self.retryable = retryable


class TpapTimeoutError(TpapConnectionError):
    """The device did not answer in time."""


class TpapProtocolError(TpapError):
    """The device answered with something the protocol does not allow."""


class TpapDiscoveryError(TpapProtocolError):
    """The in-band TPAP discover failed.

    Seen when the device does not speak TPAP on this port, typically because
    Third-Party Compatibility is enabled in the TP-Link app.
    """


class TpapConfirmationError(TpapProtocolError):
    """The device's PAKE confirmation did not match.

    The device accepted the login but could not prove it knows the password
    verifier, so it may not be the real device. The session is refused.
    """


class TpapUnsupportedError(TpapProtocolError):
    """The device asked for a TPAP mode this integration does not implement."""


class TpapAuthError(TpapError):
    """The device rejected the TP-Link account credentials."""


class TpapDeviceLockedError(TpapAuthError):
    """The device locked logins after too many failed attempts."""

    def __init__(self, message: str, locked_minutes: int | None = None) -> None:
        """Store how long the device said it stays locked."""
        super().__init__(message)
        self.locked_minutes = locked_minutes


class TpapDeviceError(TpapError):
    """An authenticated request returned a device error code."""

    def __init__(self, message: str, error_code: int) -> None:
        """Store the device error code."""
        super().__init__(message)
        self.error_code = error_code


class InvalidHostError(TpapError):
    """The configured address is not an acceptable local device address."""
