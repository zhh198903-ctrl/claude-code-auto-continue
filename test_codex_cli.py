"""Codex-only checks. Fixtures are captured from the native Windows CLI."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication

import codex_cli as cli
from codex_watcher import CodexWatcher, State, clean_config

FIXTURES = Path(__file__).parent / "tests" / "fixtures" / "codex"
NOW = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def capture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class NativeScreens(unittest.TestCase):
    def test_idle_and_healthy_status_do_not_schedule(self):
        for name in ("idle.txt", "status.txt", "quota_recovered.txt"):
            s = cli.inspect_screen(capture(name), NOW)
            self.assertTrue(s.identified and s.ready)
            self.assertFalse(s.draft or s.blocked or s.running)
            self.assertEqual(s.error_kind, "")
            self.assertIsNone(s.reset_utc)

    def test_native_busy_and_completed(self):
        self.assertTrue(cli.inspect_screen(capture("busy.txt")).running)
        s = cli.inspect_screen(capture("completed.txt"))
        self.assertFalse(s.running)
        self.assertTrue(s.completion_id)

    def test_native_draft_and_all_pickers_hold(self):
        self.assertTrue(cli.inspect_screen(capture("draft.txt")).draft)
        for name in ("model_picker.txt", "effort_picker.txt", "advanced_effort.txt"):
            s = cli.inspect_screen(capture(name))
            self.assertTrue(s.identified and s.blocked, name)
            self.assertFalse(s.ready, name)

    def test_native_warning_footer_preserves_draft_guard(self):
        text = capture("warning_draft.txt")
        s = cli.inspect_screen(text)
        self.assertTrue(s.identified and s.ready and s.draft)
        self.assertEqual(s.error_kind, "")
        self.assertFalse(cli.inspect_screen(text + "\nPS C:\\probe> ").identified)
        with patch.object(cli, "read_text", return_value=text), patch.object(cli.auto, "SendKeys") as send:
            self.assertFalse(cli.send_prompt(object(), "continue"))
            send.assert_not_called()

    def test_warning_footer_keeps_idle_and_network_detection(self):
        hint = "\n⚠ 1 warning · f2 to view"
        s = cli.inspect_screen(capture("idle.txt") + hint)
        self.assertTrue(s.identified and s.ready)
        self.assertFalse(s.draft)
        self.assertEqual(cli.inspect_screen(capture("network.txt") + hint).error_kind, "network")

    def test_native_quota_and_network(self):
        s = cli.inspect_screen(capture("quota.txt"), NOW)
        self.assertEqual(s.error_kind, "quota")
        self.assertIsNotNone(s.reset_utc)
        # A same-day reset already passed stays due, not tomorrow.
        self.assertLess(s.reset_utc, NOW)
        self.assertEqual(cli.inspect_screen(capture("network.txt")).error_kind, "network")
        self.assertEqual(cli.inspect_screen(capture("default_quota.txt")).error_kind, "quota")

    def test_shell_scrollback_is_not_live_codex(self):
        self.assertFalse(cli.inspect_screen(capture("idle.txt") + "\nPS C:\\probe> ").identified)
        self.assertFalse(cli.inspect_screen("OpenAI Codex (v0.159.3)\nPS> ").identified)
        self.assertFalse(cli.inspect_screen(capture("model_picker.txt") + "\nPS C:\\probe> ").identified)

    def test_native_picker_stays_identifiable_without_startup_scrollback(self):
        text = capture("model_picker.txt").split("Select Model and Effort", 1)[1]
        s = cli.inspect_screen("Select Model and Effort" + text)
        self.assertTrue(s.identified and s.blocked)
        self.assertFalse(s.ready)

    def test_native_future_date_quota(self):
        s = cli.inspect_screen(capture("weekly_quota.txt"), NOW)
        self.assertEqual(s.error_kind, "quota")
        self.assertEqual(s.reset_utc.astimezone().strftime("%Y-%m-%d %H:%M"), "2026-10-08 18:02")

    def test_past_error_is_not_replayed(self):
        self.assertEqual(cli.inspect_screen(capture("quota_recovered.txt")).error_kind, "")
        text = capture("quota.txt").replace("› Ask Codex to do anything", "› continue\n\n› Ask Codex to do anything")
        self.assertEqual(cli.inspect_screen(text).error_kind, "")

    def test_unknown_error_and_bad_reset_require_attention(self):
        text = capture("network.txt").replace("unexpected status 503 Service Unavailable", "authentication expired 401")
        self.assertEqual(cli.inspect_screen(text).error_kind, "attention")
        text = capture("quota.txt").replace("5:33 PM", "25:99")
        self.assertIsNone(cli.inspect_screen(text).reset_utc)

    def test_picker_contains_real_labels_and_selected_row(self):
        rows = cli.picker_rows(capture("model_picker.txt"), "Select Model and Effort")
        self.assertEqual(rows[0][0], 1)
        self.assertTrue(rows[0][2])
        self.assertTrue(rows[0][1].startswith("GPT-6.1-Sol"))

    def test_sender_never_types_into_draft_busy_or_menu(self):
        for name in ("draft.txt", "busy.txt", "model_picker.txt", "effort_picker.txt"):
            with patch.object(cli, "read_text", return_value=capture(name)), patch.object(cli.auto, "SendKeys") as send:
                self.assertFalse(cli.send_prompt(object(), "continue"))
                send.assert_not_called()

    def test_dry_run_and_multiline(self):
        with patch.object(cli, "read_text", return_value=capture("idle.txt")), patch.object(cli.auto, "SendKeys") as send:
            self.assertTrue(cli.send_prompt(object(), "continue", dry_run=True))
            self.assertFalse(cli.send_prompt(object(), "line1\nline2"))
            send.assert_not_called()


class WatcherState(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = CodexWatcher()
        self.w.config["buffer"] = 20
        self.st = State(1, "probe")
        self.send = patch.object(cli, "send_prompt", return_value=True).start()
        self.addCleanup(patch.stopall)

    def observe(self, name=None, **kwargs):
        self.st.screen = cli.inspect_screen(capture(name), NOW) if name else cli.Screen(identified=True, ready=True, **kwargs)
        self.w._observe(self.st, object(), NOW)

    def test_due_quota_sends_once_then_cools_down(self):
        self.observe("quota.txt")
        self.send.assert_called_once()
        self.assertEqual(self.st.status, "sent")
        self.observe("quota.txt")
        self.send.assert_called_once()
        self.assertEqual(self.st.status, "cooldown")

    def test_future_quota_honors_buffer(self):
        self.observe(error_kind="quota", error_id="q", reset_utc=NOW - timedelta(seconds=10))
        self.assertEqual(self.st.status, "pending")
        self.send.assert_not_called()

    def test_skip_cancels_same_error(self):
        self.w.states[1] = self.st
        self.observe(error_kind="quota", error_id="q", reset_utc=NOW + timedelta(minutes=1))
        self.w.skip(1)
        self.observe(error_kind="quota", error_id="q", reset_utc=NOW - timedelta(minutes=1))
        self.send.assert_not_called()

    def test_retry_interval_and_cap_survive_brief_idle(self):
        self.w.config["max_retries"] = 1
        self.observe("network.txt")
        self.send.assert_called_once()
        self.observe()
        self.observe("network.txt")
        self.assertEqual(self.st.status, "attention")
        self.send.assert_called_once()

    def test_retry_waits_and_success_rearms_budget(self):
        self.observe("network.txt")
        self.observe("network.txt")
        self.send.assert_called_once()
        self.observe(completion_id="new completion")
        self.assertEqual(self.st.retry_attempts, 0)
        self.observe("network.txt")
        self.assertEqual(self.send.call_count, 2)

    def test_send_failure_does_not_consume_reset(self):
        self.send.return_value = False
        self.observe("quota.txt")
        self.assertEqual(self.st.consumed_quota, "")
        self.assertIsNotNone(self.st.reset_utc)

    def test_excluded_draft_menu_and_running_never_send(self):
        for name in ("draft.txt", "busy.txt", "model_picker.txt"):
            self.observe(name)
        self.w.config["excluded"] = ["probe"]
        self.observe("quota.txt")
        self.send.assert_not_called()

    def test_after_finish_requires_new_turn_and_spends_own_budget(self):
        self.w.config["after_finish"]["probe"] = "Continue next task"
        self.w.config["after_finish_loops"]["probe"] = 1
        self.observe("completed.txt")
        self.send.assert_not_called()
        self.observe(completion_id="new")
        self.w._observe(self.st, object(), NOW + timedelta(seconds=6))
        self.send.assert_called_once_with(unittest.mock.ANY, "Continue next task", dry_run=False)
        self.assertEqual(self.w.config["after_finish_loops"]["probe"], 0)

    def test_two_watchers_share_no_configuration_or_state(self):
        other = CodexWatcher()
        self.w.config["excluded"].append("probe")
        self.w.config["model_overrides"]["probe"] = "gpt-6.1-sol"
        self.w.states[1] = self.st
        self.assertEqual(other.config["excluded"], [])
        self.assertEqual(other.config["model_overrides"], {})
        self.assertEqual(other.states, {})

    def test_unrecognized_and_unreadable_windows_never_send(self):
        self.w.running = True
        self.w.states[1] = self.st
        with patch.object(cli, "terminal_handles", return_value=[1]), patch.object(cli, "window_from_handle", return_value=object()), patch.object(cli, "read_text", return_value=None):
            self.w.tick()
            self.assertEqual(self.st.status, "unknown")
        with patch.object(cli, "terminal_handles", return_value=[1]), patch.object(cli, "window_from_handle", return_value=object()), patch.object(cli, "read_text", return_value="PS> "):
            self.w.tick()
            self.assertEqual(self.w.states, {})
        self.send.assert_not_called()

    def test_configuration_is_defensive_and_copies_nested_values(self):
        source = {"poll": "bad", "buffer": -5, "excluded": ["x"], "after_finish": {"x": "a\nb"}, "model_overrides": {"x": "gpt-6.1-sol"}}
        result = clean_config(source)
        result["excluded"].append("y")
        self.assertEqual(source["excluded"], ["x"])
        self.assertEqual(result["buffer"], 0)
        self.assertEqual(result["after_finish"], {})


if __name__ == "__main__":
    unittest.main()
