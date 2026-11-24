# Vendored TPAP code: provenance and changes

| | |
|---|---|
| Upstream repository | https://github.com/ZeliardM/python-kasa |
| Branch | `feature/tpap` (open PR python-kasa/python-kasa#1592) |
| Pinned commit | `f4dff399016a9daa47316ffb209f6b74a989b6dc` (2026-11-23, "Handle malformed TPAP replies as library errors") |
| License | GPL-3.0-or-later, see `LICENSE.python-kasa` (copyright the python-kasa developers; pyHS100 © 2016 GadgetReactor) |
| Reviewed | 2026-11-24 |

At review time the official python-kasa (`master` at `fd81d62`, release 0.12.0)
contains no TPAP transport, so the experimental branch is the only source.

## Files

### `crypto.py` — verbatim copy

Copied from `kasa/transports/tpap/crypto.py`
(sha256 `bf435ca0bbdcd6cf70b3f7a45b3ed11e5899969b9b5e49a94da373ffed65bbd2`).
The only change is the import of the exception class:

```diff
-from kasa.exceptions import KasaException
+from ...exceptions import TpapProtocolError as KasaException  # vendored: was kasa.exceptions
```

Check it yourself:

```bash
git -C python-kasa show f4dff39:kasa/transports/tpap/crypto.py \
  | diff - custom_components/kasa_tpap/vendor/tpap/crypto.py
```

It contains the SPAKE2+ computation (P-256/384/521 points with `ecdsa`,
PBKDF2, HKDF, HMAC/CMAC from `hashlib` and `cryptography`) and the password
encodings the device asks for in `extra_crypt`. No cryptographic primitive is
implemented here: hashing, HKDF, HMAC, CMAC and AEAD come from `hashlib` and
`cryptography`, and elliptic-curve point arithmetic from `ecdsa`. The
`md5_crypt`/`sha256_crypt` helpers re-implement glibc `crypt()` *encodings*,
which the device's protocol mandates for some firmware.

### `session.py` — derived from `TpapEncryptionSession`

From `kasa/transports/tpaptransport.py`. Changes:

1. Only `aes_128_ccm` is kept (the only cipher the client offers).
2. **Reply sequence numbers cannot go backwards.** Upstream logs a mismatch
   between the request's and the reply's sequence number and decrypts with
   the reply's number, whatever it is. An attacker on the network path could
   therefore replay an older encrypted reply from the same session (for
   example a stale `device_on` state). Here a reply is accepted only if its
   number is at least the request's and higher than every reply accepted
   before; otherwise the session is dropped. Like upstream, a device that
   answers with a higher number still works.
3. The 32-bit sequence number cannot overflow (upstream would raise
   `struct.error`); the session is renewed instead.

### `../../client.py` — derived from `TpapTransport`

The login and request logic is a reduced re-write of upstream's transport,
limited to what a Kasa switch on plain HTTP needs. Differences:

| Topic | Upstream | Here |
|---|---|---|
| Scope | Tapo/Kasa plugs, hubs, cameras, robots; TLS modes 0–2; DAC attestation; default and setup-code passcodes | SMART-protocol plugs, switches, dimmers and power strips; only TLS mode 0, passcode type `userpw` (pake 2). Anything else raises `TpapUnsupportedError` |
| Discovery | UDP discovery values or in-band discover | In-band discover over HTTP only (also used, without credentials, by the DHCP and scan discovery) |
| Offered vs chosen suite | Accepts any suite/cipher it supports | Device must choose exactly what was offered (suite 1, `aes_128_ccm`) |
| Debug log of requests | Logs the full plaintext request | Never logs payloads, credentials, session id or keys |
| Response sequence | Any number accepted (replay possible) | Must be ≥ the request's and increasing |
| CPU-heavy SPAKE2+/PBKDF2 | Runs on the event loop | Runs in an executor thread |
| Redirects | `HttpClient` default | `allow_redirects=False`; any 3xx is an error |
| Destination | Any host | Only the validated private-LAN IP; the device cannot redirect to another host |
| Session id | Inserted in the URL unchecked | Printable ASCII, 1–256 chars, without `/`, `?`, `#`, `%` or a backslash |
| Response size | Unbounded | Capped at 256 KiB |
| Lockout | Raises `DEVICE_BLOCKED` | Also refuses further logins locally until the lockout expires (capped at 24 h, since the value is unauthenticated) |
| Refused `pake_share` | Auth error for known codes | Any refusal is an auth error; a lost reply is not retried; once rejected, the client sends no further logins until reauthentication |
| Failed PAKE confirmation | Raises; the protocol may log in again | Final: no further login to that address until the entry is reconfigured or reloaded |
| Malformed register data | Some shapes raise `AttributeError`/`TypeError` | Always a `TpapProtocolError` |
| Request protocol | SMART and IOT (`IOT.TPAP`) | SMART only (`component_nego` must succeed); IOT devices are refused |
| Owner check | md5 of account name from UDP discovery | Not available without UDP discovery; the device itself rejects other accounts |

Kept as upstream: the request JSON shape (`method`, `params`,
`request_time_milis`, `terminal_uuid`), `pake_register` parameters
(`username` = md5("admin"), random 32-byte `user_random`), PBKDF2 iteration
cap of 100,000, the error-code sets, constant-time comparison of
`dev_confirm`, never retrying a refused `pake_share`, and treating
unencrypted `/ds` replies as errors only.

## Updating

1. Review the upstream diff since the pinned commit, paying attention to
   `crypto.py`, `tpaptransport.py` and their tests.
2. Copy `crypto.py` again and re-apply the single import change.
3. Port relevant transport changes to `client.py` / `session.py`.
4. Update the commit, date and hash above and run the test suite.

If python-kasa ships TPAP in a release, consider whether this vendored copy
can be removed. It still must not be installed as a requirement while Home
Assistant's TP-Link integration pins a different python-kasa version.
