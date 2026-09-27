"""Optional local Ollaya observer for Local Engineer.

Disabled by default. Enable with LOCAL_ENGINEER_OLLAYA=1.
The observer is advisory only: it never changes the repair decision path.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import urllib.request

DEFAULT_URL = "http://127.0.0.1:11435/v1/systemone"


def enabled() -> bool:
    return os.environ.get("LOCAL_ENGINEER_OLLAYA", "").lower() in ("1", "true", "yes", "on")


def _compact(value, limit=900):
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _classify(state):
    build = state.get("build_status", "")
    test = state.get("test_status", "")
    return {
        "phase": state.get("phase", ""),
        "status": state.get("status", ""),
        "build": _compact(build, 900),
        "test": _compact(test, 900),
        "hypothesis": _compact(state.get("hypothesis", ""), 900),
        "last_tool": state.get("last_tool", ""),
        "round": state.get("rounds", 0),
        "repair_attempt": state.get("repair_attempts", 0),
    }


def decide(state, timeout=8):
    payload = {
        "model": os.environ.get("LOCAL_ENGINEER_OLLAYA_MODEL", "laya"),
        "state": json.dumps(_classify(state), ensure_ascii=False),
        "questions": {
            "failure_class": {
                "type": "choice",
                "instructions": "Classify the current Local Engineer state.",
                "criteria": {
                    "none": "No current build/test failure is present.",
                    "build_error": "Build/compiler/type-check failure.",
                    "test_error": "Test or assertion failure.",
                    "repair_loop": "Repeated repair attempts or repeated tool loop.",
                    "unknown": "Insufficient evidence to classify."
                }
            },
            "should_reflect": {
                "type": "choice",
                "instructions": "Should the agent reconsider its current hypothesis?",
                "criteria": {
                    "yes": "Current evidence indicates the hypothesis may be wrong or stale.",
                    "no": "Current evidence is consistent with the hypothesis.",
                    "unknown": "Insufficient evidence."
                }
            }
        }
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        os.environ.get("LOCAL_ENGINEER_OLLAYA_URL", DEFAULT_URL),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer local",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def observe(state, task=""):
    """Record an advisory decision when explicitly enabled.

    Failure to reach Ollaya is deliberately silent so the Bonsai path remains
    unchanged. Results are kept in working_state.json via the caller's save.
    """
    if not enabled():
        return None
    try:
        result = decide(state)
    except Exception as exc:
        state["ollaya_observer"] = {
            "enabled": True,
            "available": False,
            "error": type(exc).__name__,
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        return None
    state["ollaya_observer"] = {
        "enabled": True,
        "available": True,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "result": result,
    }
    return result
