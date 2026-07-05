"""Aranet4 environment probe (temperature / humidity / CO2).

Not a network probe — it talks to an Aranet4 CO2 monitor over Bluetooth LE using
the `aranet4` pip library. Kept separate from probes.py, which is stdlib-only.

On macOS the Bluetooth "address" is a CoreBluetooth UUID, not a MAC; that UUID is
exactly what get_current_readings() expects, so `python aranet.py scan` prints the
value to drop into config.json.
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def read(mac):
    """Current reading. Returns {ok, temp_c, humidity, co2, pressure, battery, ...}."""
    if not mac:
        return {"ok": False, "error": "no-mac-configured"}
    try:
        from aranet4 import client
        c = client.get_current_readings(mac)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:150]}
    # co2 == -1 while the sensor is still warming up after a battery change
    if c is None or c.co2 in (None, -1):
        return {"ok": False, "error": "no-reading"}
    return {"ok": True, "name": c.name or None, "temp_c": c.temperature,
            "humidity": c.humidity, "co2": c.co2, "pressure": c.pressure,
            "battery": c.battery, "interval": c.interval, "ago": c.ago}


def history(mac, since_ts=None):
    """Stored on-device log as [{ts, temp_c, humidity, co2, pressure}], oldest first.
    Only entries strictly newer than since_ts are returned, so callers can fill the
    gap since the last recorded sample without duplicating rows.

    We drive the low-level GATT record read directly rather than the library's
    get_all_records(): aranet4 2.6.0 has model branches for Aranet2/radiation/radon
    but none for the flagship Aranet4, so get_all_records() flags it "unknown model"
    and returns an empty log. The underlying get_records() works fine."""
    if not mac:
        return []
    import asyncio
    return asyncio.run(_history(mac, since_ts))


async def _history(mac, since_ts):
    import datetime as dt
    from aranet4.client import Aranet4, Param, _log_times
    m = Aranet4(address=mac)
    await m.connect()
    try:
        interval = await m.get_interval()
        ago = await m.get_seconds_since_update()
        log_size = await m.get_total_readings()
        if not log_size:
            return []
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        times = _log_times(now, log_size, interval, ago)
        get = lambda p: m.get_records(p, log_size=log_size, start=1, end=log_size)
        co2 = await get(Param.CO2)
        temp = await get(Param.TEMPERATURE)
        humi = await get(Param.HUMIDITY)
        pres = await get(Param.PRESSURE)
    finally:
        try:
            await m.device.disconnect()
        except Exception:
            pass
    out = []
    for t, c, tp, h, p in zip(times, co2, temp, humi, pres):
        ts = t.timestamp()
        if since_ts and ts <= since_ts:
            continue
        if c in (None, -1):
            continue
        out.append({"ts": ts, "temp_c": tp, "humidity": h, "co2": c, "pressure": p})
    return out


def scan(duration=8):
    """Discover nearby Aranet4 devices. Returns [(address, name)]."""
    import asyncio
    from bleak import BleakScanner

    async def go():
        devs = await BleakScanner.discover(timeout=duration)
        return [(d.address, d.name) for d in devs
                if (d.name or "").lower().startswith("aranet")]
    return asyncio.run(go())


def _configured_mac():
    try:
        with open(os.path.join(BASE, "config.json")) as f:
            return (json.load(f).get("aranet") or {}).get("mac")
    except (FileNotFoundError, ValueError):
        return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "read"
    if cmd == "scan":
        found = scan()
        if not found:
            print("no Aranet4 devices found (is it advertising / in range?)")
        for addr, name in found:
            print(f"{addr}  {name}")
    elif cmd == "read":
        mac = sys.argv[2] if len(sys.argv) > 2 else _configured_mac()
        print(json.dumps(read(mac), indent=2))
    elif cmd == "history":
        mac = sys.argv[2] if len(sys.argv) > 2 else _configured_mac()
        recs = history(mac)
        print(f"{len(recs)} records")
        for r in recs[-10:]:
            print(r)
    else:
        print(__doc__)
        print("usage: python aranet.py [scan | read [MAC] | history [MAC]]")
