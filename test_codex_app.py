"""Replay UIA trees captured from the isolated real desktop App."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import codex_app as app

FIXTURES = Path(__file__).parent / 'tests/fixtures/codex_app'

class Node:
    def __init__(self, data, parent=None):
        self.ControlTypeName = data['kind']
        self.Name = data['name']
        self.AutomationId = data['id']
        self.IsEnabled = data['enabled']
        self.IsOffscreen = data['offscreen']
        self.runtime = data['runtime']
        self.value = data['value']
        self.parent = parent
        self.children = [Node(c, self) for c in data['children']]

    def GetChildren(self):
        return self.children

    def GetParentControl(self):
        return self.parent

    def GetRuntimeId(self):
        return self.runtime

    def GetValuePattern(self):
        return SimpleNamespace(Value=self.value) if self.value is not None else None

    def Exists(self, *args):
        return True

    def DocumentControl(self, **kwargs):
        return self

def replay(name):
    node = Node(json.loads((FIXTURES / (name + '.json')).read_text(encoding='utf-8')))
    return app.Window(node, 123)

class DesktopScreens(unittest.TestCase):
    def test_native_high_and_extra_high_are_distinct(self):
        self.assertEqual(app._status_effort('自定义 极高，第 5 项，共 6 项。'), 'xhigh')
        self.assertEqual(app._status_effort('自定义 高，第 4 项，共 6 项。'), 'high')
        self.assertEqual(app._status_effort('6 Sol 标准，第 2 项，共 5 项。'), 'medium')
        self.assertEqual(app._status_effort('6 Sol 深度，第 3 项，共 5 项。'), 'high')
    def test_installed_app_identity_does_not_depend_on_chatgpt_title(self):
        self.assertTrue(app.is_desktop_executable(r'C:\Program Files\WindowsApps\OpenAI.Codex_26.930.0_x64__package\app\ChatGPT.exe'))
        self.assertFalse(app.is_desktop_executable(r'C:\Apps\ChatGPT\ChatGPT.exe'))
        self.assertFalse(app.is_desktop_executable(r'C:\Users\test\AppData\Local\OpenAI\Codex\bin\abc\codex.exe'))
        self.assertFalse(app.is_desktop_executable(r'C:\npm\node_modules\@openai\codex\vendor\bin\codex.exe'))

    def test_native_idle_and_historical_errors(self):
        view = app.read_text(replay('idle'))
        self.assertTrue(view.screen.identified and view.screen.ready)
        self.assertFalse(view.screen.running or view.screen.draft or view.screen.blocked)
        self.assertEqual(view.screen.error_kind, '')
        self.assertTrue(view.screen.completion_id)

    def test_long_conversation_keeps_the_native_composer_readable(self):
        window = replay('idle')
        data = {'kind': 'TextControl', 'name': 'Earlier assistant message', 'id': '',
                'enabled': True, 'offscreen': False, 'runtime': [1, 1], 'value': None, 'children': []}
        window.control.children[:0] = [Node(data, window.control) for _ in range(2200)]
        view = app.read_text(window)
        self.assertTrue(view.screen.identified and view.screen.ready)
        self.assertEqual(view.screen.error_kind, '')

    def test_native_network_and_quota_without_reset(self):
        self.assertEqual(app.read_text(replay('network')).screen.error_kind, 'network')
        view = app.read_text(replay('quota'))
        self.assertEqual(view.screen.error_kind, 'quota_retry')
        self.assertIsNone(view.screen.reset_utc)

    def test_open_native_menu_blocks_every_sender(self):
        window = replay('menu')
        self.assertTrue(app.read_text(window).screen.blocked)
        self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))

    def test_draft_identity_switch_and_other_app_mode_hold(self):
        window = replay('idle')
        view = app.read_text(window)
        composer = view.composer
        composer.value = 'do not submit this draft'
        self.assertTrue(app.read_text(window).screen.draft)
        self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))
        composer.value = ''
        app.prepare(window, view.identity)
        window.control.Name = 'Another conversation'
        self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))
        window = replay('idle')
        for c in app.walk(window.control):
            if c.Name == '切换模式，当前模式：Codex':
                c.Name = '切换模式，当前模式：ChatGPT'
        self.assertFalse(app.read_text(window).screen.identified)
        self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))

    def test_assistant_quoting_quota_is_not_a_native_error(self):
        window = replay('idle')
        for c in app.walk(window.control):
            if c.ControlTypeName == 'TextControl' and c.Name == 'CODEX_RETRY_RECOVERED':
                c.Name = '你已达到使用上限。请稍后再试。'
        self.assertEqual(app.read_text(window).screen.error_kind, '')

    def test_user_quoting_an_error_is_not_a_native_error(self):
        window = replay('network')
        nodes = list(app.walk(window.control))
        last_user = max(i for i, c in enumerate(nodes) if c.Name == '你说：')
        for c in nodes[last_user + 1:]:
            if c.ControlTypeName == 'TextControl' and c.Name.startswith('Native network'):
                c.Name = '你已达到使用上限。请稍后再试。'
            if c.ControlTypeName == 'TextControl' and c.Name.startswith('unexpected status'):
                c.Name = 'No native error'
        # Unknown native failure text is held, never scheduled from the quoted user text.
        self.assertEqual(app.read_text(window).screen.error_kind, 'attention')

    def test_unknown_native_error_requires_attention(self):
        window = replay('network')
        for c in app.walk(window.control):
            if c.ControlTypeName == 'TextControl' and c.Name.startswith('unexpected status'):
                c.Name = 'Unexpected authentication state. Sign in again.'
        self.assertEqual(app.read_text(window).screen.error_kind, 'attention')

    def test_an_unreadable_desktop_is_held(self):
        window = replay('idle')
        with patch.object(window.control, 'GetChildren', side_effect=RuntimeError('UIA disconnected')):
            self.assertIsNone(app.read_text(window))
            self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))

if __name__ == '__main__':
    unittest.main()
