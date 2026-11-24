# Security

This document explains what Kasa TPAP Local does on your network, what it
protects, and what it does not.

## Trust model in one paragraph

Home Assistant custom integrations are **not sandboxed**. This code runs
inside the Home Assistant Core process with the same rights as Core itself:
it could read every file under `/config`, every other integration's
credentials, and reach any network. You are trusting this code (and its one
added dependency, `ecdsa`) as much as Home Assistant itself. Read it before
installing; it is about 1,000 lines plus the vendored `crypto.py`.

## What the integration does, and does not do

| | |
|---|---|
| Network destinations | At runtime, only the configured device IPs, on port 80 or the TPAP port the device announces. During discovery (see below), port 80 of LAN addresses. |
| Cloud | None. No TP-Link cloud, SmartThings, bridge servers, telemetry, analytics, update checks or crash reporting. |
| Discovery | No UDP broadcast or mDNS. Discovery only sends the credential-free HTTP `login`/`discover` request: to a single address reported by Home Assistant's DHCP watcher for a TP-Link MAC prefix, or, if you choose **Scan my network**, to every address of Home Assistant's own LAN subnets. |
| Code loading | No downloads at runtime and no `eval`/`exec` or plugins. `ecdsa==0.19.2` is installed once by Home Assistant from PyPI when the integration first loads. |
| Files | None read or written; configuration goes through Home Assistant's config-entry API. |
| Other integrations | Not accessed or modified. The official TP-Link integration is not touched or monkey-patched, and its `kasa` package is never imported. |
| Shell commands | None. |
| HTTP endpoints exposed | None. |

## Address restrictions

