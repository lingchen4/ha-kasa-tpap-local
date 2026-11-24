"""A simulated TPAP Kasa switch served over real HTTP, for tests.

The device side of SPAKE2+ follows ``MockTpapDevice`` in python-kasa's
``tests/transports/test_tpaptransport.py`` (ZeliardM/python-kasa commit
f4dff399016a9daa47316ffb209f6b74a989b6dc, GPL-3.0-or-later). It holds the
password verifier like a device does and refuses logins that do not prove it.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import secrets
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from ecdsa import ellipticcurve

from custom_components.kasa_tpap.vendor.tpap import crypto
from custom_components.kasa_tpap.vendor.tpap.session import key_nonce_from_shared

USERNAME = "user@example.invalid"
PASSWORD = "not-a-real-password"
MAC = "74-FE-CE-12-34-56"
STOK = "abcdef0123456789"
PASSWORD_SHADOW = {"type": "password_shadow", "params": {"passwd_id": 2}}


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


class _Suite:
    def __init__(self) -> None:
        m_comp, n_comp, nist, self.ec_curve = crypto._suite_parameters(1)
        self.curve = nist.curve
        self.g = nist.generator
        self.order = int(self.g.order())
        self.order_len = (self.order.bit_length() + 7) // 8
        self.m = self.point(m_comp)
        self.n = self.point(n_comp)

    def point(self, sec1: bytes) -> Any:
        x, y = crypto._sec1_to_xy(sec1, self.ec_curve)
        return ellipticcurve.Point(self.curve, x, y, self.order)

    def encode(self, point: Any) -> bytes:
        return crypto._xy_to_uncompressed(point.x(), point.y(), self.ec_curve)


@dataclass
class SimulatedDevice:
    """Behaviour switches for one simulated HS200."""

    username: str = USERNAME
    password: str = PASSWORD
    mac: str = MAC
    extra_crypt: dict[str, Any] | None = field(
        default_factory=lambda: dict(PASSWORD_SHADOW)
    )
    is_on: bool | None = False  # None: no relay (power strip parent)
    model: str = "HS200"
    device_type: str = "SMART.KASASWITCH"
    components: list[str] = field(default_factory=lambda: ["device", "led"])
    brightness: int | None = None  # dimmers
    energy: dict[str, Any] | None = None  # get_energy_usage result
    current_power_w: float | None = None  # get_current_power result
    children: list[dict[str, Any]] = field(default_factory=list)
    child_page_size: int = 2
    tpap: dict[str, Any] = field(
        default_factory=lambda: {"tls": 0, "pake": [2], "dac": 0}
    )
    start_seq: int = 100
    # Fault injection
    discover_reply: Any = None  # replaces the discover answer (dict or str)
    register_overrides: dict[str, Any] = field(default_factory=dict)
    share_overrides: dict[str, Any] = field(default_factory=dict)
    tamper_dev_confirm: bool = False
    reply_seq_offset: int = 0  # reply with request seq + offset
    share_error_code: int | None = None  # refuse pake_share with this code
    share_delay: float = 0.0  # delay the pake_share answer only
    expire_session_after: int | None = None  # requests before -40401
    delay: float = 0.0
    redirect_to: str | None = None
    locked_minutes: int = 0
    plain_ds_reply: dict[str, Any] | None = None
    # Observations
    calls: Counter[str] = field(default_factory=Counter)
    failed_logins: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    concurrent: int = 0
    max_concurrent: int = 0
    port: int = 0

    def __post_init__(self) -> None:
        self._suite = _Suite()
        self._aead: AESCCM | None = None
        self._base_nonce = b""
        self._session_requests = 0

    # ------------------------------------------------------------ app

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/", self._login)
        app.router.add_post("/stok={stok}/ds", self._ds)
        return app

    async def _guard(self) -> None:
        self.calls["http"] += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        await asyncio.sleep(self.delay or 0)

    async def _login(self, request: web.Request) -> web.StreamResponse:
        await self._guard()
        try:
            if self.redirect_to:
                raise web.HTTPFound(self.redirect_to)
            body = await request.json()
            step = body["params"]["sub_method"]
            self.calls[step] += 1
            if self.locked_minutes:
                return web.json_response(
                    {
                        "error_code": -40404,
                        "error_info": {"lockedMinute": self.locked_minutes},
                    }
                )
            if step == "discover":
                if self.discover_reply is not None:
                    if isinstance(self.discover_reply, str):
                        return web.Response(
                            text=self.discover_reply, content_type="text/html"
                        )
                    return web.json_response(self.discover_reply)
                tpap = dict(self.tpap)
                tpap.setdefault("port", self.port)
                return web.json_response(
                    {"error_code": 0, "result": {"mac": self.mac, "tpap": tpap}}
                )
            if step == "pake_register":
                return web.json_response(self._register(body["params"]))
            if step == "pake_share":
                await asyncio.sleep(self.share_delay)
                if self.share_error_code is not None:
                    self.failed_logins += 1
                    return web.json_response({"error_code": self.share_error_code})
                return web.json_response(self._share(body["params"]))
            return web.json_response({"error_code": -1})
        finally:
            self.concurrent -= 1

    def _verifier_secret(self) -> str:
        return crypto.build_credentials(
            self.extra_crypt, self.username, self.password, self.mac.replace("-", "")
        )

    def _register(self, params: dict[str, Any]) -> dict[str, Any]:
        assert params["passcode_type"] == "userpw"
        assert params["cipher_suites"] == [1]
        suite = self._suite
        salt = secrets.token_bytes(16)
        iterations = 100
        w0, w1 = crypto._derive_ab(
            self._verifier_secret().encode(), salt, iterations, 32
        )
        self._w0, self._w1 = w0 % suite.order, w1 % suite.order
        self._y = secrets.randbelow(suite.order - 1) + 1
        self._dev_share = self._y * suite.g + self._w0 * suite.n
        self._user_random = base64.b64decode(params["user_random"])
        self._dev_random = secrets.token_bytes(32)
        result: dict[str, Any] = {
            "cipher_suites": 1,
            "iterations": iterations,
            "encryption": "aes_128_ccm",
            "dev_random": _b64(self._dev_random),
            "dev_salt": _b64(salt),
            "dev_share": _b64(suite.encode(self._dev_share)),
        }
        if self.extra_crypt is not None:
            result["extra_crypt"] = self.extra_crypt
        result.update(self.register_overrides)
        return {"error_code": 0, "result": result}

    def _share(self, params: dict[str, Any]) -> dict[str, Any]:
        suite = self._suite
        user_share = suite.point(base64.b64decode(params["user_share"]))
        z = self._y * (user_share + (-(self._w0 * suite.m)))
        v = self._y * (self._w1 * suite.g)
        context = crypto._hash(
            "SHA256", crypto.PAKE_CONTEXT_TAG + self._user_random + self._dev_random
        )
        transcript = b"".join(
            crypto._len8le(part)
            for part in (
                context,
                b"",
                b"",
                suite.encode(suite.m),
                suite.encode(suite.n),
                suite.encode(user_share),
                suite.encode(self._dev_share),
                suite.encode(z),
                suite.encode(v),
                crypto._encode_w(self._w0, suite.order_len),
            )
        )
        transcript_hash = crypto._hash("SHA256", transcript)
        keys = crypto._hkdf_expand("ConfirmationKeys", transcript_hash, 64, "SHA256")
        shared_key = crypto._hkdf_expand("SharedKey", transcript_hash, 32, "SHA256")
        expected = crypto._hmac("SHA256", keys[:32], suite.encode(self._dev_share))
        if not hmac.compare_digest(expected, base64.b64decode(params["user_confirm"])):
            self.failed_logins += 1
            return {
                "error_code": -2203,
                "error_info": {"remainAttempts": 15 - self.failed_logins},
            }
        dev_confirm = crypto._hmac("SHA256", keys[32:], suite.encode(user_share))
        if self.tamper_dev_confirm:
            dev_confirm = bytes(b ^ 0xFF for b in dev_confirm)
        key, self._base_nonce = key_nonce_from_shared(shared_key)
        self._aead = AESCCM(key, tag_length=16)
        self._session_requests = 0
        result = {
            "dev_confirm": _b64(dev_confirm),
            "stok": STOK,
            "start_seq": self.start_seq,
        }
        result.update(self.share_overrides)
        return {"error_code": 0, "result": result}

    async def _ds(self, request: web.Request) -> web.StreamResponse:
        await self._guard()
        try:
            self.calls["request"] += 1
            data = await request.read()
            if self.redirect_to:
                raise web.HTTPFound(self.redirect_to)
            if self._aead is None or request.match_info["stok"] != STOK:
                return web.json_response({"error_code": -40401})
            if (
                self.expire_session_after is not None
                and self._session_requests >= self.expire_session_after
            ):
                self._aead = None
                return web.json_response({"error_code": -40401})
            self._session_requests += 1
            if self.plain_ds_reply is not None:
                return web.json_response(self.plain_ds_reply)
            seq = data[:4]
            nonce = self._base_nonce[:-4] + seq
            req = json.loads(self._aead.decrypt(nonce, data[4:], None))
            self.requests.append(req)
            reply = self._handle(req)
            if self.reply_seq_offset:
                seq = (int.from_bytes(seq, "big") + self.reply_seq_offset).to_bytes(
                    4, "big"
                )
                nonce = self._base_nonce[:-4] + seq
            payload = seq + self._aead.encrypt(nonce, json.dumps(reply).encode(), None)
            return web.Response(body=payload, content_type="application/octet-stream")
        finally:
            self.concurrent -= 1

    def _handle(self, req: dict[str, Any]) -> dict[str, Any]:
        method = req["method"]
        params = req.get("params") or {}
        if method == "component_nego":
            return {
                "error_code": 0,
                "result": {
                    "component_list": [
                        {"id": c, "ver_code": 1} for c in self.components
                    ]
                },
            }
        if method == "get_device_info":
            result: dict[str, Any] = {
                "device_id": "8022" + "0" * 36,
                "fw_ver": "1.4.0 Build 260910 Rel.183540",
                "hw_ver": "5.26",
                "mac": self.mac,
                "model": self.model,
                "nickname": _b64(b"porch"),
                "rssi": -32,
                "type": self.device_type,
            }
            if self.is_on is not None:
                result["device_on"] = self.is_on
            if self.brightness is not None:
                result["brightness"] = self.brightness
            return {"error_code": 0, "result": result}
        if method == "set_device_info":
            if "device_on" in params:
                self.is_on = bool(params["device_on"])
            if "brightness" in params:
                self.brightness = int(params["brightness"])
            return {"error_code": 0}
        if method == "get_energy_usage" and self.energy is not None:
            return {"error_code": 0, "result": self.energy}
        if method == "get_current_power" and self.current_power_w is not None:
            return {"error_code": 0, "result": {"current_power": self.current_power_w}}
        if method == "get_child_device_list" and self.children:
            start = int(params.get("start_index", 0))
            page = self.children[start : start + self.child_page_size]
            return {
                "error_code": 0,
                "result": {
                    "child_device_list": page,
                    "start_index": start,
                    "sum": len(self.children),
                },
            }
        if method == "control_child":
            child = next(
                (c for c in self.children if c["device_id"] == params["device_id"]),
                None,
            )
            if child is None:
                return {"error_code": 0, "result": {"responseData": {"error_code": -1}}}
            inner = params["requestData"]
            child["device_on"] = bool(inner["params"]["device_on"])
            return {"error_code": 0, "result": {"responseData": {"error_code": 0}}}
        return {"error_code": -1}


def child(
    device_id: str, position: int, name: bytes, on: bool = False
) -> dict[str, Any]:
    """A child_device_list entry of a power strip."""
    return {
        "device_id": device_id,
        "device_on": on,
        "nickname": _b64(name),
        "position": position,
        "model": "EP40M",
    }
