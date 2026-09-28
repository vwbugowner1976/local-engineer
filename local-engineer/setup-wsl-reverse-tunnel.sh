#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 macmini@100.125.201.25"
  exit 2
fi

REMOTE="$1"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/local-engineer-reverse-tunnel.service"

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh not found"
  exit 1
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT" <<EOF
[Unit]
Description=Local Engineer reverse SSH tunnel to WSL
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=10 -R 127.0.0.1:2222:127.0.0.1:22 $REMOTE
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

if ! systemctl --user daemon-reload; then
  echo "systemd user services are not available in this WSL instance."
  echo "Enable WSL systemd, then run this script again."
  exit 1
fi

systemctl --user enable --now local-engineer-reverse-tunnel.service
sleep 1

if systemctl --user is-active --quiet local-engineer-reverse-tunnel.service; then
  echo "LOCAL_ENGINEER_REVERSE_TUNNEL=OK"
  echo "127.0.0.1:2222 -> WSL:22"
else
  echo "LOCAL_ENGINEER_REVERSE_TUNNEL=FAIL"
  systemctl --user --no-pager status local-engineer-reverse-tunnel.service || true
  exit 1
fi
