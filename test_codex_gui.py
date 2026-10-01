"""GUI integration checks without running or testing a Claude session."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QSettings, QThread
from PyQt6.QtWidgets import QApplication

import gui
from codex_watcher import clean_config


class IsolatedWindow(gui.MainWindow):
    def _build_updater(self):
        self.updater_thread = QThread(self)

    def _api_apply(self):
        pass


class CodexGuiChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        scratch = Path(__file__).parent / "ccdebug"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        store = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        store.setValue("autostart", False)
        self.store = store
        with patch.object(gui, "QSettings", return_value=store):
            self.window = IsolatedWindow()
        self.window._log_path = None
        # Exercise only dispatch boundaries; do not start Claude or live UIA.
        for signal in (self.window.sig_claude_start, self.window.sig_claude_stop,
                       self.window.sig_codex_start, self.window.sig_codex_stop):
            signal.disconnect()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.app.processEvents()

    def test_modes_dispatch_only_selected_drivers(self):
        seen = []
        for name in ("claude_start", "claude_stop", "codex_start", "codex_stop"):
            getattr(self.window, "sig_" + name).connect(lambda n=name: seen.append(n))
        for mode, expected in [("claude", ["claude_start", "codex_stop"]),
                               ("codex", ["claude_stop", "codex_start"]),
                               ("both", ["claude_start", "codex_start"])]:
            seen.clear()
            self.window._watch_targets = mode
            self.window._start_selected_watchers()
            self.assertEqual(seen, expected)
        self.window._stop_watchers()
        self.assertFalse(self.window._watch_requested)

    def test_codex_changes_preserve_claude_settings(self):
        self.store.setValue("interval", 47)
        self.store.setValue("model_overrides", '{"same":"opus"}')
        self.window._codex_config["poll"] = 7
        self.window._on_codex_override("same", "model", "gpt-6.1-sol")
        self.assertEqual(self.store.value("interval", type=int), 47)
        self.assertEqual(self.store.value("model_overrides", type=str), '{"same":"opus"}')
        self.assertEqual(json.loads(self.store.value("codex_config"))["poll"], 7)
        self.assertEqual(self.window._model_overrides, {})

    def test_codex_actions_never_emit_claude_commands(self):
        emitted = []
        self.window.sig_fire_now.connect(lambda h: emitted.append(("claude", h)))
        self.window.sig_codex_fire.disconnect()
        self.window.sig_codex_fire.connect(lambda h: emitted.append(("codex", h)))
        self.window._cli_action({"provider": "codex", "hwnd": 123, "title": "same"}, "fire")
        self.assertEqual(emitted, [("codex", 123)])
        self.window._cli_action({"provider": "codex", "hwnd": 123, "title": "same"}, "exclude")
        self.assertEqual(self.window._codex_config["excluded"], ["same"])
        self.assertEqual(self.window._excluded_titles, [])

    def test_snapshots_are_separate_and_table_controls_use_codex_values(self):
        row = {"hwnd": 123, "title": "probe", "status": "idle", "reset_utc": None,
               "last_sent_utc": None, "excluded": False, "model": "gpt-6.1-sol", "effort": "high"}
        self.window._watch_targets = "codex"
        self.window._codex_config["model_overrides"]["probe"] = "gpt-6.1-sol"
        self.window._codex_config["effort_overrides"]["probe"] = "high"
        self.window._on_codex_snapshot([dict(row, provider="codex")])
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertTrue(self.window.table.item(0, 0).text().startswith("[Codex CLI]"))
        self.assertEqual(self.window.table.cellWidget(0, 5).currentText(), "gpt-6.1-sol")
        self.assertEqual(self.window.table.cellWidget(0, 6).currentData(), "high")
        self.window._on_codex_override("probe", "effort", "xhigh")
        self.assertEqual(self.window.table.cellWidget(0, 6).currentData(), "xhigh")
        # Original snapshot object is preserved at the GUI boundary.
        self.window._on_claude_snapshot([row])
        self.assertNotIn("provider", row)
        self.assertEqual(self.window.table.rowCount(), 1)
        self.window._watch_targets = "both"
        self.window._merge_cli_rows()
        self.assertEqual(self.window.table.rowCount(), 2)

    def test_codex_settings_round_trip_independently(self):
        self.window._watch_targets = "both"
        self.window._codex_config.update(poll=8, dry_run=True)
        self.window._codex_config["after_finish"]["probe"] = "Next task"
        self.window._save_cli_settings()
        self.window._codex_config = clean_config({})
        self.window._load_cli_settings()
        self.assertEqual(self.window._watch_targets, "both")
        self.assertEqual(self.window._codex_config["poll"], 8)
        self.assertTrue(self.window._codex_config["dry_run"])
        self.assertEqual(self.window._codex_config["after_finish"]["probe"], "Next task")

    def test_front_panel_codex_controls_save_only_codex_parameters(self):
        self.window.watch_targets_combo.setCurrentIndex(self.window.watch_targets_combo.findData("codex"))
        original = self.window.interval_spin.value()
        self.assertFalse(self.window.interval_spin.isEnabled())
        self.window._codex_spins["poll"].setValue(12)
        self.window._codex_spins["retry"].setValue(45)
        self.window.codex_dry_run_check.setChecked(True)
        self.assertEqual(self.window._codex_config["poll"], 12)
        self.assertEqual(self.window._codex_config["retry"], 45)
        self.assertTrue(self.window._codex_config["dry_run"])
        self.assertEqual(self.window.interval_spin.value(), original)
        self.window.watch_targets_combo.setCurrentIndex(self.window.watch_targets_combo.findData("both"))
        self.assertTrue(self.window.interval_spin.isEnabled())
        self.assertEqual(self.window._codex_spins["poll"].value(), 12)


if __name__ == "__main__":
    unittest.main()
