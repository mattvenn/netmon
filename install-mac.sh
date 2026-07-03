#!/bin/bash
# Install (or remove) netmon as a launchd agent so it runs whenever you're logged in.
#   ./install-mac.sh          install + start
#   ./install-mac.sh remove   stop + uninstall
set -e

LABEL="com.mattvenn.netmon"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# project venv for dependencies (requirements.txt)
if [ ! -x "$DIR/.venv/bin/python3" ]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
PYTHON3="$DIR/.venv/bin/python3"

if [ "$1" = "remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "netmon removed. Data kept in $DIR/netmon.db"
  exit 0
fi

mkdir -p "$DIR/logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON3</string>
    <string>$DIR/netmon.py</string>
    <string>--db</string><string>$DIR/netmon.db</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/netmon.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/netmon.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 2
IP=$(ipconfig getifaddr en0 2>/dev/null || echo "<mac-ip>")
echo "netmon running."
echo "  dashboard:  http://localhost:8737"
echo "  from phone: http://$IP:8737  (test page: http://$IP:8737/phone)"
echo "  logs:       $DIR/logs/netmon.log"
