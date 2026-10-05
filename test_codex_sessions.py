"""ID-based settings and native binding boundaries, without native input."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PyQt6.QtWidgets import QApplication

import codex_app as app
from codex_sessions import session_options
from codex_watcher import CodexWatcher, State, clean_config
from test_codex_submission import NativeSurface


class SessionChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_ids_survive_rename_and_explicit_empty_overrides_legacy(self):
        config = clean_config(dict(model_overrides={'same': 'legacy'}, excluded=['same'],
            sessions={'one': {'model': '', 'excluded': False, 'effort': 'high'},
                      'two': {'model': 'gpt-6-astra'}}))
        self.assertEqual(session_options(config, 'one', 'same')['model'], '')
        self.assertFalse(session_options(config, 'one', 'same')['excluded'])
        self.assertEqual(session_options(config, 'one', 'renamed')['effort'], 'high')
        self.assertEqual(session_options(config, 'two', 'same')['model'], 'gpt-6-astra')
        self.assertEqual(session_options(config, '', 'same')['model'], 'legacy')

    def test_model_effort_and_spent_budget_use_only_bound_id(self):
        driver = SimpleNamespace(prepare=Mock(), apply_session_options=Mock(return_value=True),
                                 send_prompt=Mock(return_value=True))
        watcher = CodexWatcher(driver, provider='codex_app')
        watcher.config = clean_config(dict(sessions={
            'one': {'model': 'gpt-6.1-sol', 'effort': 'high', 'loops': 1},
            'two': {'model': 'gpt-6-astra', 'effort': 'xhigh', 'loops': 3}}))
        state = State(123, 'same', identity='native', thread_hint='hint', thread_id='two')
        window = SimpleNamespace()
        spent = []
        watcher.session_loops_spent.connect(lambda ident, left: spent.append((ident, left)))
        self.assertTrue(watcher._send(state, window, 'continue', datetime.now(timezone.utc),
                                     'after finish', 'after_finish'))
        driver.apply_session_options.assert_called_once_with(window, 'gpt-6-astra', 'xhigh')
        self.assertEqual(window.expected_thread_hint, 'hint')
        self.assertEqual(spent, [('two', 2)])
        self.assertEqual(watcher.config['sessions']['one']['loops'], 1)
        self.assertEqual(watcher.config['after_finish_loops'], {})

    def test_same_title_context_change_holds_model_and_pending_send(self):
        surface = NativeSurface()
        surface.view.thread_hint = 'first'
        surface.window.expected_thread_hint = 'first'
        surface.button.IsEnabled = False
        with patch.object(app, 'read_text', side_effect=lambda _: surface.view), \
             patch.object(app.ctypes, 'windll', SimpleNamespace(user32=surface.user)), \
             patch.object(app.time, 'sleep'):
            receipt = app.send_prompt(surface.window, 'continue').submission
            self.assertIsNotNone(receipt)
            surface.view.thread_hint = 'second'
            surface.button.IsEnabled = True
            self.assertFalse(app.apply_session_options(surface.window, 'gpt-6-astra', 'high'))
            self.assertEqual(app.poll_submission(surface.window, receipt).status, 'held')
            self.assertEqual(surface.clicks, 0)
            surface.view.user_turn, surface.view.user_text = 'new', 'continue'
            self.assertFalse(app._submitted(surface.view, receipt))

    def test_explicit_link_is_stale_checked_and_dropped_after_context_switch(self):
        view = app.View('native', 'same', app.Screen(identified=True, ready=True), thread_hint='first')
        window = SimpleNamespace(Name='same', hwnd=123)
        driver = SimpleNamespace(read_text=Mock(return_value=view), window_from_handle=Mock(return_value=window),
            terminal_handles=Mock(return_value=[123]), session_key=Mock(return_value='native'),
            inspect_screen=lambda text, now: text.screen)
        watcher = CodexWatcher(driver, provider='codex_app')
        watcher.running = True
        watcher.activities = SimpleNamespace(thread=lambda ident: {'id': ident, 'title': 'same'},
                                              thread_id_for_title=lambda title: '')
        watcher._refresh_recent_activities = Mock()
        watcher.states[123] = State(123, 'same', identity='native', thread_hint='first',
                                   retry_attempts=4, seen_running=True, submission=object())
        request = dict(hwnd=123, thread_id='two', identity='native', thread_hint='stale')
        watcher.bind_session(request)
        self.assertEqual(watcher.session_bindings, {})
        request['thread_hint'] = 'first'
        watcher.bind_session(request)
        state = watcher.states[123]
        self.assertEqual((state.thread_id, state.retry_attempts, state.seen_running, state.submission),
                         ('two', 0, False, None))
        watcher.tick()
        self.assertEqual(watcher.states[123].thread_id, 'two')
        view.thread_hint = 'second'
        watcher.tick()
        self.assertEqual(watcher.session_bindings, {})
        self.assertEqual(watcher.states[123].thread_id, '')
        self.assertIsNot(watcher.states[123], state)
