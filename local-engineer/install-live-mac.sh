#!/bin/bash
set -euo pipefail

ROOT="/Users/macmini/local-engineer-src/local-engineer"
USER_NAME="$(id -un)"
USER_HOME="$HOME"
UID_NOW="$(id -u)"
AGENT_DIR="$USER_HOME/Library/LaunchAgents"
LOG_DIR="$USER_HOME/Library/Logs/local-engineer"
LIVE_AGENT_PLIST="$AGENT_DIR/com.localengineer.live.plist"
PUBLISH_AGENT_PLIST="$AGENT_DIR/com.localengineer.live-publish.plist"
DAEMON_DIR="/Library/LaunchDaemons"
LIVE_DAEMON_PLIST="$DAEMON_DIR/com.localengineer.live.plist"
PUBLISH_DAEMON_PLIST="$DAEMON_DIR/com.localengineer.live-publish.plist"

mkdir -p "$LOG_DIR"

if [ ! -f "$ROOT/live_server.py" ]; then
  echo "ERROR: $ROOT/live_server.py not found"
  exit 1
fi
if [ ! -f "$ROOT/publish_live_status.py" ]; then
  echo "ERROR: $ROOT/publish_live_status.py not found"
  exit 1
fi

PYTHON="$(command -v python3 || true)"
GH="$(command -v gh || true)"
TAILSCALE="$(command -v tailscale || true)"

if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found"
  exit 1
fi
if [ -z "$GH" ]; then
  echo "ERROR: GitHub CLI (gh) not found. Install/login gh first."
  exit 1
fi
if ! "$GH" auth status >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated. Run: gh auth login"
  exit 1
fi

# If a manually started live_server is already listening on :8765, hand that
# listener over to launchd. Do not kill an unrelated process on this port.
PIDS="$(/usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  for PID in $PIDS; do
    CMD="$(/bin/ps -p "$PID" -o command= 2>/dev/null || true)"
    if [[ "$CMD" == *"$ROOT/live_server.py"* ]]; then
      echo "Stopping existing manual live_server (pid $PID) so launchd can own :8765."
      /bin/kill "$PID" 2>/dev/null || true
    else
      echo "ERROR: port 8765 is already used by another process:"
      echo "  pid=$PID"
      echo "  $CMD"
      echo "Refusing to stop an unrelated process."
      exit 1
    fi
  done
  /bin/sleep 1
fi

# Detect whether this SSH session has a usable GUI launchd domain.
# SSH-only sessions commonly have no gui/$UID domain, which is exactly the
# failure mode seen on this Mac (launchctl error 125).
GUI_OK=0
if /bin/launchctl print "gui/$UID_NOW" >/dev/null 2>&1; then
  GUI_OK=1
fi

write_agent_plists() {
  mkdir -p "$AGENT_DIR"

  cat > "$LIVE_AGENT_PLIST" <<PLIST
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

  cat > "$PUBLISH_AGENT_PLIST" <<PLIST
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
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$GH"):/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$USER_HOME</string>
  </dict>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/publish.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/publish.error.log</string>
</dict>
</plist>
PLIST

  /usr/bin/plutil -lint "$LIVE_AGENT_PLIST" "$PUBLISH_AGENT_PLIST"
  chmod 600 "$LIVE_AGENT_PLIST" "$PUBLISH_AGENT_PLIST"

  /bin/launchctl bootout "gui/$UID_NOW/com.localengineer.live" 2>/dev/null || true
  /bin/launchctl bootout "gui/$UID_NOW/com.localengineer.live-publish" 2>/dev/null || true
  /bin/launchctl bootstrap "gui/$UID_NOW" "$LIVE_AGENT_PLIST"
  /bin/launchctl bootstrap "gui/$UID_NOW" "$PUBLISH_AGENT_PLIST"
  /bin/launchctl kickstart -kp "gui/$UID_NOW/com.localengineer.live" || true
  /bin/launchctl kickstart -kp "gui/$UID_NOW/com.localengineer.live-publish" || true
}

