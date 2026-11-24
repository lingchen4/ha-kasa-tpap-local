"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from custom_components.kasa_tpap import client as client_module
from custom_components.kasa_tpap.client import TpapClient

from .device_simulator import PASSWORD, USERNAME, SimulatedDevice


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
async def device(socket_enabled: None) -> AsyncGenerator[SimulatedDevice]:
    """A simulated HS200 listening on 127.0.0.1."""
    sim = SimulatedDevice()
    server = TestServer(sim.app(), host="127.0.0.1")
    await server.start_server()
    sim.port = server.port
    sim.server = server  # type: ignore[attr-defined]
    yield sim
    await server.close()


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the client reach the simulator on 127.0.0.1.

    Production code refuses loopback; only the client tests relax this.
    """
    monkeypatch.setattr(client_module, "validate_device_host", lambda host: host)


@pytest.fixture
async def make_client(
    device: SimulatedDevice, allow_loopback: None
) -> AsyncGenerator[Callable[..., TpapClient]]:
    """Build clients talking to the simulator; closes their sessions."""
    sessions: list[aiohttp.ClientSession] = []

    def _make(**kwargs: Any) -> TpapClient:
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(force_close=True),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        sessions.append(session)
        kwargs.setdefault("retry_backoff", 0)
        return TpapClient(
            kwargs.pop("host", "127.0.0.1"),
            kwargs.pop("username", USERNAME),
            kwargs.pop("password", PASSWORD),
            session,
            port=device.port,
            **kwargs,
        )

    yield _make
    for session in sessions:
        await session.close()
