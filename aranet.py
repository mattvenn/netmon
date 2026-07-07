"""Aranet4 environment probe (temperature / humidity / CO2).

Not a network probe — it talks to an Aranet4 CO2 monitor over Bluetooth LE using
the `aranet4` pip library. Kept separate from probes.py, which is stdlib-only.

On macOS the Bluetooth "address" is a CoreBluetooth UUID, not a MAC; that UUID is
exactly what get_current_readings() expects, so `python aranet.py scan` prints the
value to drop into config.json.
"""

import asyncio
import json
import os
import sys
import threading

BASE = os.path.dirname(os.path.abspath(__file__))

# One long-lived event loop for all BLE work. bleak's BlueZ backend keeps a single
# D-Bus connection per event loop; a fresh asyncio.run() per read (across new loops)
# opens a new system-bus connection each time and leaks it, until dbus refuses the
# user's 257th connection (max_connections_per_user=256) and every BLE call fails.
_ble_loop = None
_ble_lock = threading.Lock()


def _ble_run(coro, timeout):
    global _ble_loop
    with _ble_lock:
        if _ble_loop is None:
            _ble_loop = asyncio.new_event_loop()
            threading.Thread(target=_ble_loop.run_forever, daemon=True,
                             name="aranet-ble").start()
    return asyncio.run_coroutine_threadsafe(coro, _ble_loop).result(timeout)


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


def read_advertisement(mac, timeout=15):
    """Current values read passively from the Aranet's BLE *advertisement* — no GATT
    connection. Needs the sensor's "Smart Home integrations" broadcast enabled (it's
    what carries the measurements in the advert). Far more reliable than read() on a
    weak link (e.g. a Pi's onboard BT) and has no single-connection contention, so
    several hosts can read at once. Same return shape as read(). history() still needs
    the connection-based path."""
    if not mac:
        return {"ok": False, "error": "no-mac-configured"}
    try:
        return _ble_run(_read_advertisement(mac, timeout), timeout + 10)
    except Exception as e:
        # BLE/adapter errors (e.g. Bluetooth powered off) must be a recorded failure,
        # not an exception that bubbles up and aborts the whole collector cycle.
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:150]}


async def _read_advertisement(mac, timeout):
    import asyncio
    from aranet4.client import Aranet4Advertisement
    from bleak import BleakScanner
    target = mac.lower()
    hit = {}

    def cb(device, ad_data):
        if device.address.lower() != target or "adv" in hit:
            return
        adv = Aranet4Advertisement(device, ad_data)
        if adv.readings and adv.readings.co2 not in (None, -1):
            hit["adv"] = adv

    scanner = BleakScanner(detection_callback=cb)
    await scanner.start()
    try:
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        while "adv" not in hit and loop.time() - t0 < timeout:
            await asyncio.sleep(0.2)
    finally:
        await scanner.stop()
    adv = hit.get("adv")
    if not adv:
        return {"ok": False, "error": "no-advert-reading"}  # broadcast off or out of range
    r = adv.readings
    return {"ok": True, "name": r.name or None, "temp_c": r.temperature,
            "humidity": r.humidity, "co2": r.co2, "pressure": r.pressure,
            "battery": r.battery, "interval": r.interval, "ago": r.ago}


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
    return _ble_run(_history(mac, since_ts), 120)


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
    elif cmd == "advert":
        mac = sys.argv[2] if len(sys.argv) > 2 else _configured_mac()
        print(json.dumps(read_advertisement(mac), indent=2))
    elif cmd == "history":
        mac = sys.argv[2] if len(sys.argv) > 2 else _configured_mac()
        recs = history(mac)
        print(f"{len(recs)} records")
        for r in recs[-10:]:
            print(r)
    else:
        print(__doc__)
        print("usage: python aranet.py [scan | read [MAC] | history [MAC]]")
