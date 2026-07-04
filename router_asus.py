"""Thin client for the Asus (asuswrt) router web API: uptime, WAN state,
client list, syslog. Stdlib only — login mimics the web UI's login.cgi."""

import base64
import json
import re
import urllib.parse
import urllib.request


class AsusRouter:
    def __init__(self, host, user, password, timeout=6):
        self.base = f"http://{host}"
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout
        self.token = None

    def login(self):
        body = ("group_id=&action_mode=&action_script=&action_wait=5"
                "&current_page=Main_Login.asp&next_page=index.asp"
                "&login_authorization=" + urllib.parse.quote(self._auth)).encode()
        req = urllib.request.Request(self.base + "/login.cgi", data=body)
        req.add_header("Referer", self.base + "/Main_Login.asp")
        req.add_header("User-Agent", "netmon")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            cookie = r.headers.get("Set-Cookie") or ""
        m = re.search(r"asus_token=([^;]+)", cookie)
        if not m:
            raise RuntimeError("router login refused (bad credentials?)")
        self.token = m.group(1)

    def _get(self, path, _retry=True):
        if not self.token:
            self.login()
        req = urllib.request.Request(self.base + path)
        req.add_header("Referer", self.base + "/index.asp")
        req.add_header("User-Agent", "netmon")
        req.add_header("Cookie", "asus_token=" + self.token)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            out = r.read().decode(errors="replace")
        if "error_status" in out or "Main_Login.asp" in out:  # session expired
            if _retry:
                self.token = None
                return self._get(path, _retry=False)
            raise RuntimeError("router session rejected")
        return out

    def _appget(self, hooks):
        return self._get("/appGet.cgi?hook=" + urllib.parse.quote(hooks, safe="()"))

    def uptime_secs(self):
        m = re.search(r"\((\d+) secs since boot", self._appget("uptime()"))
        return int(m.group(1)) if m else None

    def wan(self):
        out = self._appget("wanlink()")

        def field(name):
            m = re.search(r"wanlink_%s\(\)\s*\{\s*return\s*'?(.*?)'?;\s*\}" % name, out)
            return m.group(1) if m else None

        return {"status": field("status"), "statusstr": field("statusstr"),
                "ipaddr": field("ipaddr"), "gateway": field("gateway"),
                "dns": field("dns"), "type": field("type")}

    def clients(self):
        """Online clients with the router's view of each: ip, rssi, band."""
        d = json.loads(self._appget("get_clientlist()"))["get_clientlist"]
        conn_names = {"0": "wired", "1": "2.4GHz", "2": "5GHz"}
        out = []
        for mac, v in d.items():
            if ":" not in mac or not isinstance(v, dict) or v.get("isOnline") != "1":
                continue
            rssi = v.get("rssi")
            out.append({"mac": mac,
                        "name": (v.get("name") or v.get("nickName") or "").strip()[:24],
                        "ip": v.get("ip"),
                        "rssi": int(rssi) if rssi and rssi != "0" else None,
                        "conn": conn_names.get(v.get("isWL"), v.get("isWL"))})
        return out

    def netdev(self):
        """Cumulative per-interface byte counters. asuswrt returns hex; counters may be
        32-bit and wrap. Keys: INTERNET (WAN), WIRED, WIRELESS0 (2.4GHz), WIRELESS1 (5GHz)."""
        out = self._appget("netdev(appobj)")
        res = {}
        for m in re.finditer(r"'(\w+)':\{rx:0x([0-9a-fA-F]+),tx:0x([0-9a-fA-F]+)\}", out):
            res[m.group(1)] = {"rx": int(m.group(2), 16), "tx": int(m.group(3), 16)}
        return res

    def client_traffic(self, epoch, dura=24):
        """Per-client WAN usage from Traffic Analyzer (needs it enabled + USB storage).
        Returns [{mac, down_mb, up_mb}] for the trailing `dura` hours. Empty if TA has no data."""
        out = self._get(f"/getAppTraffic.asp?client=all&mode=detail&dura={dura}&date={int(epoch)}")
        m = re.search(r"array_statistics\s*=\s*(\[.*\]);", out, re.S)
        if not m:
            return []
        rows = []
        for entry in __import__("json").loads(m.group(1)):
            if len(entry) == 3 and isinstance(entry[0], str) and ":" in entry[0]:
                rows.append({"mac": entry[0].upper(),
                             "down_mb": round(entry[2] / 1e6, 2),
                             "up_mb": round(entry[1] / 1e6, 2)})
        return rows

    def syslog_lines(self):
        html = self._get("/Main_LogStatus_Content.asp")
        m = re.search(r"<textarea[^>]*>(.*?)</textarea>", html, re.S)
        return m.group(1).strip().splitlines() if m else []