`network.py` accepts only literal IP addresses in an allowlist of LAN
ranges: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16` and `fc00::/7`. It
deliberately does not use Python's `is_private`, which also accepts
documentation, benchmarking and IETF ranges and IPv6 transition addresses
(6to4, Teredo) that embed public IPv4 addresses. It refuses:

- host names, because DNS could later point a name elsewhere (DNS rebinding);
- everything outside the allowlist (public, loopback, link-local, multicast,
  CGNAT, 6to4, Teredo and so on);
- scoped IPv6 addresses.

The same check applies to manual entry, DHCP discovery, the network scan
and reconfiguration.

The client builds every URL from the validated address and checks the host
again before each request. HTTP redirects are never followed: any 3xx answer
is an error, so the device (or something impersonating it) cannot send the
login elsewhere. Response bodies are capped at 256 KiB.

## Discovery traffic

| Mechanism | When | What is sent |
|---|---|---|
| DHCP | Home Assistant's DHCP watcher sees a TP-Link MAC prefix (the list the official TP-Link integration uses) | For an unconfigured device, one HTTP `discover` request to that address. For a configured device, nothing, unless the move rules below apply |
| Scan | Only when you pick **Scan my network** in the setup dialog | One HTTP `discover` request to port 80 of each address in Home Assistant's enabled IPv4 LAN subnets. Wider subnets are narrowed to the /24 around Home Assistant, so at most 1,024 addresses are probed, 32 at a time, with a 2 s timeout |

The probe carries no credentials and nothing identifying beyond Home
Assistant's IP. Other LAN hosts may log a stray `POST /`. Only addresses that
answer with a supported TPAP login are offered. Non-TPAP TP-Link devices
(KLAP or AES, handled by the official integration) are ignored.

**Automatic address updates.** DHCP data and the discover answer are
unauthenticated, so they are not trusted to move a configured device. The
stored address changes only when all of these hold:

1. the device is currently unreachable at its stored address (a working
   device is never moved);
2. no other move was tried for that device in the last hour;
3. a full login at the new address, including the PAKE confirmation,
   succeeds and reports the same MAC.

Someone on your LAN spoofing DHCP therefore gets at most one online password
guess per hour, and only while the real device is offline. A device that
fails the PAKE confirmation is never retried: the entity becomes unavailable
and logs that the integration must be reconfigured.

## Credentials

- **What is stored.** The TP-Link account email and password, in the config
  entry. Home Assistant writes config entries to
  `/config/.storage/core.config_entries` as **plain-text JSON**. They are
  **not encrypted at rest** by Home Assistant. Anyone with access to the
  `/config` directory, an add-on with `/config` access, SSH/Samba access, or
  an unencrypted backup can read them.
  Home Assistant's own backups can be encrypted. Keep backup encryption on
  and the key safe.
- **Reuse across devices.** When you add another device, the form
  pre-fills the email of an account already stored by this integration.
  Leaving the password empty reuses that account's stored password, skipping
  entries whose credentials are known to be wrong (reauthentication pending
  or setup error). The password is never sent to the browser. When
  reauthentication changes the password (same email), each other entry with
  that email and the old password is first logged in with the new one, and
  only those that accept it are updated. Only this integration's own config
  entries are read.
- **Why the account password.** TPAP's `userpw` login is keyed to the
  TP-Link account that owns the device; there is no device-local password.
  The same password protects your TP-Link cloud account. Use a unique,
  strong password and enable two-step verification for the TP-Link account.
- **On the wire.** The password is never sent. SPAKE2+ (a password-
  authenticated key exchange) proves knowledge of it. A passive observer
  learns nothing that allows an offline guess. An active attacker posing as
  the device gets at most one online guess per login attempt, and the device
  locks out after repeated failures.
- **In logs and errors.** The email, password, session id (`stok`), keys,
  and request and response payloads are never logged or included in
  exception messages. aiohttp error texts, which can contain the URL and
  therefore the session id, are replaced. Tests assert this
  (`test_no_secrets_in_logs_or_errors`).
- **Diagnostics.** Host, email, password, MAC, device id, name, title and
  unique id are redacted (`test_diagnostics_are_redacted`).

## Protocol security properties (TPAP over plain HTTP)

The target HS200 announces `tls: 0`, so TPAP runs over plain HTTP. What that
gives you:

| Property | Status |
|---|---|
| Password confidentiality | Yes (PAKE). |
| Device authentication | Partial. The device must return a valid `dev_confirm`, proving it holds the verifier derived from your password; this is checked in constant time before any data is trusted. Anyone else holding that verifier (for example TP-Link's cloud or a compromised device) could impersonate it. Device certificate attestation (DAC) is not used by this device in this mode. |
| Request/response confidentiality and integrity | Yes. AES-128-CCM with a key from the PAKE; each message is bound to its sequence number. |
| Replay of device replies | Refused. A reply's sequence number must be at least its request's and higher than any reply accepted before. Upstream accepts any number, which would allow replaying an older reply. |
| Unencrypted replies | Never trusted as data. They are only read as error codes. A forged "session expired" error can force a fresh login (denial of service, no data exposure). |
| Metadata | Visible to anyone on the LAN: that HA talks to the device, when, the URL path including the session id, and message sizes. |
| Denial of service | Anyone on the LAN can drop or delay traffic, or lock the device out by sending wrong logins. That is inherent to the device. |
| Downgrade | None. Only TLS mode 0 with `userpw` is implemented, and the device must pick the exact suite and cipher offered. Anything else is refused rather than falling back to AES/KLAP or another login. |
| Lockout safety | A refused `pake_share` is never retried, whatever the error code, and neither is a `pake_share` whose answer was lost. Once credentials are rejected, the client sends no further login (polling or commands) until you reauthenticate. A reported lockout stops all logins until it expires; an unauthenticated lockout time is capped at 24 h. |

Cryptographic primitives come from `cryptography` (HKDF, HMAC, CMAC,
AES-CCM), `hashlib` (PBKDF2, SHA-x, MD5/SHA-1 where the protocol mandates
them) and `ecdsa` (curve arithmetic). Randomness comes from `secrets`. The
CPU-heavy PBKDF2/SPAKE2+ step runs in an executor thread, not on the event
loop.

### Known limitations

- `ecdsa` is pure Python and not constant-time (see `DEPENDENCIES.md`).
- The protocol mandates some weak legacy encodings of the password inside
  the PAKE, such as unsalted SHA-1 when the device asks for
  `password_shadow` id 2. They are only used as PAKE input, never sent.
- TPAP over TLS, DAC attestation, UDP discovery and the device-owner check
  that relies on UDP discovery are not implemented, nor are legacy
  IOT-protocol devices, bulbs and colour lights.
- The protocol is reverse-engineered and has had no formal review. The
  vendored code comes from an open, unmerged pull request.

## Reporting a vulnerability

Please do not open a public issue for security problems. Contact the
maintainer privately. Include the Home Assistant version, the integration
version, and redacted logs (enable debug logging only for
`custom_components.kasa_tpap`).
