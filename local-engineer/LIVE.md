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
