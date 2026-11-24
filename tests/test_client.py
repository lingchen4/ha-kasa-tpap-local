"""Tests of the TPAP client against a simulated device over real HTTP."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.kasa_tpap.client import MAX_ATTEMPTS, TpapClient
from custom_components.kasa_tpap.exceptions import (
    TpapAuthError,
    TpapConfirmationError,
    TpapConnectionError,
    TpapDeviceLockedError,
    TpapDiscoveryError,
    TpapProtocolError,
    TpapTimeoutError,
    TpapUnsupportedError,
)

from .device_simulator import PASSWORD, STOK, USERNAME, SimulatedDevice

MakeClient = Callable[..., TpapClient]


async def test_handshake_and_device_info(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A full SPAKE2+ login, then an encrypted get_device_info."""
    client = make_client()
    info = await client.async_get_device_info()

    assert info.mac == "74:fe:ce:12:34:56"
    assert info.model == "HS200"
    assert info.hardware_version == "5.26"
    assert info.firmware_version == "1.4.0 Build 260910 Rel.183540"
    assert info.nickname == "porch"
    assert info.is_on is False
    assert info.rssi == -32
    assert device.calls == {
        "http": 4,
        "discover": 1,
        "pake_register": 1,
        "pake_share": 1,
        "request": 1,
    }
    assert client.has_session


