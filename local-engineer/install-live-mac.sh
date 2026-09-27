#!/bin/bash
set -euo pipefail

ROOT="/Users/macmini/local-engineer-src/local-engineer"
AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/local-engineer"
LIVE_PLIST="$AGENT_DIR/com.localengineer.live.plist"
PUBLISH_PLIST="$AGENT_DIR/com.localengineer.live-publish.plist"

mkdir -p "$AGENT_DIR" "$LOG_DIR"

if [ ! -f "$ROOT/live_server.py" ]; then
  echo "ERROR: $ROOT/live_server.py not found"
  exit 1
fi
if [ ! -f "$ROOT/publish_live_status.py" ]; then
  echo "ERROR: $ROOT/publish_live_status.py not found"
  exit 1
fi

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found"
  exit 1
fi

GH="$(command -v gh || true)"
if [ -z "$GH" ]; then
  echo "ERROR: GitHub CLI (gh) not found. Install/login gh first."
  exit 1
fi

if ! "$GH" auth status >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated. Run: gh auth login"
  exit 1
fi

cat > "$LIVE_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.localengineer.live</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/live_server.py</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/live.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/live.error.log</string>
</dict>
</plist>
PLIST

cat > "$PUBLISH_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.localengineer.live-publish</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/publish_live_status.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/publish.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/publish.error.log</string>
</dict>
</plist>
PLIST

chmod 600 "$LIVE_PLIST" "$PUBLISH_PLIST"

UID_NOW="$(id -u)"
/bin/launchctl bootout "gui/$UID_NOW/com.localengineer.live" 2>/dev/null || true
/bin/launchctl bootout "gui/$UID_NOW/com.localengineer.live-publish" 2>/dev/null || true
/bin/launchctl bootstrap "gui/$UID_NOW" "$LIVE_PLIST"
/bin/launchctl bootstrap "gui/$UID_NOW" "$PUBLISH_PLIST"

/bin/launchctl kickstart -kp "gui/$UID_NOW/com.localengineer.live"
/bin/launchctl kickstart -kp "gui/$UID_NOW/com.localengineer.live-publish"

TAILSCALE="$(command -v tailscale || true)"
if [ -n "$TAILSCALE" ]; then
  "$TAILSCALE" funnel --bg 8765 >/tmp/local-engineer-funnel.log 2>&1 || true
else
  echo "WARNING: tailscale command not found; existing Funnel configuration was not changed."
fi

echo
echo "Local Engineer Live installed."
echo "Android viewer: https://macminimac-mini.tailaf7a1e.ts.net/"
echo "ChatGPT bridge: GitHub live-status/live/status.json"
echo "Logs: $LOG_DIR"
