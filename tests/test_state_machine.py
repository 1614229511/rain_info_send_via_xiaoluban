"""状态机与防抖逻辑单元测试（纯本地，不联网）。"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import state_machine as sm  # noqa: E402


class EvaluateTests(unittest.TestCase):
    def test_initial_state(self) -> None:
        state = sm.new_state()
        self.assertEqual(state["status"], "DRY")
        self.assertEqual(state["dry_counter"], 0)
        self.assertEqual(state["last_event_time"], 0)

    def test_rain_start(self) -> None:
        result = sm.evaluate(sm.new_state(), 0.5, now=1_000)
        self.assertEqual(result["event"], sm.EVENT_RAIN_START)
        self.assertTrue(result["push"])
        self.assertEqual(result["state"]["status"], "RAINING")
        self.assertTrue(result["state"]["rain_announced"])
        self.assertEqual(result["state"]["last_event_time"], 1_000)

    def test_stay_raining_no_event(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        result = sm.evaluate(state, 0.9, now=1_300)
        self.assertIsNone(result["event"])
        self.assertFalse(result["push"])
        self.assertEqual(result["state"]["status"], "RAINING")

    def test_threshold_is_inclusive(self) -> None:
        result = sm.evaluate(sm.new_state(), 0.08, now=1_000)
        self.assertEqual(result["event"], sm.EVENT_RAIN_START)

    def test_below_threshold_after_start_triggers_debounce(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]

        first = sm.evaluate(state, 0.01, now=1_300)
        self.assertIsNone(first["event"])
        self.assertEqual(first["state"]["dry_counter"], 1)

        second = sm.evaluate(first["state"], 0.0, now=1_600)
        self.assertIsNone(second["event"])
        self.assertEqual(second["state"]["dry_counter"], 2)

        third = sm.evaluate(second["state"], 0.02, now=1_900)
        self.assertEqual(third["event"], sm.EVENT_RAIN_STOP)
        self.assertTrue(third["push"])
        self.assertEqual(third["state"]["status"], "DRY")
        self.assertEqual(third["state"]["dry_counter"], 0)
        self.assertFalse(third["state"]["rain_announced"])

    def test_rain_resumes_during_debounce_resets_counter(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        state = sm.evaluate(state, 0.0, now=1_300)["state"]
        self.assertEqual(state["dry_counter"], 1)

        resumed = sm.evaluate(state, 0.3, now=1_600)
        self.assertIsNone(resumed["event"])
        self.assertEqual(resumed["state"]["status"], "RAINING")
        self.assertEqual(resumed["state"]["dry_counter"], 0)

    def test_dry_confirm_one_stops_immediately(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        result = sm.evaluate(state, 0.0, now=1_300, dry_confirm=1)
        self.assertEqual(result["event"], sm.EVENT_RAIN_STOP)

    def test_start_cooldown_suppresses_and_stop_is_also_skipped(self) -> None:
        state = sm.new_state()
        state["last_event_time"] = 1_000
        result = sm.evaluate(state, 0.5, now=1_060, min_push_interval_minutes=30)
        self.assertEqual(result["event"], sm.EVENT_RAIN_START)
        self.assertFalse(result["push"])
        self.assertFalse(result["state"]["rain_announced"])
        self.assertIn("冷却", result["reason"])

        state = result["state"]
        for offset in (300, 600):
            state = sm.evaluate(state, 0.0, now=1_060 + offset)["state"]
        final = sm.evaluate(state, 0.0, now=1_060 + 900)
        self.assertEqual(final["event"], sm.EVENT_RAIN_STOP)
        self.assertFalse(final["push"])
        self.assertIn("未成功播报", final["reason"])

    def test_start_cooldown_expires(self) -> None:
        state = sm.new_state()
        state["last_event_time"] = 1_000
        result = sm.evaluate(state, 0.5, now=1_000 + 31 * 60, min_push_interval_minutes=30)
        self.assertTrue(result["push"])
        self.assertTrue(result["state"]["rain_announced"])

    def test_cooldown_zero_never_suppresses(self) -> None:
        state = sm.new_state()
        state["last_event_time"] = 1_000
        result = sm.evaluate(state, 0.5, now=1_001, min_push_interval_minutes=0)
        self.assertTrue(result["push"])

    def test_evaluate_does_not_mutate_input(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        snapshot = copy.deepcopy(state)
        sm.evaluate(state, 0.0, now=1_300)
        self.assertEqual(state, snapshot)

    def test_invalid_intensity_is_treated_as_dry(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        for bad in (None, "abc", float("nan"), -1):
            result = sm.evaluate(state, bad, now=1_300)
            self.assertEqual(result["state"]["last_intensity"], 0.0)


class StateFileTests(unittest.TestCase):
    def test_legacy_state_infers_rain_announced(self) -> None:
        normalized = sm.normalize_state(
            {"status": "RAINING", "last_event_time": 123, "dry_counter": 1}
        )
        self.assertTrue(normalized["rain_announced"])
        self.assertEqual(normalized["last_event_time"], 123)

    def test_invalid_values_are_normalized(self) -> None:
        normalized = sm.normalize_state(
            {"status": "UNKNOWN", "last_event_time": -5, "dry_counter": "x"}
        )
        self.assertEqual(normalized["status"], "DRY")
        self.assertEqual(normalized["last_event_time"], 0)
        self.assertEqual(normalized["dry_counter"], 0)

    def test_save_load_roundtrip(self) -> None:
        state = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        state["location"] = {"latitude": 30.1, "longitude": 120.2, "source": "config"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime_state.json"
            sm.save_state(path, state)
            loaded = sm.load_state(path)
        self.assertEqual(loaded["status"], "RAINING")
        self.assertEqual(loaded["last_event_time"], 1_000)
        self.assertEqual(loaded["location"]["latitude"], 30.1)

    def test_missing_file_returns_initial_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loaded = sm.load_state(Path(tmp) / "nope.json")
        self.assertEqual(loaded, sm.new_state())

    def test_revert_transition_restores_control_fields(self) -> None:
        previous = sm.evaluate(sm.new_state(), 0.5, now=1_000)["state"]
        previous["last_intensity"] = 0.5
        result = sm.evaluate(previous, 0.0, now=1_300)
        reverted = sm.revert_transition(result["state"], previous)
        self.assertEqual(reverted["status"], "RAINING")
        self.assertEqual(reverted["dry_counter"], 0)
        self.assertTrue(reverted["rain_announced"])
        # 遥测数据保留最新值
        self.assertEqual(reverted["last_intensity"], 0.0)
        self.assertEqual(reverted["last_update_time"], 1_300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