async def test_session_is_reused(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Later requests use the established session without logging in."""
    client = make_client()
    for _ in range(3):
        await client.async_get_device_info()
    assert device.calls["pake_share"] == 1
    assert device.calls["request"] == 3


async def test_username_is_part_of_the_secret(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Without extra_crypt the device expects ``username/password``."""
    device.extra_crypt = None
    assert (await make_client().async_get_device_info()).model == "HS200"
    with pytest.raises(TpapAuthError):
        await make_client(username="other@example.invalid").async_get_device_info()


async def test_turn_on_and_off(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """set_device_info switches the relay."""
    client = make_client()
    await client.async_set_device_on(True)
    assert device.is_on is True
    assert (await client.async_get_device_info()).is_on is True
    await client.async_set_device_on(False)
    assert device.is_on is False
    assert device.requests[0]["params"] == {"device_on": True}


async def test_physical_change_is_seen(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A change made at the wall switch shows up on the next poll."""
    client = make_client()
    assert (await client.async_get_device_info()).is_on is False
    device.is_on = True
    assert (await client.async_get_device_info()).is_on is True


async def test_invalid_credentials_are_not_retried(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A refused pake_share counts toward the lockout, so it is sent once."""
    client = make_client(password="wrong")
    with pytest.raises(TpapAuthError):
        await client.async_get_device_info()
    assert device.calls["pake_share"] == 1
    assert device.failed_logins == 1
    assert not client.has_session


async def test_pake_confirmation_failure(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A device that cannot prove the verifier gets no session."""
    device.tamper_dev_confirm = True
    client = make_client()
    with pytest.raises(TpapConfirmationError):
        await client.async_get_device_info()
    assert not client.has_session
    assert device.calls["request"] == 0


async def test_timeout_is_retried_then_raised(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Timeouts are bounded: MAX_ATTEMPTS tries, then TpapTimeoutError."""
    device.delay = 0.5
    client = make_client(request_timeout=0.1)
    with pytest.raises(TpapTimeoutError):
        await client.async_get_device_info()
    assert device.calls["http"] == MAX_ATTEMPTS
    assert device.calls["pake_register"] == 0


async def test_session_expiry_logs_in_again(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """-40401 on /ds drops the session; the request is resent after login."""
    device.expire_session_after = 1
    client = make_client()
    await client.async_get_device_info()
    info = await client.async_get_device_info()
    assert info.model == "HS200"
    assert device.calls["pake_share"] == 2
    assert device.calls["discover"] == 1  # login values are kept


async def test_disconnect_and_recovery(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """The device going away raises; it is used again when it comes back."""
    client = make_client()
    await client.async_get_device_info()
    server: TestServer = device.server  # type: ignore[attr-defined]
    await server.close()
    with pytest.raises(TpapConnectionError):
        await client.async_get_device_info()

    revived = TestServer(device.app(), host="127.0.0.1", port=device.port)
    await revived.start_server()
    try:
        assert (await client.async_get_device_info()).model == "HS200"
    finally:
        await revived.close()


async def test_concurrent_requests_are_serialised(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Parallel callers share one login and never overlap on the wire."""
    device.delay = 0.01
    client = make_client()
    results = await asyncio.gather(*(client.async_get_device_info() for _ in range(5)))
    assert all(r.model == "HS200" for r in results)
    assert device.calls["pake_share"] == 1
    assert device.max_concurrent == 1


async def test_redirect_is_refused(
    device: SimulatedDevice, make_client: MakeClient, socket_enabled: None
) -> None:
    """A redirect is never followed, so credentials cannot leave the device."""
    hits = 0

    async def _elsewhere(request: web.Request) -> web.Response:
        nonlocal hits
        hits += 1
        return web.json_response({})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", _elsewhere)
    other = TestServer(app, host="127.0.0.1")
    await other.start_server()
    try:
        device.redirect_to = f"http://127.0.0.1:{other.port}/steal"
        with pytest.raises(TpapProtocolError, match="redirect"):
            await make_client().async_get_device_info()
        assert hits == 0
    finally:
        await other.close()


async def test_reply_with_an_older_sequence_is_refused(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A reply numbered below its request (a replay) is refused."""
    client = make_client()
    await client.async_get_device_info()
    device.reply_seq_offset = -1
    with pytest.raises(TpapProtocolError, match="sequence"):
        await client.async_get_device_info()
    assert not client.has_session


async def test_reply_with_a_newer_sequence_is_accepted(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Like upstream, a reply numbered above its request is accepted once."""
    device.reply_seq_offset = 1
    client = make_client()
    assert (await client.async_get_device_info()).model == "HS200"
    # The next request reuses that number: replaying it is refused.
    device.reply_seq_offset = 0
    with pytest.raises(TpapProtocolError, match="sequence"):
        await client.async_get_device_info()


async def test_plaintext_reply_is_never_trusted(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """An unencrypted /ds answer is not taken as device data."""
    device.plain_ds_reply = {"error_code": 0, "result": {"device_on": True}}
    client = make_client()
    with pytest.raises(TpapProtocolError, match="unencrypted"):
        await client.async_get_device_info()


@pytest.mark.parametrize(
    "reply",
    [
        "<html><body><center>200 OK</center></body></html>",
        {"error_code": -40210},
        {"error_code": 0, "result": {"mac": "74-FE-CE-12-34-56"}},
    ],
)
async def test_discovery_failure(
    device: SimulatedDevice, make_client: MakeClient, reply: object
) -> None:
    """No usable TPAP discover answer (e.g. Third-Party Compatibility on)."""
    device.discover_reply = reply
    with pytest.raises(TpapDiscoveryError, match="Third-Party Compatibility"):
        await make_client().async_get_device_info()
    assert device.calls["pake_register"] == 0


@pytest.mark.parametrize(
    ("tpap", "register"),
    [
        ({"tls": 2, "pake": [2]}, {}),
        ({"tls": 0, "pake": [0]}, {}),
        ({"tls": 0, "pake": [2]}, {"cipher_suites": 2}),
        ({"tls": 0, "pake": [2]}, {"encryption": "chacha20_poly1305"}),
    ],
)
async def test_unsupported_modes(
    device: SimulatedDevice,
    make_client: MakeClient,
    tpap: dict[str, object],
    register: dict[str, object],
) -> None:
    """Modes outside the implemented path are refused, never downgraded."""
    device.tpap = tpap
    device.register_overrides = register
    with pytest.raises(TpapUnsupportedError):
        await make_client().async_get_device_info()
    assert device.calls["pake_share"] == 0


@pytest.mark.parametrize("iterations", [0, 100_001, "many", True])
async def test_bad_iterations(
    device: SimulatedDevice, make_client: MakeClient, iterations: object
) -> None:
    """The PBKDF2 count from the unauthenticated reply is bounded."""
    device.register_overrides = {"iterations": iterations}
    with pytest.raises(TpapProtocolError, match="iterations"):
        await make_client().async_get_device_info()
    assert device.calls["pake_share"] == 0


async def test_invalid_session_id(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A session id that is not a plain token is refused."""
    device.share_overrides = {"stok": "../../evil?x=1"}
    with pytest.raises(TpapProtocolError, match="session id"):
        await make_client().async_get_device_info()


async def test_lockout_is_respected(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """After a lockout the client stops contacting the device."""
    device.locked_minutes = 10
    client = make_client()
    with pytest.raises(TpapDeviceLockedError) as err:
        await client.async_get_device_info()
    assert err.value.locked_minutes == 10
    calls = sum(device.calls.values())
    with pytest.raises(TpapDeviceLockedError):
        await client.async_get_device_info()
    assert sum(device.calls.values()) == calls


async def test_oversized_response(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Response bodies are capped."""
    device.discover_reply = "x" * (300 * 1024)
    with pytest.raises(TpapProtocolError, match="too large"):
        await make_client().async_get_device_info()


async def test_close_drops_the_session(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """async_close forgets the session; the next request logs in again."""
    client = make_client()
    await client.async_get_device_info()
    await client.async_close()
    assert not client.has_session
    await client.async_get_device_info()
    assert device.calls["discover"] == 2


async def test_no_secrets_in_logs_or_errors(
    device: SimulatedDevice,
    make_client: MakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Credentials, session ids and payloads never reach the log."""
    caplog.set_level(logging.DEBUG)
    client = make_client()
    await client.async_get_device_info()
    await client.async_set_device_on(True)
    device.expire_session_after = 2  # the next request finds the session gone
    await client.async_get_device_info()
    assert device.calls["pake_share"] == 2
    with pytest.raises(TpapAuthError) as err:
        await make_client(password="wrong-" + PASSWORD).async_get_device_info()

    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.kasa_tpap")
    )
    text = ours + str(err.value) + repr(client)
    for secret in (PASSWORD, USERNAME, STOK, "device_on", "user_confirm"):
        assert secret not in text
    assert "pake_share confirmation from 127.0.0.1 verified" in ours


@pytest.mark.parametrize("code", [-1, -40210, 1234])
async def test_any_refused_pake_share_is_an_auth_error(
    device: SimulatedDevice, make_client: MakeClient, code: int
) -> None:
    """Every pake_share refusal counts toward the lockout: never retried."""
    device.share_error_code = code
    client = make_client()
    with pytest.raises(TpapAuthError):
        await client.async_get_device_info()
    assert device.calls["pake_share"] == 1


async def test_lost_pake_share_reply_is_not_retried(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """If the pake_share answer is lost, the login is not sent again."""
    device.share_delay = 0.5
    client = make_client(request_timeout=0.2)
    with pytest.raises(TpapTimeoutError):
        await client.async_get_device_info()
    assert device.calls["pake_share"] == 1


async def test_rejected_credentials_are_not_used_again(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """After a rejection, the client refuses to log in until reconfigured."""
    client = make_client(password="wrong")
    with pytest.raises(TpapAuthError):
        await client.async_get_device_info()
    calls = sum(device.calls.values())
    with pytest.raises(TpapAuthError, match="re-authenticate"):
        await client.async_set_device_on(True)
    assert sum(device.calls.values()) == calls
    assert device.failed_logins == 1


async def test_lockout_time_is_capped(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """A spoofed huge lockout cannot disable the device for months."""
    device.locked_minutes = 10**12
    with pytest.raises(TpapDeviceLockedError) as err:
        await make_client().async_get_device_info()
    assert err.value.locked_minutes == 24 * 60


@pytest.mark.parametrize(
    "extra_crypt",
    [{"type": 5, "params": {}}, {"type": ["x"], "params": {"passwd_id": 2}}],
)
async def test_malformed_extra_crypt_is_a_protocol_error(
    device: SimulatedDevice, make_client: MakeClient, extra_crypt: object
) -> None:
    """Bad register data raises a TPAP error, not an unexpected exception."""
    device.register_overrides = {"extra_crypt": extra_crypt}
    with pytest.raises(TpapProtocolError):
        await make_client().async_get_device_info()


async def test_session_id_with_url_safe_symbols(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Session ids may contain symbols such as + and = (base64)."""
    import custom_components.kasa_tpap.client as client_module

    assert client_module._STOK_RE.match("abc+/=")
    device.share_overrides = {"stok": "Ab+c=="}
    client = make_client()
    with pytest.raises(TpapProtocolError):
        # The simulator only serves its own session id; the client accepted
        # the format and sent the request.
        await client.async_get_device_info()
    assert device.calls["request"] >= 1


async def test_non_numeric_tls_is_refused(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """tls must be the number 0; anything else is not treated as plain HTTP."""
    device.tpap = {"tls": True, "pake": [2]}
    with pytest.raises(TpapUnsupportedError):
        await make_client().async_get_device_info()


async def test_confirmation_failure_is_final(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """After a failed confirmation no further login goes to that address."""
    device.tamper_dev_confirm = True
    client = make_client()
    with pytest.raises(TpapConfirmationError):
        await client.async_get_device_info()
    shares = device.calls["pake_share"]
    with pytest.raises(TpapConfirmationError, match="reconfigure"):
        await client.async_get_device_info()
    assert device.calls["pake_share"] == shares


async def test_get_current_power_refusal_is_not_fatal(
    device: SimulatedDevice, make_client: MakeClient
) -> None:
    """Firmware refusing get_current_power leaves the power unknown."""
    device.energy = {"today_energy": 5}
    energy = await make_client().async_get_energy()
    assert energy.power_w is None
    assert energy.today_kwh == 0.005
