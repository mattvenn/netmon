"""Network probe functions. Stdlib only, portable across macOS and Linux (Raspberry Pi)."""

import json
import platform
import random
import re
import socket
import struct
import subprocess
import time
import urllib.request

IS_MAC = platform.system() == "Darwin"

DNS_TEST_NAME = "google.com"
HTTP_CHECK_URL = "https://www.gstatic.com/generate_204"
SPEED_DOWN_URL = "https://speed.cloudflare.com/__down?bytes=50000000"
SPEED_UP_URL = "https://speed.cloudflare.com/__up"
SPEED_TIME_CAP = 12.0  # seconds per direction
SPEED_UP_BYTES = 3_000_000


def get_default_gateway():
    try:
        if IS_MAC:
            out = subprocess.run(["route", "-n", "get", "default"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"gateway:\s*([\d.]+)", out)
        else:
            out = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"default via ([\d.]+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def ping(host, count=5, timeout_s=2):
    """Returns {ok, loss_pct, min_ms, avg_ms, max_ms}."""
    if IS_MAC:
        cmd = ["ping", "-c", str(count), "-i", "0.2", "-W", str(timeout_s * 1000), host]
    else:
        cmd = ["ping", "-c", str(count), "-i", "0.2", "-W", str(timeout_s), host]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=count * timeout_s + 10).stdout
    except Exception:
        return {"ok": False, "loss_pct": 100.0}
    m = re.search(r"(\d+) packets transmitted, (\d+)(?: packets)? received", out)
    if not m:
        return {"ok": False, "loss_pct": 100.0}
    sent, recv = int(m.group(1)), int(m.group(2))
    loss = 100.0 * (sent - recv) / sent if sent else 100.0
    res = {"ok": recv > 0, "loss_pct": round(loss, 1)}
    m = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)", out)
    if m:
        res.update(min_ms=float(m.group(1)), avg_ms=float(m.group(2)), max_ms=float(m.group(3)))
    return res


def dns_query(server, name=DNS_TEST_NAME, timeout_s=2.0):
    """Raw UDP DNS A query to a specific server. Returns {ok, ms, rcode, answers}."""
    qid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    packet = header + qname + struct.pack(">HH", 1, 1)  # A, IN
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout_s)
    t0 = time.monotonic()
    try:
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(2048)
        ms = (time.monotonic() - t0) * 1000
        rid, flags, _, ancount = struct.unpack(">HHHH", data[:8])
        if rid != qid:
            return {"ok": False, "error": "bad-id"}
        rcode = flags & 0x000F
        return {"ok": rcode == 0, "ms": round(ms, 1), "rcode": rcode, "answers": ancount}
    except socket.timeout:
        return {"ok": False, "error": "timeout"}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    finally:
        sock.close()


def dns_system(name=DNS_TEST_NAME):
    """Resolve via the system resolver (whatever the OS is configured to use)."""
    t0 = time.monotonic()
    try:
        socket.getaddrinfo(name, None, socket.AF_INET)
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000, 1)}
    except socket.gaierror as e:
        return {"ok": False, "error": str(e)}


def http_check(url=HTTP_CHECK_URL, timeout_s=5.0):
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netmon"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            r.read()
            return {"ok": r.status in (200, 204),
                    "ms": round((time.monotonic() - t0) * 1000, 1), "status": r.status}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def wifi_info():
    return _wifi_mac() if IS_MAC else _wifi_linux()


def _wifi_mac():
    try:
        out = subprocess.run(["system_profiler", "SPAirPortDataType", "-json"],
                             capture_output=True, text=True, timeout=20).stdout
        data = json.loads(out)["SPAirPortDataType"][0]["spairport_airport_interfaces"]
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
    for iface in data:
        if iface.get("spairport_status_information") != "spairport_status_connected":
            continue
        cur = iface.get("spairport_current_network_information", {})
        res = {"ok": True, "iface": iface.get("_name")}
        sn = cur.get("spairport_signal_noise", "")
        m = re.match(r"(-?\d+) dBm / (-?\d+) dBm", sn)
        if m:
            res["rssi_dbm"] = int(m.group(1))
            res["noise_dbm"] = int(m.group(2))
            res["snr_db"] = res["rssi_dbm"] - res["noise_dbm"]
        m = re.match(r"(\d+)\s*\((\S+),\s*(\S+)\)", cur.get("spairport_network_channel", ""))
        if m:
            res["channel"] = int(m.group(1))
            res["band"] = m.group(2)
            res["width"] = m.group(3)
        res["rate_mbps"] = cur.get("spairport_network_rate")
        res["phymode"] = cur.get("spairport_network_phymode")
        return res
    return {"ok": False, "error": "not-connected"}


def _wifi_linux():
    import glob
    ifaces = [p.split("/")[-2] for p in glob.glob("/sys/class/net/*/wireless")]
    if not ifaces:
        return {"ok": False, "error": "no-wifi-iface"}
    iface = ifaces[0]
    try:
        out = subprocess.run(["iw", "dev", iface, "link"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
    if "Not connected" in out or not out.strip():
        return {"ok": False, "error": "not-connected", "iface": iface}
    res = {"ok": True, "iface": iface}
    m = re.search(r"signal:\s*(-?\d+) dBm", out)
    if m:
        res["rssi_dbm"] = int(m.group(1))
    m = re.search(r"freq:\s*(\d+)", out)
    if m:
        freq = int(m.group(1))
        res["band"] = "2GHz" if freq < 3000 else ("5GHz" if freq < 5900 else "6GHz")
    m = re.search(r"tx bitrate:\s*([\d.]+) MBit/s", out)
    if m:
        res["rate_mbps"] = float(m.group(1))
    return res


def speed_download(url=SPEED_DOWN_URL, time_cap=SPEED_TIME_CAP):
    """Timed chunked download; stops at time_cap so slow links still finish."""
    t0 = time.monotonic()
    total = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "netmon"})
        with urllib.request.urlopen(req, timeout=10) as r:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if time.monotonic() - t0 > time_cap:
                    break
    except Exception as e:
        if total == 0:
            return {"ok": False, "error": type(e).__name__}
    elapsed = time.monotonic() - t0
    if elapsed <= 0 or total == 0:
        return {"ok": False, "error": "no-data"}
    return {"ok": True, "mbps": round(total * 8 / elapsed / 1e6, 2),
            "bytes": total, "secs": round(elapsed, 1)}


def speed_upload(url=SPEED_UP_URL, nbytes=SPEED_UP_BYTES):
    payload = random.randbytes(nbytes)
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"User-Agent": "netmon",
                                              "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=45) as r:
            r.read()
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}
    elapsed = time.monotonic() - t0
    return {"ok": True, "mbps": round(nbytes * 8 / elapsed / 1e6, 2),
            "bytes": nbytes, "secs": round(elapsed, 1)}
