#!/usr/bin/env python3
"""Publish a sanitized Local Engineer live snapshot to the dedicated live-status branch.

This intentionally publishes only operational state. It never publishes event logs,
source code, tool arguments, or checkpoint files.
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import pathlib
import subprocess
import sys

REPO = "vwbugowner1976/local-engineer"
BRANCH = "live-status"
PATH = "live/status.json"
HERE = pathlib.Path(__file__).resolve().parent


def gh(*args: str) -> str:
    p = subprocess.run(["gh", *args], cwd=HERE, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "gh failed")
    return p.stdout


def latest_session():
    sessions = pathlib.Path.home() / ".local/state/local-engineer/sessions"
    if not sessions.exists():
        return None
    dirs = [p for p in sessions.iterdir() if p.is_dir()]
    return max(dirs, key=lambda p: p.name) if dirs else None


def read_state(session):
    if not session:
        return {}
    try:
        return json.loads((session / "working_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def compact(value, limit=1200):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def snapshot():
    session = latest_session()
    state = read_state(session)
    return {
        "schema": "local-engineer-live-v1",
        "published_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session": session.name if session else None,
        "status": state.get("status", "not_running"),
        "phase": state.get("phase", "unknown"),
        "round": state.get("rounds", 0),
        "repair_attempt": state.get("repair_attempts", 0),
        "last_tool": state.get("last_tool", ""),
        "target": state.get("hypothesis_target_file", ""),
        "next_action": compact(state.get("next_action", ""), 1000),
        "build_status": compact(state.get("build_status", ""), 1200),
        "test_status": compact(state.get("test_status", ""), 1200),
        "hypothesis": compact(state.get("hypothesis", ""), 1200),
        "files_modified": state.get("files_modified", [])[-20:],
    }


def get_existing_sha():
    try:
        raw = gh("api", f"repos/{REPO}/contents/{PATH}?ref={BRANCH}")
        return json.loads(raw).get("sha")
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def publish(payload):
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("ascii")
    args = [
        "api", "--method", "PUT", f"repos/{REPO}/contents/{PATH}",
        "-H", "Accept: application/vnd.github+json",
        "-f", "message=live: update Local Engineer status",
        "-f", f"content={encoded}",
        "-f", f"branch={BRANCH}",
    ]
    sha = get_existing_sha()
    if sha:
        args += ["-f", f"sha={sha}"]
    gh(*args)


def main():
    try:
        payload = snapshot()
        publish(payload)
        print(json.dumps({"ok": True, "session": payload["session"],
                          "round": payload["round"], "status": payload["status"]}))
    except Exception as exc:
        print(f"live publish failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
