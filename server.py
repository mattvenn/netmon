"""Dashboard web server. Serves the UI and a small JSON API over the LAN."""

import json
import os
import socket
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import netmon

try:
    import qrcode
    import qrcode.image.svg
except ImportError:  # optional: dashboard hides the QR if unavailable
    qrcode = None

BASE = os.path.dirname(os.path.abspath(__file__))

# set by serve()
DB_PATH = None
CYCLE_LOCK = None
PORT = None


def lan_ip():
    """The address this machine has on the LAN (no traffic is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep stdout for collector messages

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, name):
        try:
            with open(os.path.join(BASE, name), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        except FileNotFoundError:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            self._page("dashboard.html")
        elif url.path == "/phone":
            self._page("phone.html")
        elif url.path == "/api/ping":
            self._send(200, {"pong": time.time()})
        elif url.path == "/api/qr.svg":
            ip = lan_ip()
            if not qrcode or not ip:
                self._send(503, {"error": "qrcode library not installed" if not qrcode
                                 else "no LAN address"})
                return
            import io
            img = qrcode.make(f"http://{ip}:{PORT}/",
                              image_factory=qrcode.image.svg.SvgPathImage, border=4)
            buf = io.BytesIO()
            img.save(buf)
            self._send(200, buf.getvalue(), "image/svg+xml")
        elif url.path == "/api/status":
            conn = db.connect(DB_PATH)
            latest = db.latest(conn, window_s=180)
            speed = db.history(conn, time.time() - 24 * 3600, ["speed_down", "speed_up"])
            sources = [r[0] for r in conn.execute(
                "SELECT DISTINCT source FROM samples WHERE ts >= ?",
                (time.time() - 7 * 24 * 3600,))]
            conn.close()
            res = netmon.diagnose(latest)
            res["ts"] = time.time()
            res["sources"] = sources
            res["last_speed"] = {s["probe"]: s for s in speed[-4:]}
            ip = lan_ip()
            res["lan_url"] = f"http://{ip}:{PORT}/" if ip else None
            self._send(200, res)
        elif url.path == "/api/history":
            hours = float(qs.get("hours", ["6"])[0])
            conn = db.connect(DB_PATH)
            rows = db.history(conn, time.time() - hours * 3600)
            conn.close()
            self._send(200, {"rows": rows})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        if self.path == "/api/run-probe":
            conn = db.connect(DB_PATH)
            with CYCLE_LOCK:
                recorded = netmon.run_cycle(conn)
            res = netmon.diagnose(db.latest(conn))
            conn.close()
            self._send(200, {"recorded": recorded, **res})
        elif self.path == "/api/run-speedtest":
            conn = db.connect(DB_PATH)
            with CYCLE_LOCK:
                res = netmon.run_speedtest(conn)
            conn.close()
            self._send(200, res)
        elif self.path == "/api/phone-report":
            try:
                payload = json.loads(body)
                source = str(payload.get("source", "phone"))[:32]
                conn = db.connect(DB_PATH)
                n = 0
                for t in payload.get("tests", [])[:20]:
                    db.insert(conn, source, "phone_" + str(t.get("probe", "?"))[:24],
                              str(t.get("target", "?"))[:64], bool(t.get("ok")),
                              t.get("value"), t.get("detail"))
                    n += 1
                conn.close()
                self._send(200, {"recorded": n})
            except (ValueError, KeyError, TypeError) as e:
                self._send(400, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})


def serve(db_path, lock, port):
    global DB_PATH, CYCLE_LOCK, PORT
    DB_PATH = db_path
    CYCLE_LOCK = lock
    PORT = port
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.serve_forever()
