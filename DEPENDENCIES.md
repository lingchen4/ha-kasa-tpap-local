# Dependency inventory

Reviewed 2026-11-24 against Home Assistant 2026.10.4 (test harness) and the
`dev` branch constraints of Home Assistant Core.

## Runtime dependencies

| Package | Version | Source | License | Added by this integration? | Why |
|---|---|---|---|---|---|
| `aiohttp` | HA's (3.14.5) | Home Assistant Core | Apache-2.0 | No | HTTP to the device |
| `yarl` | HA's (1.25.x) | Home Assistant Core | Apache-2.0 | No | URL building |
| `cryptography` | HA's (50.0.2) | Home Assistant Core | Apache-2.0 OR BSD-3-Clause | No | HKDF, HMAC, CMAC, AES-CCM, EC point decoding |
| `probatio` | HA's (0.13.0) | Home Assistant Core | MIT | No | Config-flow schemas (HA's voluptuous successor) |
| **`ecdsa`** | **`==0.19.2`** | PyPI | MIT | **Yes** | Elliptic-curve point addition and scalar multiplication for SPAKE2+ |
| `six` | ≥1.9.0 (pulled in by `ecdsa`) | PyPI, normally already installed | MIT | Transitively | `ecdsa` compatibility shim |

Home Assistant built-in integrations used: `network` (manifest
dependency; lists Home Assistant's LAN interfaces for the scan) and `dhcp`
(via the manifest's `dhcp` matchers; reports TP-Link MAC prefixes). Both ship
with Home Assistant Core.

Everything else is the Python standard library (`hashlib`, `hmac`,
`secrets`, `ipaddress`, `json`, `base64`, `struct`).

### Why `ecdsa`

SPAKE2+ needs `x·G + w·M` and `x·(R − w·N)` on NIST P-256. `cryptography`
deliberately exposes no raw point addition or scalar multiplication with
arbitrary points, so the upstream TPAP implementation (and the reference
`tapo-p110-ha` integration) use `ecdsa` for this. No other library already
shipped with Home Assistant offers it. Re-implementing curve arithmetic was
ruled out (no home-made cryptographic primitives).

### Security implications of `ecdsa`

- Pure Python, maintained (tlsfuzzer project), MIT, no compiled code.
- Its maintainers state it is **not constant-time** and not hardened against
  side channels. Here it processes an ephemeral scalar and the
  password-derived scalar `w0` during login. Exploiting timing would need
  precise measurements of the Home Assistant host's CPU during logins; on a
  LAN appliance this is judged low risk, but it is a real limitation.
- Pinned to an exact version. Home Assistant installs it from PyPI with `pip`
  the first time the integration loads; Home Assistant does not verify
  hashes. To check what was installed, compare against the PyPI digests:
  - `ecdsa-0.19.2-py2.py3-none-any.whl`
    sha256 `840f5dc5e375c68f36c1a7a5b9caad28f95daa65185c9253c0c08dd952bb7399`
  - `ecdsa-0.19.2.tar.gz`
    sha256 `62635b0ac1ca2e027f82122b5b81cb706edc38cd91c63dda28e4f3455a2bf930`
- Nothing else is downloaded, at install or at runtime.
- Does not conflict with the official TP-Link integration: python-kasa 0.12.0
  does not depend on `ecdsa`. The upstream TPAP branch asks for
  `ecdsa>=0.19.1`, which 0.19.2 satisfies, should it be released.

## Vendored code

| Code | Upstream | Revision | License |
|---|---|---|---|
| `vendor/tpap/crypto.py` (verbatim but one import) | ZeliardM/python-kasa `feature/tpap` | `f4dff399016a9daa47316ffb209f6b74a989b6dc` | GPL-3.0-or-later |
| `vendor/tpap/session.py`, `client.py` (derived) | same | same | GPL-3.0-or-later |
| `tests/device_simulator.py` (derived from upstream test mock) | same | same | GPL-3.0-or-later |

Details and the full list of changes: `custom_components/kasa_tpap/vendor/tpap/UPSTREAM.md`.
Because GPL code is included, this whole integration is distributed under
GPL-3.0-or-later (`LICENSE`).

## Not used, on purpose

| Option | Reason |
|---|---|
| `python-kasa` from the `feature/tpap` git branch as a requirement | Would replace the `kasa` package that Home Assistant's TP-Link integration pins (`python-kasa[speedups]==0.12.0`) in the shared environment, and depends on a moving branch |
| `python-kasa` released on PyPI | No TPAP support in any release at review time (latest 0.12.0) |
| `rabilrbl/tapo-p110-ha`'s `tpap_client.py` (MIT) | Blocking `urllib` calls, unpinned `ecdsa`, non-constant-time and case-folded comparison of the device confirmation, Tapo-plug specific |
| `passlib` | Not needed: the vendored `crypto.py` already implements the `crypt()` encodings the protocol requires |

## Development-only dependencies

`pytest-homeassistant-custom-component==0.13.375` (pins Home Assistant
2026.10.4 and pytest), `ruff`, `mypy`. None of these are installed on the
Home Assistant host.
