# Local Engineer Live

A small local status/event viewer for the existing Local Engineer session files.

## Run on the Mac

```bash
cd /Users/macmini/local-engineer-src/local-engineer
python3 live_server.py --host 0.0.0.0 --port 8765
```

Then open `http://<Mac-mini-IP>:8765/` from a phone on the same network.

### Endpoints

- `/status` — compact live state
- `/events?since=N` — incremental JSONL events
- `/current` — current working_state.json
- `/checkpoint` — current checkpoint path
- `/stop` and `/resume` — create control request files (the current runtime/launcher must consume these before they become active controls)

The viewer polls every 1.2 seconds, so it behaves like a live tail without requiring WebSocket support.

The server reads the existing `sessions/<id>/events.jsonl` emitted by Local Engineer and does not modify the agent's execution loop.


## Automatic macOS startup

Install the viewer as a per-user `launchd` agent so closing Terminal does not stop it:

```bash
cd /Users/macmini/local-engineer-src/local-engineer
bash install-live-mac.sh
```

The installer creates two user agents:

- `com.localengineer.live` — keeps `live_server.py` on port 8765.
- `com.localengineer.live-publish` — publishes a **sanitized** status snapshot every 15 seconds.

It also re-applies the Tailscale Funnel configuration with `tailscale funnel --bg 8765` when the Tailscale CLI is available. The live server is managed by macOS `launchd`, so it is not tied to a Terminal window. Apple documents LaunchAgents as the per-user mechanism managed by `launchd`. 

## ChatGPT-readable bridge

The publisher writes only operational state to the dedicated `live-status` branch:

```
live/status.json
```

This avoids exposing `events.jsonl`, tool arguments, source contents, or checkpoints. GitHub's contents API supports updating a file on a branch, and the GitHub CLI uses the user's stored authentication for authenticated API calls.

The current snapshot can be read from the GitHub repository with the GitHub connector. It is intentionally **not** a control channel: the published file cannot stop/resume Local Engineer.

The publisher interval is 15 seconds to keep GitHub API traffic bounded while still giving near-live visibility.
