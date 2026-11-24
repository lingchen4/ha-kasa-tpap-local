# Kasa TPAP Local

A Home Assistant custom integration that controls TP-Link Kasa (and Tapo)
plugs, wall switches, dimmers and power strips over the newer **TPAP**
login, locally, with no cloud. It was built for an HS200 (US, HW 5.26,
firmware 1.4.0 Build 260910) that the official TP-Link integration can no
longer talk to (python-kasa issue
[#1847](https://github.com/python-kasa/python-kasa/issues/1847)).

- **Local only.** It talks HTTP to the device's IP; no TP-Link cloud, SmartThings or bridge.
- **Finds devices for you.**
  - DHCP discovery notices TPAP devices as they join the network. It also follows a configured device that went offline and came back at a new IP, after verifying it with a real login.
  - A **Scan my network** button lists TPAP devices on your LAN.
  - You can still enter an IP address manually.
- **Remembers your TP-Link account.** Add the next device by choosing it and clicking Submit.
- **Entities by device type:** see [Supported devices](#supported-devices). They work with HomeKit Bridge like any other entity.
- **Runs next to the official TP-Link integration** without changing it or its `python-kasa` package.
- Security notes: [SECURITY.md](SECURITY.md). Dependencies and upstream code: [DEPENDENCIES.md](DEPENDENCIES.md).

> **Status.** The protocol code is tested against a simulated device that
> runs the real SPAKE2+ exchange (141 automated tests). It has **not yet
> been run against physical hardware** by the author. Use the
> [smoke test](#manual-smoke-test-on-the-real-hs200) first.

## Supported devices

Devices are recognised by the components they report (`component_nego`),
not by model name. Any TPAP device using the SMART request protocol with
these components works.

| Kind | Examples | Entities |
|---|---|---|
| Wall switch, plug | HS200, KS205, P100, P115 | `switch` |
| Dimmer (`brightness`) | KS225, HS220 | `light` with brightness |
| Energy-monitoring plug (`energy_monitoring`) | KP125M, EP25, P110 | `switch`, plus *Current power* (W) and *Today's energy* (kWh) sensors |
| Power strip (`control_child`) | EP40M, P300 | one `switch` per outlet, each its own sub-device |

Not supported: bulbs and colour lights, cameras, hubs, robot vacuums, legacy
IOT-protocol Kasa devices, and TPAP over TLS. Devices still on KLAP/AES stay
with the official TP-Link integration.

The examples are models python-kasa runs over the SMART protocol with these
components (HS200 and HS220 also exist as older IOT-protocol hardware, which
is not supported). Whether a given model's firmware uses TPAP varies. Only
the HS200 above is known to.

## How it works

```
Home Assistant Core
 ├─ tplink (official) ── python-kasa 0.12.0 (KLAP/AES) ──► other Kasa devices  [unchanged]
 └─ kasa_tpap (this)
     discovery    DHCP (TP-Link MAC) or "Scan my network" ─► POST http://IP/ login/discover
                  (no credentials; only TPAP answers are offered)
     config_flow  pick device ─► account (saved one pre-filled) ─► real TPAP login
                  ─► component_nego ─► entry (unique id = MAC)
     coordinator  poll every 30 s: get_device_info (+ get_energy_usage, get_child_device_list)
                  back-off up to 5 min when unreachable
     entities     switch / light / sensor / outlet switches ─► command ─► read back state
     client       one request at a time per device:
         POST /           login/discover        → tls 0, port 80, pake [2]
         POST /           login/pake_register   → salt, iterations, device share
         (SPAKE2+ in an executor thread)
         POST /           login/pake_share      → verify dev_confirm, session id
         POST /stok=…/ds  AES-128-CCM requests
```

The TPAP code is a reviewed subset of the open python-kasa pull request
[#1592](https://github.com/python-kasa/python-kasa/pull/1592), pinned to commit
`f4dff39`. It is vendored under this integration's own namespace
(`custom_components/kasa_tpap/vendor/tpap`), so it never replaces the `kasa`
package Home Assistant's TP-Link integration uses. See
[UPSTREAM.md](custom_components/kasa_tpap/vendor/tpap/UPSTREAM.md).

## Requirements

- Home Assistant 2026.10 or newer (Home Assistant OS on a Raspberry Pi is fine).
- The device on the same LAN as Home Assistant, at a private IPv4/IPv6 address.
  A DHCP reservation is still recommended. DHCP discovery can follow an
  address change, but only for an unreachable device whose DHCP hostname or
  MAC matches TP-Link's patterns, and only after a verified login.
- The TP-Link account that owns the device.
- **Third-Party Compatibility turned off** for the device in the TP-Link
  (Kasa/Tapo) app. With it on, the device offers only an AES login that
  firmware 1.4.0 rejects (error 1003). With it off, it offers TPAP.

## Before you install

1. In the TP-Link app, open the device → Settings → **Third-Party
   Compatibility** → off. Wait about a minute.
2. Optional: do the [smoke test](#manual-smoke-test-on-the-real-hs200) from a
   computer on the same network.
3. If the official TP-Link integration still lists the device, open it
   (**Settings → Devices & services → TP-Link Smart Home → the device**) and
   choose **⋮ → Disable**. Disable only that device, not the integration
   entry, so your other Kasa devices keep working.

## Installation

### Option A: manual (ZIP)

1. Download `kasa_tpap-<version>.zip` and its `.sha256` file. Check the
   checksum: `shasum -a 256 -c kasa_tpap-<version>.zip.sha256`.
2. Install the **Samba share** add-on (Settings → Add-ons → Add-on store),
   start it, and open the `config` share from your computer
   (`smb://homeassistant.local/config` or `\\homeassistant.local\config`).
3. Create the folder `custom_components` if it does not exist, unzip the
   archive, and copy the `kasa_tpap` folder into it:
   `config/custom_components/kasa_tpap/manifest.json` must exist.
4. Restart Home Assistant (**Settings → System → ⏻ → Restart Home Assistant**).
   On first start Home Assistant installs `ecdsa==0.19.2` from PyPI.

The Studio Code Server or File editor add-ons also work for copying files. No
SSH access into the Home Assistant Core container is needed.

### Option B: HACS (custom repository)

1. Publish this repository on GitHub with a release that has
   `kasa_tpap.zip` attached (built by `scripts/build_zip.sh`, see `hacs.json`).
2. HACS → ⋮ → **Custom repositories** → add the repository URL, type
   *Integration*.
3. Install **Kasa TPAP Local**, then restart Home Assistant.

## Adding devices

### Discovered automatically

After a restart, or when a device reconnects to Wi-Fi, Home Assistant may
show **Discovered: Kasa TPAP Local (192.168.50.65)** under **Settings →
Devices & services**. Click **Add**, enter (or confirm) the TP-Link account,
and submit.

### Scan or enter manually

1. **Settings → Devices & services → + Add integration → Kasa TPAP Local**.
2. Choose:
   - **Scan my network for devices**. A progress indicator shows while it
     runs (a few seconds on a /24, up to a minute at most). It lists
     unconfigured TPAP devices as `IP (MAC)`. Pick one.
   - **Enter the IP address manually**, e.g. `192.168.50.65`.
3. Fill in the **TP-Link account email** and **password**. If another
   Kasa TPAP device is already configured, its email is pre-filled; leave the
   password empty to reuse the saved one.
4. Optional: a **Display name** (empty uses the device's own name) and a
   **Polling interval** (default 30 s, 10–300 s).
5. Submit. The integration logs in to the device for real before it creates
   the entry, so any problem shows up right away (see
   [Troubleshooting](#troubleshooting)).

For the HS200 named *porch* you get one device with `switch.porch`. Power
strips get `switch.<strip>_<outlet>`. Energy plugs also get
`sensor.<name>_current_power` and `sensor.<name>_today_s_energy`.

To change the IP or account later, open the entry's **⋮ → Reconfigure**;
leave the password empty to keep it. To change the polling interval, use
**Configure**. If the TP-Link password changes, Home Assistant asks you to
re-authenticate once; other devices using the same account are updated too.

## HomeKit

The entities are ordinary switches, lights and sensors. If your HomeKit
Bridge uses an include filter, add the new entities (or the `kasa_tpap`
domain). Apple Home shows them as new accessories; reassign rooms and
automations there.

## Manual smoke test on the real HS200

From a computer on the same LAN, with this repository and its development
environment (see [Development](#development)):

```bash
.venv/bin/python scripts/smoke_test.py --host 192.168.50.65 --toggle
```

Enter the TP-Link email and password when asked (the password is not echoed
or stored). Expected output:

```
OK   model=HS200 hw=5.26
     firmware=1.4.0 Build 260910 Rel.183540
     name='porch' rssi=-32 on=False
OK   switched to True (expected True)
OK   restored to False
```

`--toggle` switches the porch light briefly on and off. Leave it out for a
read-only test. Add `--debug` to see the protocol steps; credentials, session
id and payloads are never printed.

Then check in Home Assistant:

| # | Step | Expected |
|---|---|---|
| 1 | **Add integration → Scan my network** | The HS200 is listed as `192.168.50.65 (…)` |
| 2 | Pick it, enter the account | Entry created; device page shows HS200, HW 5.26, firmware 1.4.0 |
| 3 | Toggle the switch in the UI | Light follows within about 1 s; state matches |
| 4 | Press the physical switch | HA shows the new state within one polling interval |
| 5 | Unplug or cut Wi-Fi to the device | Entity becomes *Unavailable* within about 1 minute |
| 6 | Restore it | Entity recovers automatically (back-off is at most 5 minutes) |
| 7 | Block the device's internet access in the router; repeat steps 3–4 | Still works (local only) |
| 8 | Toggle from Apple Home through HomeKit Bridge | Light follows |
| 9 | Check other Kasa devices in the official TP-Link integration | Unaffected |
| 10 | Download diagnostics (device page → ⋮ → Download diagnostics) | No email, password, IP, MAC or name in the file |
| 11 | Check **Settings → System → Logs** with debug enabled | No credentials, session ids or payloads |

## Troubleshooting

Enable debug logging (no secrets are logged):

```yaml
logger:
  logs:
    custom_components.kasa_tpap: debug
```

| Message | Meaning and fix |
|---|---|
| *No unconfigured TPAP device answered* (scan) | Third-Party Compatibility may still be on, the device may be on another subnet or VLAN than Home Assistant, or it is already configured. Add it by IP instead. |
| *did not answer the TPAP discovery request… Third-Party Compatibility* | The device does not speak TPAP right now. Turn Third-Party Compatibility **off** in the TP-Link app, wait a minute, retry. |
| *rejected the TP-Link email or password* | Use the account that owns the device (the one in the TP-Link app). A phone-number account may need `<number>@tplink.id`. Each wrong attempt counts toward the device's lockout, so the integration stops trying until you re-authenticate. |
| *Enter the password of this TP-Link account* | No saved password exists for that email; type it once. |
| *temporarily locked logins* | Too many wrong passwords. Wait (usually up to 30 minutes); the integration does not retry during the lockout. |
| *did not answer in time* / *unreachable* | Check power, Wi-Fi and the IP (router DHCP list). |
| *Enter a private LAN IP address* | Only literal IPs in 10/8, 172.16/12, 192.168/16 or fc00::/7 are accepted. |
| *asks for a mode this integration does not support* | TPAP over TLS, a login without the account password, or a legacy IOT-protocol device. Please report it with the debug log. |
| *failed to prove that it knows your credentials* | The PAKE confirmation did not match: the IP may belong to another device or something is intercepting traffic. The connection is refused on purpose. |
| Entity *Unavailable* | The device was unreachable at the last poll (or a strip stopped reporting that outlet); it recovers automatically. |

## Uninstall and rollback

1. **Settings → Devices & services → Kasa TPAP Local → ⋮ → Delete** for each
   entry. This removes the device, its entities and the stored credentials.
   Also delete any *Ignored* Kasa TPAP discoveries if you want them gone.
2. Delete the folder `config/custom_components/kasa_tpap` (Samba or File
   editor) and restart Home Assistant.
3. Optional: `ecdsa` stays installed in Home Assistant's Python environment
   until the next Home Assistant update reinstalls dependencies. It is inert
   if unused.
4. To go back to the official integration, re-enable the device in the
   TP-Link integration and turn Third-Party Compatibility back on if you need
   it. The official integration cannot control this firmware until python-kasa
   ships TPAP support.
5. Remove the entities from the HomeKit Bridge include filter if you added them.

Nothing else is changed by this integration, so no further cleanup is needed.

## Development

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python \
  "pytest-homeassistant-custom-component==0.13.375" "ecdsa==0.19.2" ruff mypy
.venv/bin/python -m pytest              # 141 tests
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy custom_components        # strict
scripts/build_zip.sh                    # dist/kasa_tpap-<version>.zip, dist/kasa_tpap.zip + .sha256
```

`tests/device_simulator.py` is a TPAP device served over real HTTP that runs
the device side of SPAKE2+ and simulates switches, dimmers, energy plugs and
power strips. Client tests therefore exercise the real handshake,
encryption, redirects, timeouts, session expiry, child paging and discovery
probes.

## License

GPL-3.0-or-later, because it includes code derived from python-kasa. See
[LICENSE](LICENSE) and [DEPENDENCIES.md](DEPENDENCIES.md).
