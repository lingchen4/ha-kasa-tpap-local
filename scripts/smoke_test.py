"""Manual smoke test of the TPAP client against a real device.

Run from a computer on the same LAN as the device, with the development
virtualenv (see README, "Development"):

    .venv/bin/python scripts/smoke_test.py --host 192.168.50.65 [--toggle]

The TP-Link email and password are prompted for (the password is not echoed)
and are only sent to the device, through the PAKE login. Nothing is stored.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.kasa_tpap.client import TpapClient
from custom_components.kasa_tpap.exceptions import TpapError


async def _run(host: str, username: str, password: str, toggle: bool) -> int:
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(force_close=True),
        cookie_jar=aiohttp.DummyCookieJar(),
    ) as session:
        client = TpapClient(host, username, password, session)
        try:
            info = await client.async_get_device_info()
            print(f"OK   model={info.model} hw={info.hardware_version}")
            print(f"     firmware={info.firmware_version}")
            print(f"     name={info.nickname!r} rssi={info.rssi} on={info.is_on}")
            if toggle:
                original = info.is_on
                await client.async_set_device_on(not original)
                after = (await client.async_get_device_info()).is_on
                print(f"OK   switched to {after} (expected {not original})")
                await asyncio.sleep(2)
                await client.async_set_device_on(original)
                restored = (await client.async_get_device_info()).is_on
                print(f"OK   restored to {restored}")
                if after == original or restored != original:
                    print("FAIL state did not follow the commands")
                    return 1
        except TpapError as err:
            print(f"FAIL {type(err).__name__}: {err}")
            return 1
        finally:
            await client.async_close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True, help="device IP address")
    parser.add_argument("--toggle", action="store_true", help="switch off/on")
    parser.add_argument("--debug", action="store_true", help="protocol debug log")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    if args.debug:
        logging.getLogger("custom_components.kasa_tpap").setLevel(logging.DEBUG)
    username = input("TP-Link account email: ").strip()
    password = getpass.getpass("TP-Link account password: ")
    return asyncio.run(_run(args.host, username, password, args.toggle))


if __name__ == "__main__":
    sys.exit(main())
