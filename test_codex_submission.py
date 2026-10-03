"""Regression checks for native disabled Send and confirmed submission."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication
import codex_app as app
from codex_watcher import CodexWatcher, State
from test_codex_app import FIXTURES, Node, replay


class NativeSurface:
    """A controllable native view: Invoke can succeed without submitting."""
    def __init__(self):
        self.value = ''
        self.writes = self.clicks = 0
        self.ack_on_click = True
        self.foreground = 123
        self.window = SimpleNamespace(hwnd=123, expected_identity='chat',
                                      control=SimpleNamespace(SetActive=lambda: None))
        self.pattern = self
        self.composer = SimpleNamespace(GetValuePattern=lambda: self.pattern,
                                        SetFocus=lambda: None)
        self.button = SimpleNamespace(IsEnabled=True, IsOffscreen=False, ClassName='',
            GetPropertyValue=lambda _: '', GetInvokePattern=lambda: SimpleNamespace(Invoke=self.invoke))
        self.view = app.View('chat', 'probe', app.Screen(identified=True, ready=True),
                             self.composer, self.button, user_turn='old', user_text='earlier')
        self.user = SimpleNamespace(GetForegroundWindow=lambda: self.foreground,
                                    SetForegroundWindow=lambda _: None)

    @property
    def Value(self):
        return self.value

    def SetValue(self, value):
        self.writes += 1
        self.value = value

    def invoke(self):
        self.clicks += 1
        if self.ack_on_click:
            self.ack()

    def ack(self):
        self.view.user_text = self.value
        self.view.user_turn = 'new'
        self.value = '随心输入\n'

    def screen(self, **kwargs):
        self.view.screen = replace(self.view.screen, **kwargs)


class PendingSubmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.surface = NativeSurface()
        patch.object(app, 'read_text', side_effect=lambda _: self.surface.view).start()
        patch.object(app.ctypes, 'windll', SimpleNamespace(user32=self.surface.user)).start()
        patch.object(app.time, 'sleep', return_value=None).start()
        self.addCleanup(patch.stopall)
        self.watcher = CodexWatcher(app, provider='codex_app', label='Codex App')
        self.watcher.config.update(poll=1, retry=5, buffer=20, max_retries=1)
        self.state = State(123, 'probe', identity='chat')
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def observe(self, seconds=0):
        self.state.screen = self.surface.view.screen
        self.watcher._observe(self.state, self.surface.window, self.now + timedelta(seconds=seconds))

    def stage(self):
        return app.send_prompt(self.surface.window, 'continue')

    def test_disabled_send_waits_then_sends_once_without_retyping(self):
        s = self.surface
        s.button.IsEnabled = False
        s.screen(error_kind='quota_retry', error_id='quota')
        self.observe()
        self.assertEqual((s.writes, s.clicks, self.state.retry_attempts), (1, 0, 0))
        self.assertEqual(self.state.status, 'waiting_send')
        s.screen(draft=True)
        for seconds in (10, 50, 500, 5000):
            self.observe(seconds)
        self.assertEqual((s.writes, s.clicks, self.state.retry_attempts), (1, 0, 0))
        s.button.IsEnabled = True
        self.observe(5001)
        self.assertEqual((s.writes, s.clicks, self.state.retry_attempts), (1, 1, 1))
        self.assertEqual(self.state.status, 'sent')
        self.assertIsNone(self.state.submission)
        s.screen(draft=False, running=True)
        self.observe(5002)
        self.assertEqual(s.clicks, 1)

    def test_aria_disabled_and_native_grey_state_override_enabled(self):
        for flag in ('disabled=true', 'readonly=true;disabled=true;busy=false', 'busy=true'):
            with self.subTest(flag=flag):
                self.surface.button.GetPropertyValue = lambda _, flag=flag: flag
                self.assertFalse(app._send_available(self.surface.button))
        self.surface.button.GetPropertyValue = lambda _: ''
        self.surface.button.ClassName = 'size-token-button-composer opacity-50'
        outcome = self.stage()
        self.assertEqual(outcome.status, 'waiting_send')
        self.assertEqual(self.surface.clicks, 0)
        self.surface.button.ClassName = 'disabled:opacity-50 aria-disabled:cursor-default'
        self.assertTrue(app.poll_submission(self.surface.window, outcome.submission))
        self.assertEqual(self.surface.clicks, 1)

    def test_successful_invoke_without_ack_is_not_counted_or_repeated(self):
        self.surface.ack_on_click = False
        self.surface.screen(error_kind='quota_retry', error_id='quota')
        self.observe()
        self.assertEqual(self.state.status, 'confirming_send')
        self.assertEqual(self.surface.clicks, 1)
        self.assertEqual(self.state.retry_attempts, 0)
        self.observe(1)
        self.assertEqual(self.surface.clicks, 1)
        self.surface.ack()
        self.observe(2)
        self.assertEqual(self.state.retry_attempts, 1)
        self.assertEqual(self.state.status, 'sent')

    def test_user_edit_clear_or_conversation_switch_cancels_pending(self):
        for change in ('edit', 'clear', 'switch'):
            with self.subTest(change=change):
                self.surface.view.identity = 'chat'
                self.surface.value = ''
                self.surface.button.IsEnabled = False
                receipt = self.stage().submission
                self.assertIsNotNone(receipt)
                if change == 'switch':
                    self.surface.view.identity = 'another'
                else:
                    self.surface.value = 'user draft' if change == 'edit' else ''
                self.surface.button.IsEnabled = True
                outcome = app.poll_submission(self.surface.window, receipt)
                self.assertEqual(outcome.status, 'held')
                self.assertIsNone(outcome.submission)
                self.assertEqual(self.surface.clicks, 0)

    def test_unreadable_view_or_ambiguous_composer_retains_receipt(self):
        self.surface.button.IsEnabled = False
        receipt = self.stage().submission
        for view in (None, app.View('', 'probe', app.Screen(blocked=True))):
            with patch.object(app, 'read_text', return_value=view):
                outcome = app.poll_submission(self.surface.window, receipt)
            self.assertIs(outcome.submission, receipt)
            self.assertFalse(outcome)

    def test_open_menu_running_attention_or_reset_buffer_hold_pending(self):
        self.surface.button.IsEnabled = False
        receipt = self.stage().submission
        self.surface.button.IsEnabled = True
        for blocked in ({'blocked': True}, {'running': True}, {'error_kind': 'attention'}):
            original = self.surface.view.screen
            self.surface.screen(**blocked)
            self.assertFalse(app.poll_submission(self.surface.window, receipt))
            self.surface.view.screen = original
        self.assertFalse(app.poll_submission(self.surface.window, receipt, allow_click=False))
        self.assertEqual(self.surface.clicks, 0)
        self.assertTrue(app.poll_submission(self.surface.window, receipt))

    def test_future_reset_buffer_is_respected_for_already_staged_draft(self):
        self.surface.button.IsEnabled = False
        self.surface.screen(error_kind='quota_retry', error_id='q')
        self.observe()
        self.surface.button.IsEnabled = True
        self.surface.screen(error_kind='quota', reset_utc=self.now + timedelta(seconds=50), draft=True)
        self.observe(69)
        self.assertEqual(self.surface.clicks, 0)
        self.observe(70)
        self.assertEqual(self.surface.clicks, 1)

    def test_after_finish_loop_is_spent_only_after_submission(self):
        self.watcher.config['after_finish'] = {'probe': 'continue'}
        self.watcher.config['after_finish_loops'] = {'probe': 1}
        self.state.initialized = True
        self.state.seen_running = True
        self.state.idle_since = self.now - timedelta(seconds=6)
        self.surface.button.IsEnabled = False
        self.observe()
        self.assertIsNotNone(self.state.submission)
        self.assertEqual(self.watcher.config['after_finish_loops']['probe'], 1)
        self.surface.button.IsEnabled = True
        self.surface.screen(draft=True)
        self.observe(1)
        self.assertEqual(self.watcher.config['after_finish_loops']['probe'], 0)

    def test_changed_after_finish_setting_or_exclusion_cancels_receipt(self):
        self.surface.button.IsEnabled = False
        self.state.submission = self.stage().submission
        self.state.submit_action = 'after_finish'
        self.watcher.config['after_finish'] = {'probe': 'different prompt'}
        self.observe()
        self.assertIsNone(self.state.submission)
        self.state.submission = app.Submission('chat', 'continue', 'old')
        self.watcher.config['excluded'] = ['probe']
        self.observe()
        self.assertIsNone(self.state.submission)
        self.assertEqual(self.surface.clicks, 0)

    def test_cleared_composer_with_no_user_turn_is_not_submission(self):
        self.surface.ack_on_click = False
        receipt = self.stage().submission
        self.surface.value = '随心输入\n'
        self.assertFalse(app.poll_submission(self.surface.window, receipt))
        self.assertFalse(app._submitted(self.surface.view, receipt))

    def test_focus_loss_before_click_holds_the_owned_draft(self):
        self.surface.button.IsEnabled = False
        receipt = self.stage().submission
        self.surface.button.IsEnabled = True
        self.surface.foreground = 456
        self.assertFalse(app.poll_submission(self.surface.window, receipt))
        self.assertEqual(self.surface.clicks, 0)


class CapturedNativeSubmission(unittest.TestCase):
    def test_real_native_bubble_excludes_its_timestamp(self):
        window = replay('idle')
        captured = json.loads((FIXTURES / 'native_user_bubble.json').read_text(encoding='utf-8'))
        window.control.children.append(Node(captured, window.control))
        view = app.read_text(window)
        self.assertEqual(view.user_text, 'continue')
        self.assertTrue(view.user_turn)
        self.assertTrue(app._submitted(view, app.Submission(view.identity, 'continue', 'earlier-turn')))


if __name__ == '__main__':
    unittest.main()
