#!/usr/bin/env python3
"""netmon — home network monitor.

Commands:
  probe       run one measurement cycle, print results (and record them)
  speedtest   run a bandwidth test, print results (and record them)
  run         run forever: collector loop + web dashboard
"""

import argparse
import json
import os
import re
import socket
import threading
import time

import db
import probes

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = "netmon.db"
DEFAULT_PORT = 8737
DEFAULT_INTERVAL = 30
DEFAULT_SPEED_INTERVAL = 15 * 60

def load_config():
    """Optional machine-specific config.json next to this file.
    Keys: lan_targets — {name: ip} of LAN devices to ping each cycle (e.g. an extender)."""
    try:
        with open(os.path.join(BASE, "config.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError as e:
        print(f"[config] config.json invalid, ignoring: {e}", flush=True)
        return {}


CONFIG = load_config()


def _stable_source():
    """A source name that doesn't drift. config.json wins; else the OS's *fixed*
    hostname (macOS gethostname() changes with network state, so avoid it there)."""
    if CONFIG.get("source"):
        return CONFIG["source"]
    if os.uname().sysname == "Darwin":
        try:
            import subprocess
            name = subprocess.run(["scutil", "--get", "LocalHostName"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
            if name:
                return name
        except Exception:
            pass
    return socket.gethostname().split(".")[0]


SOURCE = _stable_source()


def run_cycle(conn, source=SOURCE):
    """One measurement cycle. Returns the samples it recorded."""
    recorded = []

    def rec(probe, target, res, value_key):
        ok = res.get("ok", False)
        value = res.get(value_key) if ok else None
        detail = {k: v for k, v in res.items() if k not in ("ok", value_key)}
        db.insert(conn, source, probe, target, ok, value, detail or None)
        recorded.append({"probe": probe, "target": target, "ok": ok,
                         "value": value, "detail": detail})

    wifi = probes.wifi_info()
    rec("wifi", wifi.get("iface", "?"), wifi, "rssi_dbm")

    gw = probes.get_default_gateway()
    if gw:
        rec("gateway_ping", gw, probes.ping(gw, count=5), "avg_ms")
    else:
        rec("gateway_ping", "none", {"ok": False, "error": "no-default-route"}, "avg_ms")

    hop2 = _hop2_ip(conn)
    if hop2:
        rec("hop2_ping", hop2, probes.ping(hop2, count=3), "avg_ms")

    for name, ip in CONFIG.get("lan_targets", {}).items():
        res = probes.ping(ip, count=3)
        res["ip"] = ip
        rec("lan_ping", name, res, "avg_ms")

    for host in ("1.1.1.1", "8.8.8.8"):
        rec("wan_ping", host, probes.ping(host, count=3), "avg_ms")

    rec("dns", "system", probes.dns_system(), "ms")
    if gw:
        rec("dns", "router", probes.dns_query(gw), "ms")
    rec("dns", "direct", probes.dns_query("1.1.1.1"), "ms")

    rec("http", "gstatic", probes.http_check(), "ms")
    return recorded


_HOP2 = {"ip": None, "next_try": 0.0}


def _hop2_ip(conn):
    """The ISP box's IP. Discovered via traceroute once, then cached; falls back to the
    last value recorded in the DB so the probe keeps running during an outage."""
    if _HOP2["ip"]:
        return _HOP2["ip"]
    if time.time() < _HOP2["next_try"]:
        return None
    _HOP2["next_try"] = time.time() + 3600
    ip = probes.get_hop2()
    if not ip:
        row = conn.execute(
            "SELECT target FROM samples WHERE probe='hop2_ping' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        ip = row[0] if row else None
    _HOP2["ip"] = ip
    return ip


_ROUTER = {"client": None, "last_uptime": None}
ROUTER_LOG = os.path.join(BASE, "logs", "router-syslog.log")
ROUTER_EVENT_RE = None  # built on first use from the patterns below
ROUTER_EVENT_PATTERNS = {
    "wan_down": r"Ethernet link down|did not function properly",
    "wan_up": r"Ethernet link up|WAN was restored",
    "wifi": r"deauth|disassoc|assoc fail|roam.*fail",
    "dhcp": r"DHCPS?:?.*(?:offer|decline|nak)",
}


def run_router_probe(conn, source=SOURCE):
    """Ask the router itself: uptime (reboot detection), WAN state as it sees it,
    per-client radio view, and its syslog (archived + notable events extracted)."""
    cfg = CONFIG.get("router")
    if not cfg:
        return None
    import router_asus
    host = cfg.get("host", "192.168.2.1")
    if _ROUTER["client"] is None:
        _ROUTER["client"] = router_asus.AsusRouter(host, cfg["user"], cfg["pass"])
    r = _ROUTER["client"]
    try:
        up = r.uptime_secs()
        wan = r.wan()
        clients = r.clients()
        log_lines = r.syslog_lines()
    except Exception as e:
        db.insert(conn, source, "router_api", host, False, None,
                  {"error": f"{type(e).__name__}: {e}"[:150]})
        return False

    db.insert(conn, source, "router_uptime", host, True, up)
    if up and _ROUTER["last_uptime"] and up < _ROUTER["last_uptime"]:
        db.insert(conn, source, "router_event", "reboot", False, None,
                  {"line": f"router rebooted (uptime {_ROUTER['last_uptime']}s -> {up}s)"})
    _ROUTER["last_uptime"] = up

    db.insert(conn, source, "router_wan", wan.get("ipaddr") or "?",
              wan.get("status") == "1", None, wan)
    db.insert(conn, source, "router_clients", host, True, len(clients),
              {"clients": clients})

    for line in _append_router_log(log_lines):
        for kind, pat in ROUTER_EVENT_PATTERNS.items():
            if re.search(pat, line, re.IGNORECASE):
                db.insert(conn, source, "router_event", kind,
                          kind.endswith("_up"), None, {"line": line[:200]})
                break
    return True


def _append_router_log(lines):
    """Persist the router's log locally (it rotates away) and return the new lines."""
    os.makedirs(os.path.dirname(ROUTER_LOG), exist_ok=True)
    try:
        with open(ROUTER_LOG) as f:
            old_last = f.read().splitlines()[-1]
    except (FileNotFoundError, IndexError):
        old_last = None
    if old_last and old_last in lines:
        new = lines[len(lines) - 1 - lines[::-1].index(old_last) + 1:]
    else:
        new = lines
    if new:
        with open(ROUTER_LOG, "a") as f:
            f.write("\n".join(new) + "\n")
    return new


def _router_netdev():
    """Best-effort per-interface counters from the router; None if unavailable."""
    if not CONFIG.get("router"):
        return None
    try:
        import router_asus
        cfg = CONFIG["router"]
        if _ROUTER["client"] is None:
            _ROUTER["client"] = router_asus.AsusRouter(cfg["host"], cfg["user"], cfg["pass"])
        return _ROUTER["client"].netdev()
    except Exception:
        return None


def _counter_delta(before, after):
    """after - before, correcting a single 32-bit counter wrap."""
    d = after - before
    return d if d >= 0 else d + (1 << 32)


def run_speedtest(conn, source=SOURCE):
    nd0 = _router_netdev()
    t0 = time.monotonic()
    down = probes.speed_download()
    up = probes.speed_upload()
    window = time.monotonic() - t0

    contention = None
    nd1 = _router_netdev() if nd0 else None
    if nd0 and nd1 and "INTERNET" in nd0 and "INTERNET" in nd1:
        wan_rx = _counter_delta(nd0["INTERNET"]["rx"], nd1["INTERNET"]["rx"])
        our_bytes = down.get("bytes") or 0  # WAN rx counts downloads only; upload is WAN tx
        seg_tx = {seg: round(_counter_delta(nd0[seg]["tx"], nd1[seg]["tx"]) / 1e6, 1)
                  for seg in ("WIRED", "WIRELESS0", "WIRELESS1") if seg in nd0 and seg in nd1}
        # our download lands on this Mac's own segment; strip it so seg_tx shows *other* load
        contention = {
            "wan_rx_mb": round(wan_rx / 1e6, 1),
            "other_down_mb": round(max(0, wan_rx - our_bytes) / 1e6, 1),
            "seg_tx_mb": seg_tx,
            "window_s": round(window, 1),
        }
        if down.get("ok") and contention["other_down_mb"] > 30:
            busiest = max(seg_tx, key=seg_tx.get) if seg_tx else "?"
            db.insert(conn, source, "router_event", "contention", False, None,
                      {"line": f"speed test saw {contention['other_down_mb']:.0f} MB of other "
                               f"downstream traffic (busiest segment: {SEG_NAMES.get(busiest, busiest)})"})

    down_detail = dict(down)
    if contention:
        down_detail["contention"] = contention
    db.insert(conn, source, "speed_down", "cloudflare", down.get("ok", False),
              down.get("mbps"), down_detail)
    db.insert(conn, source, "speed_up", "cloudflare", up.get("ok", False),
              up.get("mbps"), up)
    return {"down": down, "up": up, "contention": contention}


SEG_NAMES = {"WIRED": "wired", "WIRELESS0": "2.4GHz WiFi", "WIRELESS1": "5GHz WiFi"}


# ---------------------------------------------------------------- diagnosis

RSSI_WEAK = -75
ROUTER_LOSS_WARN = 10.0
ROUTER_MS_WARN = 40.0
DNS_MS_WARN = 200.0


def _get(latest, probe, target=None, source=None):
    for s in latest:
        if s["probe"] == probe and (target is None or s["target"] == target) \
                and (source is None or s["source"] == source):
            return s
    return None


def diagnose(latest, source=None):
    """Turn the latest samples into per-component levels and a plain-language verdict."""
    wifi = _get(latest, "wifi", source=source)
    router = _get(latest, "gateway_ping", source=source)
    hop2 = _get(latest, "hop2_ping", source=source)
    wan1 = _get(latest, "wan_ping", "1.1.1.1", source)
    wan8 = _get(latest, "wan_ping", "8.8.8.8", source)
    dns_sys = _get(latest, "dns", "system", source)
    dns_rtr = _get(latest, "dns", "router", source)
    dns_dir = _get(latest, "dns", "direct", source)
    http = _get(latest, "http", source=source)

    comp = {}

    def level(name, sample, ok_test=None, warn_test=None, label=""):
        if sample is None:
            comp[name] = {"level": "unknown", "label": "no data"}
            return "unknown"
        if not sample["ok"]:
            err = (sample.get("detail") or {}).get("error", "failed")
            comp[name] = {"level": "bad", "label": str(err), "sample": sample}
            return "bad"
        lv = "warn" if (warn_test and warn_test(sample)) else "ok"
        comp[name] = {"level": lv, "label": label_of(sample, label), "sample": sample}
        return lv

    def label_of(s, fmt):
        try:
            return fmt.format(v=s["value"], d=s.get("detail") or {})
        except Exception:
            return str(s["value"])

    lw = level("wifi", wifi,
               warn_test=lambda s: s["value"] is not None and s["value"] <= RSSI_WEAK,
               label="{v:.0f} dBm")
    lr = level("router", router,
               warn_test=lambda s: (s.get("detail") or {}).get("loss_pct", 0) >= ROUTER_LOSS_WARN
               or (s["value"] or 0) >= ROUTER_MS_WARN,
               label="{v:.0f} ms")
    lh2 = level("isp_box", hop2, label="{v:.0f} ms") if hop2 else None
    r_wan = _get(latest, "router_wan", source=source)
    r_up = _get(latest, "router_uptime", source=source)
    if r_wan:
        level("router_wan", r_wan, label="{d[statusstr]}")
    if r_up and r_up["ok"] and r_up["value"] and comp.get("router"):
        comp["router"]["sub"] = f"up {r_up['value'] / 86400:.1f}d"
    wan_levels = [level("internet", wan1, label="{v:.0f} ms")]
    if wan8 and (not wan1 or not wan1["ok"]):
        wan_levels.append(level("internet", wan8, label="{v:.0f} ms"))
    li = "ok" if "ok" in wan_levels else wan_levels[-1]
    ls = level("dns_system", dns_sys,
               warn_test=lambda s: (s["value"] or 0) >= DNS_MS_WARN, label="{v:.0f} ms")
    lrt = level("dns_router", dns_rtr,
                warn_test=lambda s: (s["value"] or 0) >= DNS_MS_WARN, label="{v:.0f} ms")
    ld = level("dns_direct", dns_dir,
               warn_test=lambda s: (s["value"] or 0) >= DNS_MS_WARN, label="{v:.0f} ms")
    lh = level("http", http, label="{v:.0f} ms")

    lan_bad = []
    for s in latest:
        if s["probe"] == "lan_ping" and (source is None or s["source"] == source):
            if s["ok"]:
                comp["lan_" + s["target"]] = {"level": "ok", "label": f"{s['value']:.0f} ms",
                                              "sample": s}
            else:
                err = (s.get("detail") or {}).get("error", "failed")
                comp["lan_" + s["target"]] = {"level": "bad", "label": str(err), "sample": s}
                lan_bad.append(s["target"])

    # Verdict: walk the stack from the radio outwards.
    if lw == "bad":
        verdict = ("bad", "Not connected to WiFi.")
    elif lr == "bad":
        verdict = ("bad", "Connected to WiFi but can't reach the router — "
                          "WiFi link problem or the router is down/hung.")
    elif r_wan and not r_wan["ok"]:
        verdict = ("bad", "The router reports its WAN link to the ISP box is down — "
                          "ISP box, or the cable between them. Rebooting the router won't help.")
    elif lh2 == "bad":
        verdict = ("bad", "Your router is up but the ISP box behind it isn't responding — "
                          "reboot the ISP box (not the router) and check the cable between them.")
    elif li == "bad" and lh2 == "ok":
        verdict = ("bad", "Both routers are reachable but the internet beyond the ISP box is "
                          "down — fibre/ISP problem. Rebooting your router won't help; "
                          "try the fibre box, otherwise it's on the ISP.")
    elif li == "bad":
        verdict = ("bad", "Router is reachable but the internet isn't — modem/ISP side, "
                          "or the router's WAN session died (this is the case a reboot often fixes).")
    elif ls == "bad" and ld == "ok":
        verdict = ("bad", "Internet works but your DNS is broken (direct DNS to 1.1.1.1 is fine). "
                          "Router DNS problem — reboot it, or point this device's DNS at 1.1.1.1.")
    elif ls == "bad" and ld == "bad":
        verdict = ("bad", "Internet pings work but all DNS is failing — upstream DNS outage "
                          "or something blocking port 53.")
    elif lh == "bad":
        verdict = ("warn", "Pings and DNS fine but HTTPS fetch failed — flaky or heavily loaded connection.")
    elif lr == "warn":
        d = (router.get("detail") or {})
        verdict = ("warn", f"Working, but the WiFi hop to the router is degraded "
                           f"({d.get('loss_pct', 0):.0f}% loss, {router['value']:.0f} ms avg). "
                           f"Expect slowness; this is the wifi/Mac side, not the ISP.")
    elif lw == "warn":
        verdict = ("warn", "Working, but WiFi signal is weak.")
    elif "warn" in (ls, lrt, ld):
        verdict = ("warn", "Working, but DNS is slow.")
    elif lan_bad:
        verdict = ("warn", f"Internet is fine, but {', '.join(lan_bad)} isn't responding — "
                           f"its WiFi is dead until it gets a power-cycle.")
    else:
        verdict = ("ok", "All good.")

    return {"components": comp, "verdict": {"level": verdict[0], "text": verdict[1]}}


# ---------------------------------------------------------------- runner

RETAIN_DAYS = 90


def collector_loop(conn_path, lock, interval, speed_interval, stop):
    conn = db.connect(conn_path)
    last_speed = 0.0
    last_prune = 0.0
    last_router = 0.0
    router_interval = (CONFIG.get("router") or {}).get("interval", 300)
    while not stop.is_set():
        t0 = time.time()
        try:
            with lock:
                run_cycle(conn)
            if CONFIG.get("router") and time.time() - last_router >= router_interval:
                run_router_probe(conn)
                last_router = time.time()
            if speed_interval and time.time() - last_speed >= speed_interval:
                with lock:
                    run_speedtest(conn)
                last_speed = time.time()
            if time.time() - last_prune >= 24 * 3600:
                conn.execute("DELETE FROM samples WHERE ts < ?",
                             (time.time() - RETAIN_DAYS * 24 * 3600,))
                conn.commit()
                last_prune = time.time()
        except Exception as e:
            print(f"[collector] cycle error: {e!r}", flush=True)
        stop.wait(max(1.0, interval - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("speedtest")
    runp = sub.add_parser("run")
    runp.add_argument("--port", type=int, default=DEFAULT_PORT)
    runp.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    runp.add_argument("--speed-interval", type=int, default=DEFAULT_SPEED_INTERVAL,
                      help="seconds between bandwidth tests, 0 to disable")
    args = ap.parse_args()

    conn = db.connect(args.db)

    if args.cmd == "probe":
        for s in run_cycle(conn):
            mark = "ok " if s["ok"] else "FAIL"
            val = f"{s['value']}" if s["value"] is not None else "-"
            print(f"{mark} {s['probe']:<13} {s['target']:<10} {val:<8} {s['detail']}")
        res = diagnose(db.latest(conn))
        print(f"\nverdict [{res['verdict']['level']}]: {res['verdict']['text']}")
    elif args.cmd == "speedtest":
        print(json.dumps(run_speedtest(conn), indent=2))
    elif args.cmd == "run":
        import server
        lock = threading.Lock()
        stop = threading.Event()
        t = threading.Thread(target=collector_loop,
                             args=(args.db, lock, args.interval, args.speed_interval, stop),
                             daemon=True)
        t.start()
        print(f"netmon: collecting every {args.interval}s, "
              f"dashboard on http://0.0.0.0:{args.port}", flush=True)
        try:
            server.serve(args.db, lock, args.port)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()


if __name__ == "__main__":
    main()
