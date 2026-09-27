import json
import os
import unittest
from unittest.mock import patch

import ollaya_observer


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({
            "failure_class": {"value": "build_error", "probability": 0.9},
            "should_reflect": {"value": "yes", "probability": 0.8},
        }).encode()


class OllayaObserverTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ollaya_observer.enabled())

    def test_observer_is_advisory_and_records_result(self):
        state = {
            "status": "running",
            "phase": "post_edit_repair",
            "rounds": 7,
            "repair_attempts": 2,
            "build_status": "exit=1\nType error",
            "test_status": "exit=1\nfailed",
            "hypothesis": "restore missing render path",
            "last_tool": "build_project",
        }
        with patch.dict(os.environ, {"LOCAL_ENGINEER_OLLAYA": "1"}, clear=False),              patch("ollaya_observer.urllib.request.urlopen", return_value=FakeResponse()) as open_mock:
            result = ollaya_observer.observe(state, "repair")
        self.assertIsNotNone(result)
        self.assertTrue(state["ollaya_observer"]["available"])
        self.assertEqual(open_mock.call_count, 1)
        # Observer output must not directly alter control state.
        self.assertEqual(state["phase"], "post_edit_repair")
        self.assertEqual(state["status"], "running")

    def test_unavailable_ollaya_does_not_break_agent_state(self):
        state = {"status": "running", "phase": "normal"}
        with patch.dict(os.environ, {"LOCAL_ENGINEER_OLLAYA": "1"}, clear=False),              patch("ollaya_observer.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(ollaya_observer.observe(state, "task"))
        self.assertFalse(state["ollaya_observer"]["available"])
        self.assertEqual(state["status"], "running")


if __name__ == "__main__":
    unittest.main()
