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
        self.ClassName = data.get('class', '')
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

def native_task_quota():
    window = replay('quota')
    target = [c for c in app.walk(window.control) if c.ControlTypeName == 'TextControl'
              and c.Name.startswith('你已达到使用上限')][-1]
    group = target.parent
    parent = group.parent
    captured = json.loads((FIXTURES / 'native_quota_component.json').read_text(encoding='utf-8'))
    native = Node(captured, parent)
    index = parent.children.index(group)
    parent.children[index] = native
    header = {'kind': 'TextControl', 'name': 'ChatGPT 说：', 'id': '', 'enabled': True,
              'offscreen': False, 'runtime': [1, 2], 'value': None, 'children': []}
    parent.children.insert(index, Node(header, parent))
    return window, native

class DesktopScreens(unittest.TestCase):
    def test_non_codex_page_without_composer_does_not_enter_monitor_list(self):
        window = replay('onboarding_20261004')
        view = app.read_text(window)
        self.assertFalse(view.screen.identified)
        self.assertTrue(view.screen.blocked)
        self.assertIsNone(view.composer)

    def test_real_task_quota_after_commentary_is_detected(self):
        window, _ = native_task_quota()
        view = app.read_text(window)
        self.assertEqual(view.screen.error_kind, 'quota_retry')
        self.assertIsNone(view.screen.reset_utc)

    def test_real_task_quota_while_app_retries_is_visible_but_not_sent(self):
        window, _ = native_task_quota()
        view = app.read_text(window)
        scope = view.composer.GetParentControl().GetParentControl().GetParentControl()
        stop = {'kind': 'ButtonControl', 'name': '停止', 'id': '', 'enabled': True,
                'offscreen': False, 'runtime': [1, 3], 'value': None, 'children': []}
        scope.children.append(Node(stop, scope))
        view = app.read_text(window)
        self.assertTrue(view.screen.running)
        self.assertEqual(view.screen.error_kind, 'quota_retry')
        self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))

    def test_native_quota_clears_after_a_later_assistant_entry(self):
        window, native = native_task_quota()
        header = {'kind': 'TextControl', 'name': 'ChatGPT 说：', 'id': '', 'enabled': True,
                  'offscreen': False, 'runtime': [1, 4], 'value': None, 'children': []}
        parent = native.parent
        parent.children.insert(parent.children.index(native) + 1, Node(header, parent))
        self.assertEqual(app.read_text(window).screen.error_kind, '')

    def test_a_user_quotation_does_not_use_the_native_error_component(self):
        window, native = native_task_quota()
        native.ClassName = '_Paragraph_176oq_2'
        self.assertEqual(app._native_error(list(app.walk(window.control))), '')

    def test_native_high_and_extra_high_are_distinct(self):
        self.assertEqual(app._status_effort('自定义 极高，第 5 项，共 6 项。'), 'xhigh')
        self.assertEqual(app._status_effort('自定义 高，第 4 项，共 6 项。'), 'high')
        self.assertEqual(app._status_effort('6 Sol 标准，第 2 项，共 5 项。'), 'medium')
        self.assertEqual(app._status_effort('6 Sol 深度，第 3 项，共 5 项。'), 'high')
    def test_installed_app_identity_does_not_depend_on_chatgpt_title(self):
        self.assertTrue(app.is_desktop_executable(r'C:\Program Files\WindowsApps\OpenAI.Codex_26.930.0_x64__package\app\ChatGPT.exe'))
        self.assertFalse(app.is_desktop_executable(r'C:\Apps\ChatGPT\ChatGPT.exe'))
        self.assertFalse(app.is_desktop_executable(r'C:\Program Files\WindowsApps\OpenAI.ChatGPT_26.930.0_x64__package\app\ChatGPT.exe'))
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

    def test_hidden_codex_mode_cannot_enable_chatgpt_or_work(self):
        for mode in ('ChatGPT', 'Work', 'ChatGPT Work'):
            with self.subTest(mode=mode):
                window = replay('idle')
                control = next(c for c in app.walk(window.control)
                               if c.Name == '切换模式，当前模式：Codex')
                control.IsOffscreen = True
                data = dict(kind='ButtonControl', name='切换模式，当前模式：' + mode,
                            id='', enabled=True, offscreen=False, runtime=[100, 1], value=None, children=[])
                window.control.children.append(Node(data, window.control))
                self.assertFalse(app.read_text(window).screen.identified)
                self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))
        window = replay('idle')
        for control in app.walk(window.control):
            if control.Name == '切换模式，当前模式：Codex':
                control.IsOffscreen = True
        self.assertFalse(app.read_text(window).screen.identified)

    def test_conflicting_visible_modes_do_not_enable_codex_sender(self):
        window = replay('idle')
        data = dict(kind='ButtonControl', name='Switch mode, current mode: ChatGPT',
                    id='', enabled=True, offscreen=False, runtime=[100, 1], value=None, children=[])
        window.control.children.append(Node(data, window.control))
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

    def test_native_55_model_remains_identified_after_selection(self):
        view = app.read_text(replay('model_55_20261004'))
        self.assertEqual(view.screen.model, '5.5')
        self.assertEqual(view.screen.effort, 'low')
        self.assertIsNotNone(view.model_button)

    def test_native_permission_card_replaces_composer_without_changing_chat_identity(self):
        pending = app.read_text(replay('permission_request_20261004'))
        approved = app.read_text(replay('permission_approved_20261004'))
        self.assertTrue(pending.screen.identified and pending.screen.permission and pending.screen.blocked)
        self.assertIsNone(pending.composer)
        self.assertIsNotNone(pending.approval_button)
        self.assertEqual(pending.identity, approved.identity)
        self.assertEqual(pending.thread_hint, approved.thread_hint)
        self.assertFalse(approved.screen.permission)
        self.assertEqual(approved.permission_mode, 'ask-for-approval')

    def test_permission_card_requires_native_structure_and_visible_once_only_button(self):
        for change in ('offscreen', 'structure'):
            with self.subTest(change=change):
                window = replay('permission_request_20261004')
                button = next(c for c in app.walk(window.control) if c.ControlTypeName == 'ButtonControl' and c.Name == '允许一次')
                if change == 'offscreen':
                    button.IsOffscreen = True
                else:
                    button.parent.ClassName = 'ordinary-message'
                self.assertFalse(app.read_text(window).screen.permission)

    def test_native_message_redraw_preserves_conversation_identity(self):
        before = app.read_text(replay('permission_approved_20261004'))
        later = app.read_text(replay('network_20261004'))
        self.assertEqual(before.identity, later.identity)
        self.assertEqual(before.thread_hint, later.thread_hint)

    def test_native_sidebar_switch_changes_context_even_when_panel_is_reused(self):
        before = app.read_text(replay('identity_before_switch_20261004'))
        other = app.read_text(replay('identity_other_chat_20261004'))
        returned = app.read_text(replay('identity_after_switch_20261004'))
        self.assertNotEqual(before.thread_hint, other.thread_hint)
        self.assertNotEqual(before.identity, returned.identity)

    def test_native_identical_title_and_prompt_still_have_separate_contexts(self):
        first = app.read_text(replay('identical_title_first_20261004'))
        second_window = replay('identical_title_second_20261004')
        second = app.read_text(second_window)
        self.assertEqual(first.title, second.title)
        self.assertNotEqual(first.thread_hint, second.thread_hint)
        self.assertNotEqual(first.identity, second.identity)
        app.prepare(second_window, first.identity)
        self.assertIsNone(app._ready(second_window))

    def test_permission_card_is_not_approved_in_chatgpt_mode(self):
        window = replay('permission_request_20261004')
        mode = next(c for c in app.walk(window.control) if c.ControlTypeName == 'ButtonControl' and '当前模式：Codex' in c.Name)
        mode.Name = mode.Name.replace('Codex', 'ChatGPT')
        view = app.read_text(window)
        self.assertFalse(view.screen.identified or view.screen.permission)

    def test_permission_mode_picker_and_full_access_confirmation_are_not_tool_approvals(self):
        for fixture in ('permissions_menu_20261004', 'permissions_full_confirm_20261004'):
            with self.subTest(fixture=fixture):
                self.assertFalse(app.read_text(replay(fixture)).screen.permission)

    def test_compact_layout_permission_picker_exposes_the_actual_checked_mode(self):
        for fixture, wanted in (('full', 'full-access'), ('ask', 'ask-for-approval'), ('delegate', 'approve-for-me')):
            with self.subTest(mode=wanted):
                window = replay('permissions_checked_' + fixture + '_20261004')
                menu = next(c for c in app.walk(window.control) if c.ControlTypeName == 'MenuControl')
                self.assertEqual([mode for mode in app._PERMISSION_CHOICES if app._permission_selected(menu, mode)], [wanted])

    def test_an_unreadable_desktop_is_held(self):
        window = replay('idle')
        with patch.object(window.control, 'GetChildren', side_effect=RuntimeError('UIA disconnected')):
            self.assertIsNone(app.read_text(window))
            self.assertFalse(app.send_prompt(window, 'continue', dry_run=True))

if __name__ == '__main__':
    unittest.main()
