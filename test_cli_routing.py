"""Provider isolation regressions; no Claude account or live session needed."""
import unittest
from pathlib import Path
from unittest.mock import patch

import cli_routing as routing
import codex_cli

FIXTURES = Path(__file__).parent / 'tests/fixtures/codex'

class RoutingTests(unittest.TestCase):
    def test_all_native_codex_captures_are_excluded_from_claude(self):
        for path in FIXTURES.glob('*.txt'):
            with self.subTest(path=path.name):
                self.assertNotEqual(routing.classify_text(path.read_text(encoding='utf-8')), 'claude')

    def test_custom_model_is_codex_and_preserves_composer_guard(self):
        text = (FIXTURES / 'idle.txt').read_text(encoding='utf-8').replace('GPT-6.1-Sol xhigh', 'custom/provider-model high')
        self.assertEqual(routing.classify_text(text), 'codex')
        self.assertTrue(codex_cli.inspect_screen(text).ready)
        text = text.replace('Ask Codex to do anything', 'unsent text')
        self.assertTrue(codex_cli.inspect_screen(text).draft)

    def test_unreadable_shell_and_future_codex_layout_never_default_to_claude(self):
        for text in (None, '', 'PS C:\\probe>', 'a shell command failed',
                     '>_ OpenAI Codex (v9.0)\n› Ask Codex to do anything\nNew footer layout'):
            self.assertNotEqual(routing.classify_text(text), 'claude')

    def test_captured_claude_composer_routes_only_with_live_footer(self):
        text = '─' * 40 + '\n❯ \n' + '─' * 40 + '\n  ⏵⏵ accept edits on (shift+tab to cycle)'
        self.assertEqual(routing.classify_text(text), 'claude')
        self.assertEqual(routing.classify_text(text + '\nPS C:\\probe> '), 'unknown')
        self.assertEqual(routing.classify_text('❯ quoted prose'), 'unknown')

    def test_both_enumeration_and_direct_actions_require_positive_claude_identity(self):
        windows = [object(), object(), object()]
        with patch.object(routing.claude, 'find_terminal_windows', return_value=windows), \
             patch.object(routing, 'provider', side_effect=['codex', 'unknown', 'claude']):
            self.assertEqual(routing.claude_windows(), [windows[2]])
        for identity in ('codex', 'unknown'):
            with patch.object(routing.codex, 'window_from_handle', return_value=windows[0]), \
                 patch.object(routing, 'provider', return_value=identity):
                self.assertIsNone(routing.claude_window(123))

    def test_tab_switch_from_codex_to_shell_drops_old_identity(self):
        text = (FIXTURES / 'idle.txt').read_text(encoding='utf-8')
        self.assertEqual(routing.classify_text(text), 'codex')
        self.assertEqual(routing.classify_text(text + '\nPS C:\\probe> '), 'unknown')

    def test_cached_claude_row_is_hidden_after_switch_or_failed_read(self):
        window = type('Window', (), {'NativeWindowHandle': 987654})()
        try:
            with patch.object(routing.codex, 'read_text', return_value='❯ \n⏵⏵ accept edits on'):
                self.assertEqual(routing.provider(window), 'claude')
                self.assertTrue(routing.claude_row_allowed(window.NativeWindowHandle))
            for text in ((FIXTURES / 'idle.txt').read_text(encoding='utf-8'), None):
                with patch.object(routing.codex, 'read_text', return_value=text):
                    routing.provider(window)
                    self.assertFalse(routing.claude_row_allowed(window.NativeWindowHandle))
        finally:
            routing._providers.pop(window.NativeWindowHandle, None)

if __name__ == '__main__':
    unittest.main()
