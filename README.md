# netmon — home network monitor

Figures out *which layer* of the home network is misbehaving: the Mac's WiFi,
the router, the router's DNS, or the ISP. A collector measures the network
every 30 seconds into SQLite; a dashboard shows a plain-language verdict and
history charts.

Runs the same on macOS and on a Raspberry Pi. Core monitoring is Python
stdlib; the one dependency (`qrcode`, for the dashboard's scan-to-open QR)
lives in a project venv that `install-mac.sh` sets up.

## Quick start

```bash
python3 netmon.py probe        # one measurement cycle, printed (no deps needed)
./install-mac.sh               # venv + launchd agent (starts at login, restarts if killed)
./install-mac.sh remove        # uninstall (keeps the data)
```

From the phone (same WiFi): scan the QR code in the dashboard header, or go to
`http://<mac-ip>:8737`. The phone-side test page is at `/phone` — run it when
things feel broken and the results are logged next to the Mac's, so you can
see whether the problem is the Mac or the network.

The dashboard binds to all interfaces so the phone can reach it. It is
read-mostly and LAN-only by assumption — don't port-forward it.

## What is measured (every 30 s)

| Probe | What it distinguishes |
|---|---|
| WiFi signal/noise, channel, band, link rate | Mac↔router radio quality; band-flapping (2.4 vs 5 GHz) |
| Ping router (5×) | the WiFi hop itself — loss/jitter here means WiFi or router, never the ISP |
| Ping 1.1.1.1 and 8.8.8.8 (3×) | the ISP/upstream path |
| DNS via system resolver | what apps actually experience |
| DNS asking the router directly | the router's DNS forwarder (the thing a reboot often fixes) |
| DNS asking 1.1.1.1 directly | bypasses the router's DNS entirely |
| HTTPS fetch (gstatic 204) | the full real-world stack |
| Bandwidth up/down vs Cloudflare (every 15 min) | throughput history; capped at ~12 s per direction |

Data lives in `netmon.db` (SQLite), pruned after 90 days.

## Reading the verdict

The banner walks the stack from the radio outwards — the first broken layer is
the diagnosis:

- **Can't reach the router** → WiFi link or router hung. If the phone still
  works (check `/phone`), it's the Mac's WiFi.
- **Router reachable, internet down** → modem/ISP, or the router's WAN session
  died. This is the case where a router reboot plausibly helps.
- **Internet fine, system DNS broken, direct DNS fine** → the router's DNS
  forwarder is wedged. Reboot the router, or set the device's DNS to 1.1.1.1
  and carry on.
- **Everything up but router ping is lossy/slow** → degraded WiFi (interference,
  weak signal, band problems). Reboot won't fix radio interference; check the
  WiFi signal chart and the channel/band in the WiFi tile.

Mac broken while phone is fine + dashboard shows router/internet OK → the
problem is below the probes on the Mac itself (WiFi driver, private relay/VPN,
per-device DNS). Compare with a `/phone` run from the same moment.

## Raspberry Pi (always-on second vantage point)

Copy this directory to the Pi, then see `install/netmon.service`. Run it on
the Pi and *don't* run the collector on the Mac (or do — the dashboard has a
source switcher when more than one machine reports). Pi needs `iw` for WiFi
stats (present on Raspberry Pi OS).

## Files

- `netmon.py` — CLI: `probe` / `speedtest` / `run`; diagnosis rules live here
- `probes.py` — the measurements (ping, raw DNS, HTTP, WiFi info, speed)
- `db.py` — SQLite schema and queries
- `server.py` — JSON API + serves the two pages
- `dashboard.html`, `phone.html` — self-contained UI, no external assets
- `install-mac.sh`, `install/netmon.service` — autostart for Mac / Pi