write_daemon_plists() {
  local tmp_live tmp_publish
  tmp_live="$(mktemp /tmp/com.localengineer.live.XXXXXX.plist)"
  tmp_publish="$(mktemp /tmp/com.localengineer.live-publish.XXXXXX.plist)"
  trap 'rm -f "$tmp_live" "$tmp_publish"' RETURN

  cat > "$tmp_live" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.localengineer.live</string>
  <key>UserName</key><string>$USER_NAME</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/live_server.py</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$USER_HOME</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/live.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/live.error.log</string>
</dict>
</plist>
PLIST

  cat > "$tmp_publish" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.localengineer.live-publish</string>
  <key>UserName</key><string>$USER_NAME</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/publish_live_status.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$USER_HOME</string>
    <key>PATH</key><string>$(dirname "$GH"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/publish.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/publish.error.log</string>
</dict>
</plist>
PLIST

  /usr/bin/plutil -lint "$tmp_live" "$tmp_publish"

  echo "No usable gui/$UID_NOW launchd domain found (SSH session)."
  echo "Installing persistent LaunchDaemons as user $USER_NAME; sudo is required."

  /usr/bin/sudo /bin/mkdir -p "$DAEMON_DIR"
  /usr/bin/sudo /usr/sbin/chown root:wheel "$tmp_live" "$tmp_publish"
  /usr/bin/sudo /bin/chmod 644 "$tmp_live" "$tmp_publish"
  /usr/bin/sudo /usr/bin/install -m 644 "$tmp_live" "$LIVE_DAEMON_PLIST"
  /usr/bin/sudo /usr/bin/install -m 644 "$tmp_publish" "$PUBLISH_DAEMON_PLIST"
  /usr/bin/sudo /usr/sbin/chown root:wheel "$LIVE_DAEMON_PLIST" "$PUBLISH_DAEMON_PLIST"

  /usr/bin/sudo /bin/launchctl bootout "system/com.localengineer.live" 2>/dev/null || true
  /usr/bin/sudo /bin/launchctl bootout "system/com.localengineer.live-publish" 2>/dev/null || true
  /usr/bin/sudo /bin/launchctl bootstrap system "$LIVE_DAEMON_PLIST"
  /usr/bin/sudo /bin/launchctl bootstrap system "$PUBLISH_DAEMON_PLIST"
  /usr/bin/sudo /bin/launchctl kickstart -kp "system/com.localengineer.live" || true
  /usr/bin/sudo /bin/launchctl kickstart -kp "system/com.localengineer.live-publish" || true

  # Do not leave stale LaunchAgent files around after switching an SSH-only
  # Mac to daemon mode.
  rm -f "$LIVE_AGENT_PLIST" "$PUBLISH_AGENT_PLIST"
}

if [ "$GUI_OK" -eq 1 ]; then
  echo "Detected usable gui/$UID_NOW launchd domain; using LaunchAgents."
  write_agent_plists
  MODE="LaunchAgent"
else
  write_daemon_plists
  MODE="LaunchDaemon"
fi

if [ -n "$TAILSCALE" ]; then
  "$TAILSCALE" funnel --bg 8765 >/tmp/local-engineer-funnel.log 2>&1 || true
else
  echo "WARNING: tailscale command not found; existing Funnel configuration was not changed."
fi

echo
echo "Local Engineer Live installed using $MODE."
echo "Android viewer: https://macminimac-mini.tailaf7a1e.ts.net/"
echo "ChatGPT bridge: GitHub live-status/live/status.json"
echo "Logs: $LOG_DIR"
echo
echo "Verify:"
if [ "$MODE" = "LaunchDaemon" ]; then
  echo "  sudo launchctl print system/com.localengineer.live"
  echo "  sudo launchctl print system/com.localengineer.live-publish"
else
  echo "  launchctl print gui/$UID_NOW/com.localengineer.live"
  echo "  launchctl print gui/$UID_NOW/com.localengineer.live-publish"
fi
echo "  curl http://127.0.0.1:8765/status"
echo "  curl https://macminimac-mini.tailaf7a1e.ts.net/status"
