"""GUI integration checks without running or testing a Claude session."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from itertools import combinations
from unittest.mock import patch

from PyQt6.QtCore import QSettings, QThread
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QPushButton, QToolButton

import gui
import cli_routing
from codex_watcher import clean_config
from watcher_ui import AppSettingsDialog


class IsolatedWindow(gui.MainWindow):
    def _build_codex_worker(self):
        super()._build_codex_worker()
        self.codex_app_worker.activities = None

    def _build_updater(self):
        self.updater_thread = QThread(self)

    def _api_apply(self):
        pass

    def _stop_local_api(self):
        # A test window never started a server. Its close must not delete the
        # discovery file owned by the real installed Auto-Continue instance.
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
                       self.window.sig_codex_start, self.window.sig_codex_stop,
                       self.window.sig_codex_app_start, self.window.sig_codex_app_stop):
            signal.disconnect()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.app.processEvents()

    def select(self, *providers):
        for provider, check in self.window.target_checks.items():
            check.setChecked(provider in providers)
        if providers:
            self.window._set_provider_view(providers[0])
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
        self.window._set_provider_view('codex')
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
        self.assertEqual(self.window.table.rowCount(), 1)
        self.window._set_provider_view('claude')
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertTrue(self.window.table.item(0, 0).text().startswith('[Claude CLI]'))
        self.assertEqual(len(self.window._cli_rows['codex']), 1)

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

    def test_permission_dialog_defaults_cancel_and_independent_persistence(self):
        original_app = dict(self.window._codex_app_config)
        original_claude = self.store.value('auto_answer_permission', None)
        def inspect_cancel(dialog):
            mode = dialog.findChild(QComboBox, 'codex_permission_mode')
            approve = dialog.findChild(QCheckBox, 'codex_auto_approve')
            self.assertEqual(mode.currentData(), 'full-access')
            self.assertTrue(approve.isChecked())
            mode.setCurrentIndex(mode.findData('ask-for-approval'))
            approve.setChecked(False)
            return QDialog.DialogCode.Rejected
        with patch.object(QDialog, 'exec', inspect_cancel):
            self.window._open_codex_settings('codex')
        self.assertEqual(self.window._codex_config['permission_mode'], 'full-access')
        self.assertTrue(self.window._codex_config['auto_approve'])
        def accept(dialog):
            mode = dialog.findChild(QComboBox, 'codex_permission_mode')
            mode.setCurrentIndex(mode.findData('ask-for-approval'))
            dialog.findChild(QCheckBox, 'codex_auto_approve').setChecked(False)
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', accept):
            self.window._open_codex_settings('codex')
        self.window._load_cli_settings()
        self.assertEqual(self.window._codex_config['permission_mode'], 'ask-for-approval')
        self.assertFalse(self.window._codex_config['auto_approve'])
        self.assertEqual(self.window._codex_app_config, original_app)
        self.assertEqual(self.store.value('auto_answer_permission', None), original_claude)
        def accept_app(dialog):
            mode = dialog.findChild(QComboBox, 'codex_permission_mode')
            approve = dialog.findChild(QCheckBox, 'codex_auto_approve')
            self.assertEqual(mode.currentData(), 'full-access')
            self.assertTrue(approve.isChecked())
            mode.setCurrentIndex(mode.findData('approve-for-me'))
            approve.setChecked(False)
            return QDialog.DialogCode.Accepted
        with patch.object(QDialog, 'exec', accept_app):
            self.window._open_codex_settings('codex_app')
        self.window._load_cli_settings()
        self.assertEqual(self.window._codex_app_config['permission_mode'], 'approve-for-me')
        self.assertFalse(self.window._codex_app_config['auto_approve'])
        self.assertEqual(self.window._codex_config['permission_mode'], 'ask-for-approval')
        self.assertEqual(self.store.value('auto_answer_permission', None), original_claude)

    def test_front_panel_codex_controls_save_only_codex_parameters(self):
        self.select("codex")
        original = self.window.interval_spin.value()
        self.assertTrue(self.window.target_cards['claude'].isHidden())
        self.window._codex_spins["poll"].setValue(12)
        self.window._codex_spins["retry"].setValue(45)
        self.window._codex_config["dry_run"] = True
        self.window._apply_codex_config()
        self.assertEqual(self.window._codex_config["poll"], 12)
        self.assertEqual(self.window._codex_config["retry"], 45)
        self.assertTrue(self.window._codex_config["dry_run"])
        self.assertEqual(self.window.interval_spin.value(), original)
        self.select("claude", "codex")
        self.assertFalse(self.window.target_cards['claude'].isHidden())
        self.assertEqual(self.window._codex_spins["poll"].value(), 12)
        self.assertFalse(any('dry-run' in check.text().lower()
                             for check in self.window.findChildren(QCheckBox)))

    def test_desktop_selection_settings_and_actions_are_independent(self):
        self.select('codex_app')
        self.assertEqual(self.window._selected_clis(), ('codex_app',))
        self.window._codex_app_spins['poll'].setValue(4)
        self.assertEqual(self.window._codex_app_config['poll'], 4)
        self.assertEqual(self.window._codex_config['poll'], 10)
        self.window._on_codex_override('same', 'effort', 'high', 'codex_app')
        self.assertEqual(self.window._codex_app_config['effort_overrides'], {'same': 'high'})
        self.assertEqual(self.window._codex_config['effort_overrides'], {})
        emitted = []
        self.window.sig_codex_app_fire.connect(lambda h: emitted.append(('app', h)))
        self.window.sig_codex_fire.connect(lambda h: emitted.append(('cli', h)))
        self.window._cli_action({'provider': 'codex_app', 'hwnd': 321, 'title': 'same'}, 'fire')
        self.assertEqual(emitted, [('app', 321)])
        self.window._cli_action({'provider': 'codex_app', 'hwnd': 321, 'title': 'same'}, 'exclude')
        self.assertEqual(self.window._codex_app_config['excluded'], ['same'])
        self.assertEqual(self.window._codex_config['excluded'], [])
        self.assertEqual(self.window._excluded_titles, [])
        self.select('claude', 'codex', 'codex_app')
        self.assertEqual(self.window._selected_clis(), ('claude', 'codex', 'codex_app'))
        self.window._save_cli_settings()
        self.window._codex_surfaces = 'cli'
        self.window._codex_app_config = clean_config({})
        self.window._load_cli_settings()
        self.assertEqual(self.window._codex_surfaces, 'all')
        self.assertEqual(self.window._codex_app_config['poll'], 4)

    def test_desktop_row_has_its_own_label_and_controls(self):
        self.select('codex_app')
        row = {'provider': 'codex_app', 'hwnd': 321, 'title': 'probe', 'status': 'idle',
               'reset_utc': None, 'last_sent_utc': None, 'excluded': False, 'model': '', 'effort': ''}
        self.window._codex_app_config['effort_overrides']['probe'] = 'high'
        self.window._on_codex_app_snapshot([row])
        self.assertTrue(self.window.table.item(0, 0).text().startswith('[Codex EXE]'))
        self.assertEqual(self.window.table.cellWidget(0, 6).currentData(), 'high')
        row['status'] = 'quota_busy'
        self.window._on_codex_app_snapshot([row])
        self.assertEqual(self.window.table.item(0, 1).text(), 'Usage limit · App retrying')

    def test_recent_activity_limit_is_independent_and_persisted(self):
        self.assertEqual(self.window.codex_recent_spin.value(), 5)
        self.window.codex_recent_spin.setValue(12)
        self.assertEqual(self.window._codex_app_config['recent_limit'], 12)
        self.assertEqual(self.window._codex_config['recent_limit'], 5)
        self.window._codex_app_config = clean_config({})
        self.window._load_cli_settings()
        self.assertEqual(self.window.codex_recent_spin.value(), 12)

    def test_all_background_activities_have_editable_session_settings(self):
        self.select('codex_app')
        rows = [{'thread_id': str(i), 'title': 'Same title', 'cwd': 'project/' + str(i),
                 'status': 'running' if i == 0 else 'completed',
                 'updated_utc': datetime(2026, 10, 4, tzinfo=timezone.utc)} for i in range(5)]
        self.window._on_codex_recent_snapshot(rows, '')
        self.assertFalse(hasattr(self.window, 'codex_recent_table'))
        self.assertEqual(self.window.table.rowCount(), 5)
        self.assertEqual(self.window.table.item(0, 1).text(), 'Running')
        for index in range(5):
            for column in (5, 6, 7):
                self.assertIsNotNone(self.window.table.cellWidget(index, column))
            combo = self.window.table.cellWidget(index, 5)
            combo.setCurrentIndex(combo.findData('gpt-6-astra' if index % 2 else 'gpt-6.1-sol'))
            effort = self.window.table.cellWidget(index, 6)
            effort.setCurrentIndex(effort.findData('high' if index % 2 else 'xhigh'))
        for index in range(5):
            self.assertEqual(self.window._codex_app_config['sessions'][str(index)]['model'],
                             'gpt-6-astra' if index % 2 else 'gpt-6.1-sol')
        self.assertEqual(self.window._codex_app_config['model_overrides'], {})
        self.window._load_cli_settings()
        self.assertEqual(self.window.table.cellWidget(1, 5).currentData(), 'gpt-6-astra')
        self.window.table.selectRow(1)
        self.window._on_codex_recent_snapshot(rows[::-1], '')
        self.assertEqual(self.window.table.currentRow(), 3)
        self.assertEqual(self.window.table.item(3, 0).text(), '[Codex] Same title\nproject/1')
        self.window._on_codex_app_running(True)
        self.assertIn('Monitoring · 5', self.window.codex_recent_status.text())
        self.window._on_codex_app_running(False)
        self.assertIn('paused', self.window.codex_recent_status.text())
        self.window._on_codex_recent_snapshot([], 'Local data unavailable')
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertEqual(self.window.codex_recent_status.text(), 'Local data unavailable')
        self.window._set_provider_view('codex')
        self.assertTrue(self.window.recent_refresh_btn.isHidden())

    def test_unbound_activity_can_be_configured_without_dispatching_input(self):
        self.select('codex_app')
        row = dict(provider='codex_app', hwnd=123, title='Same title', status='idle',
                   reset_utc=None, last_sent_utc=None, excluded=False, model='', effort='')
        self.window._on_codex_app_snapshot([row])
        self.assertIsNotNone(self.window.table.cellWidget(0, 7))
        self.window._on_codex_recent_snapshot([dict(thread_id='chat-1', title='Same title',
            status='completed', cwd='project', updated_utc=datetime.now(timezone.utc))], '')
        self.assertEqual(self.window.table.rowCount(), 2)
        for column in (5, 6, 7):
            self.assertIsNotNone(self.window.table.cellWidget(0, column))
            self.assertIsNotNone(self.window.table.cellWidget(1, column))
        signals = []
        for name in ('fire', 'skip', 'cooldown'):
            getattr(self.window, 'sig_codex_app_' + name).connect(lambda h: signals.append(h))
        record = self.window._latest_snapshot[0]
        for action in ('fire', 'skip', 'cooldown'):
            self.window._cli_action(record, action)
        self.assertEqual(signals, [])
        self.window._cli_action(record, 'exclude')
        self.assertTrue(self.window._codex_app_config['sessions']['chat-1']['excluded'])
        self.assertEqual(self.window._codex_app_config['excluded'], [])
        self.window._cli_action(record, 'include')
        self.assertFalse(self.window._codex_app_config['sessions']['chat-1']['excluded'])

    def test_live_window_is_merged_only_by_exact_session_id(self):
        self.select('codex_app')
        rows = [dict(thread_id=str(i), title='Same title', status='completed', cwd='project') for i in range(5)]
        self.window._on_codex_recent_snapshot(rows, '')
        live = dict(provider='codex_app', thread_id='3', hwnd=123, title='Same title',
                    status='idle', reset_utc=None, last_sent_utc=None, excluded=False, model='', effort='')
        self.window._on_codex_app_snapshot([live])
        self.assertEqual(self.window.table.rowCount(), 5)
        emitted = []
        self.window.sig_codex_app_fire.disconnect()
        self.window.sig_codex_app_fire.connect(emitted.append)
        self.assertEqual([b.text() for b in self.window.table.cellWidget(0, 7).findChildren(QPushButton)], ['Open'])
        self.assertEqual([b.text() for b in self.window.table.cellWidget(3, 7).findChildren(QPushButton)], ['Now'])
        self.window._cli_action(self.window._latest_snapshot[3], 'fire')
        self.assertEqual(emitted, [123])
        self.window._on_codex_app_snapshot([dict(live, thread_id='')])
        self.assertEqual(self.window.table.rowCount(), 6)

    def test_background_open_uses_session_id_and_does_not_send_a_prompt(self):
        self.select('codex_app')
        ident = '01a10524-ea41-7de0-9771-b0c2af6a8ac9'
        self.window._on_codex_recent_snapshot([dict(thread_id=ident, title='Same title', status='completed')], '')
        with patch('codex_gui.QDesktopServices.openUrl', return_value=True) as open_url:
            self.window._cli_action(self.window._latest_snapshot[0], 'open')
        self.assertEqual(open_url.call_args.args[0].toString(), 'codex://threads/' + ident)

    def test_link_dispatch_requires_one_unbound_window_and_preserves_session_id(self):
        self.select('codex_app')
        self.window.sig_codex_app_bind.disconnect()
        emitted = []
        self.window.sig_codex_app_bind.connect(emitted.append)
        self.window._cli_running['codex_app'] = True
        native = dict(provider='codex_app', hwnd=123, title='Same title', identity='native',
                      thread_hint='hint', thread_id='')
        self.window._cli_rows['codex_app'] = [native]
        row = dict(provider='codex_app', thread_id='two', title='Same title')
        self.window._cli_action(row, 'link')
        self.assertEqual(emitted, [dict(hwnd=123, thread_id='two', identity='native', thread_hint='hint')])
        self.window._cli_rows['codex_app'].append(dict(native, hwnd=456))
        self.window._cli_action(row, 'link')
        self.assertEqual(len(emitted), 1)

    def test_model_popup_survives_status_refresh_and_updates_after_close(self):
        self.select('codex_app')
        self.window.show()
        row = dict(thread_id='one', title='Same title', status='completed', cwd='project')
        self.window._on_codex_recent_snapshot([row], '')
        combo = self.window.table.cellWidget(0, 5)
        combo.showPopup()
        self.app.processEvents()
        self.assertTrue(combo.view().isVisible())
        self.window._on_codex_recent_snapshot([dict(row, status='running')], '')
        self.assertIs(self.window.table.cellWidget(0, 5), combo)
        self.assertTrue(combo.view().isVisible())
        combo.hidePopup()
        self.window._on_codex_recent_snapshot([dict(row, status='running')], '')
        self.assertEqual(self.window.table.item(0, 1).text(), 'Running')

    def test_model_focus_loss_does_not_rebuild_and_custom_text_is_saved(self):
        self.select('codex_app')
        self.window._on_codex_recent_snapshot([dict(thread_id='one', title='Same title',
                                                   status='completed')], '')
        combo = self.window.table.cellWidget(0, 5)
        combo.lineEdit().editingFinished.emit()
        self.assertIs(self.window.table.cellWidget(0, 5), combo)
        combo.setEditText('future-model')
        self.assertEqual(combo.currentData(), '')  # Still the Keep current item.
        combo.lineEdit().editingFinished.emit()
        self.assertEqual(self.window._codex_app_config['sessions']['one']['model'], 'future-model')
        self.assertEqual(self.window.table.cellWidget(0, 5).currentText(), 'future-model')

    def test_polling_preserves_unfinished_custom_model_text(self):
        self.select('codex_app')
        self.window.show()
        row = dict(thread_id='one', title='Same title', status='completed')
        self.window._on_codex_recent_snapshot([row], '')
        combo = self.window.table.cellWidget(0, 5)
        combo.lineEdit().setFocus()
        self.app.processEvents()
        combo.setEditText('future-model')
        combo.lineEdit().setModified(True)
        self.window._on_codex_recent_snapshot([dict(row, status='running')], '')
        self.assertIs(self.window.table.cellWidget(0, 5), combo)
        self.assertEqual(combo.currentText(), 'future-model')
        combo.lineEdit().editingFinished.emit()
        self.assertEqual(self.window._codex_app_config['sessions']['one']['model'], 'future-model')

    def test_after_finish_is_saved_by_id_and_preserves_spent_loops(self):
        self.select('codex_app')
        import codex_gui
        def edit_dialog(dlg):
            dlg.findChild(gui.QPlainTextEdit).setPlainText('Next task')
            self.window._on_codex_session_loops_spent('one', 0)
            dlg.findChild(gui.QDialogButtonBox).accepted.emit()
            return QDialog.DialogCode.Accepted
        with patch.object(codex_gui.QDialog, 'exec', edit_dialog):
            self.window._on_codex_after_finish('Same title', 'codex_app', 'one')
        self.assertEqual(self.window._codex_app_config['sessions']['one'],
                         {'after_finish': 'Next task', 'loops': 1})
        self.assertNotIn('Same title', self.window._codex_app_config['after_finish'])
        self.assertNotIn('two', self.window._codex_app_config['sessions'])

    def test_activity_timestamp_updates_without_replacing_live_controls(self):
        self.select('codex_app')
        row = dict(thread_id='chat', title='Chat', status='running', cwd='project',
                   updated_utc=datetime(2026, 10, 4, tzinfo=timezone.utc))
        self.window._on_codex_recent_snapshot([row], '')
        self.window._on_codex_app_snapshot([dict(provider='codex_app', hwnd=123, title='Window',
            status='idle', reset_utc=None, last_sent_utc=None, excluded=False, model='', effort='')])
        combo = self.window.table.cellWidget(1, 5)
        before = self.window.table.item(0, 4).text()
        self.window._on_codex_recent_snapshot([dict(row, updated_utc=datetime(2026, 10, 5, tzinfo=timezone.utc))], '')
        self.assertNotEqual(self.window.table.item(0, 4).text(), before)
        self.assertIs(self.window.table.cellWidget(1, 5), combo)

    def test_switched_codex_terminal_does_not_retain_a_claude_label(self):
        self.window._watch_targets = 'both'
        row = {'hwnd': 321, 'title': 'probe', 'status': 'idle', 'reset_utc': None,
               'last_sent_utc': None, 'excluded': False, 'model': '', 'effort': ''}
        with patch.dict(cli_routing._providers, {321: 'codex'}):
            self.window._on_claude_snapshot([row])
        self.assertEqual(self.window._cli_rows['claude'], [])
        self.assertEqual(self.window.table.rowCount(), 0)

    def test_tabs_show_one_type_and_preserve_all_monitor_selections(self):
        self.window.show()
        self.assertEqual([self.window.provider_tabs.tabText(i) for i in range(3)],
                         ['Codex EXE', 'Codex CLI', 'Claude CLI'])
        providers = tuple(self.window.target_checks)
        for count in range(4):
            for selected in combinations(providers, count):
                with self.subTest(selected=selected):
                    self.select(*selected)
                    self.assertEqual(set(self.window._selected_clis()), set(selected))
                    self.window._load_cli_settings()
                    self.assertEqual(set(self.window._selected_clis()), set(selected))
                    self.assertEqual(self.window.start_btn.isEnabled(), bool(selected))
                    starts = []
                    for name in ('claude_start', 'codex_start', 'codex_app_start',
                                 'claude_stop', 'codex_stop', 'codex_app_stop'):
                        getattr(self.window, 'sig_' + name).connect(lambda n=name: starts.append(n))
                    for view in providers:
                        self.window._set_provider_view(view)
                        self.app.processEvents()
                        self.assertEqual(set(self.window._selected_clis()), set(selected))
                        for provider, card in self.window.target_cards.items():
                            self.assertEqual(card.isVisible(), provider == view)
                            self.assertEqual(self.window.target_checks[provider].isChecked(), provider in selected)
                        self.assertEqual(self.store.value('gui_view_provider'), view)
                    self.assertEqual(starts, [])

    def test_compact_layout_has_always_visible_log_at_bottom(self):
        self.select('claude', 'codex_app', 'codex')
        self.window.resize(960, 620)
        self.window.show()
        self.app.processEvents()
        self.assertLessEqual(self.window.minimumSizeHint().height(), 620)
        self.assertTrue(self.window.log_view.isVisible())
        self.assertFalse(hasattr(self.window, 'log_toggle'))
        self.assertEqual(self.window.monitor_splitter.count(), 2)
        self.assertGreaterEqual(self.window.log_panel.geometry().bottom(),
                                self.window.monitor_splitter.height() - 2)
        self.assertEqual(set(self.window._selected_clis()), {'claude', 'codex_app', 'codex'})

    def test_no_selection_stops_all_watchers_and_persists(self):
        self.select('codex_app')
        self.window._start_selected_watchers()
        seen = []
        for provider, prefix in (('claude', 'claude'), ('codex', 'codex'), ('codex_app', 'codex_app')):
            getattr(self.window, 'sig_' + prefix + '_stop').connect(lambda p=provider: seen.append(p))
        self.select()
        self.assertCountEqual(seen, ['claude', 'codex', 'codex_app'])
        self.assertFalse(self.window._watch_requested)
        self.window._load_cli_settings()
        self.assertEqual(self.window._selected_clis(), ())
        self.assertFalse(self.window.start_btn.isEnabled())

    def test_legacy_selection_migrates_and_hidden_values_survive(self):
        self.store.setValue('watch_targets', 'both')
        self.store.setValue('codex_surfaces', 'app')
        self.window._load_cli_settings()
        self.assertTrue(self.window.target_checks['claude'].isChecked())
        self.assertTrue(self.window.target_checks['codex_app'].isChecked())
        self.assertFalse(self.window.target_checks['codex'].isChecked())
        self.window._codex_app_spins['poll'].setValue(8)
        self.select('codex')
        self.select('codex_app')
        self.assertEqual(self.window._codex_app_spins['poll'].value(), 8)
        self.assertEqual(self.window._codex_spins['poll'].value(), 10)

    def test_app_settings_are_global_and_cancel_does_not_commit(self):
        controls = {'autostart': self.window.autostart_check, 'boot': self.window.boot_check,
                    'keep_awake': self.window.keep_awake_check}
        dlg = AppSettingsDialog(self.window, controls)
        self.assertEqual(set(dlg.checks), set(controls))
        original = controls['autostart'].isChecked()
        dlg.checks['autostart'].setChecked(not original)
        dlg.reject()
        self.assertEqual(controls['autostart'].isChecked(), original)
        dlg.deleteLater()

    def test_claude_more_settings_are_scoped_and_exclude_codex_rows(self):
        self.window._latest_snapshot = [dict(title='Claude project', provider='claude'),
                                        dict(title='Codex project', provider='codex_app')]
        captured = {}
        real_dialog = gui.AdvancedDialog
        def create(parent, cfg, windows, patterns, auto, general, **kw):
            captured.update(windows=windows, scope=kw['scope'])
            dlg = real_dialog(parent, cfg, windows, patterns, auto, general, **kw)
            captured['tabs'] = [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())]
            return dlg
        with patch.object(gui, 'AdvancedDialog', side_effect=create), patch.object(real_dialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            self.window._open_advanced()
        self.assertEqual(captured['scope'], 'claude')
        self.assertEqual(captured['windows'], {'Claude project': 'Claude project'})
        self.assertEqual(captured['tabs'], ['Answering', 'Model recovery', 'Triggers', 'Local API'])

    def test_compact_row_menu_preserves_routing_and_pending_actions(self):
        self.select('codex_app')
        emitted = []
        self.window.sig_codex_app_skip.connect(lambda h: emitted.append(('app', h)))
        self.window.sig_skip.disconnect()
        self.window.sig_skip.connect(lambda h: emitted.append(('claude', h)))
        row = dict(provider='codex_app', hwnd=123, title='probe', status=gui.ST_PENDING,
                   reset_utc=None, last_sent_utc=None, excluded=False, model='', effort='')
        self.window._on_codex_app_snapshot([row])
        cell = self.window.table.cellWidget(0, 7)
        self.assertEqual([b.text() for b in cell.findChildren(QPushButton)], ['Now'])
        menu = cell.findChild(QToolButton).menu()
        skip = next(a for a in menu.actions() if a.text() == 'Skip pending continue')
        self.assertTrue(skip.isEnabled())
        skip.trigger()
        self.assertEqual(emitted, [('app', 123)])
        next(a for a in menu.actions() if a.text() == 'Exclude window').trigger()
        cell = self.window.table.cellWidget(0, 7)
        self.assertEqual([b.text() for b in cell.findChildren(QPushButton)], ['Include'])
        self.assertEqual(self.window._excluded_titles, [])
        cell.findChild(QPushButton).click()
        self.assertEqual(self.window._codex_app_config['excluded'], [])


if __name__ == "__main__":
    unittest.main()
