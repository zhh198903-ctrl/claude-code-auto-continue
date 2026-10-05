"""Background activity regressions using local, read-only fixture databases."""
from datetime import datetime, timezone
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtWidgets import QApplication

from codex_activity import RecentActivities, _connect, activity_limit
from codex_watcher import CodexWatcher, clean_config


class ActivityChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        scratch = Path(__file__).parent / 'ccdebug'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.index = self.root / 'state_5.sqlite'
        self.db = sqlite3.connect(self.index)
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, cwd TEXT, '
                        'rollout_path TEXT, source TEXT, updated_at INTEGER, updated_at_ms INTEGER, '
                        'recency_at_ms INTEGER, archived INTEGER, originator TEXT, thread_source TEXT)')
        self.reader = RecentActivities(self.root)

    def add(self, ident, recent, *, source='vscode', origin='Codex Desktop', archived=0,
            thread_source='user', updated=100, events=()):
        path = self.root / (ident + '.jsonl')
        with path.open('w', encoding='utf-8') as file:
            file.write(json.dumps({'type': 'session_meta', 'payload': {'originator': origin}}) + '\n')
            for kind in events:
                file.write(json.dumps({'timestamp': '2026-10-04T04:00:00Z', 'type': 'event_msg',
                                       'payload': {'type': kind}}) + '\n')
        self.db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (ident, 'Same title', 'project/' + ident, str(path), source, updated,
                         updated * 1000, recent, archived, origin, thread_source))
        self.db.commit()
        return path

    def history(self):
        db = sqlite3.connect(self.root / 'thread_history_1.sqlite')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE thread_turns (thread_id TEXT, status TEXT, error_json TEXT, '
                   'started_at INTEGER, completed_at INTEGER, rollout_ordinal INTEGER)')
        return db

    def test_default_five_and_custom_limit_follow_sidebar_recency(self):
        for i in range(8):
            self.add(str(i), i + 1, updated=1000 - i, events=('task_complete',))
        rows, error = self.reader.read()
        self.assertEqual([r['thread_id'] for r in rows], ['7', '6', '5', '4', '3'])
        self.assertEqual(error, '')
        self.assertEqual(len(self.reader.read(2)[0]), 2)
        self.assertEqual(len(self.reader.read(100)[0]), 8)

    def test_binding_checks_entire_index_and_rejects_other_clients(self):
        self.add('old', 1)
        self.add('new', 100)
        self.assertEqual(len(self.reader.read(1)[0]), 1)
        self.assertEqual(self.reader.thread_id_for_title('Same title'), '')
        self.db.execute("UPDATE threads SET title='different' WHERE id='old'")
        self.db.commit()
        self.add('chatgpt', 200, origin='ChatGPT')
        self.add('cli', 300, source='cli')
        self.add('child', 400, thread_source='subagent')
        self.add('archived', 500, archived=1)
        self.assertEqual(self.reader.thread_id_for_title('Same title'), 'new')
        self.assertEqual(self.reader.thread('new')['id'], 'new')
        for ident in ('chatgpt', 'cli', 'child', 'archived', 'absent'):
            self.assertIsNone(self.reader.thread(ident))

    def test_cli_archived_subagents_and_other_clients_do_not_fill_the_limit(self):
        self.add('cli', 999, source='cli')
        self.add('archive', 998, archived=1)
        self.add('child', 997, thread_source='subagent')
        self.add('vscode', 996, origin='codex_vscode')
        self.add('desktop', 1)
        self.assertEqual([r['thread_id'] for r in self.reader.read()[0]], ['desktop'])

    def test_older_index_resolves_desktop_identity_from_session_metadata(self):
        self.add('older', 1, thread_source=None)
        self.db.execute("UPDATE threads SET originator=NULL WHERE id='older'")
        self.db.commit()
        self.assertEqual(self.reader.read()[0][0]['thread_id'], 'older')

    def test_chatgpt_and_work_sources_never_fill_recent_codex_limit(self):
        self.add('chatgpt', 999, source='desktop', origin='ChatGPT')
        self.add('work', 998, origin='ChatGPT Work')
        self.add('chat-source', 997, source='chatgpt')
        self.add('legacy-chatgpt', 996, origin='ChatGPT')
        self.db.execute("UPDATE threads SET originator=NULL WHERE id='legacy-chatgpt'")
        self.db.commit()
        self.add('codex', 1)
        self.assertEqual([r['thread_id'] for r in self.reader.read(1)[0]], ['codex'])

    def test_projected_turns_change_without_opening_a_chat(self):
        self.add('chat', 1)
        history = self.history()
        history.execute("INSERT INTO thread_turns VALUES ('chat','completed',NULL,1,2,1)")
        history.execute("INSERT INTO thread_turns VALUES ('chat','inProgress',NULL,3,NULL,2)")
        history.commit()
        self.assertEqual(self.reader.read()[0][0]['status'], 'running')
        history.execute("UPDATE thread_turns SET status='completed',completed_at=200 WHERE rollout_ordinal=2")
        history.commit()
        row = self.reader.read()[0][0]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['updated_utc'], datetime.fromtimestamp(200, timezone.utc))

    def test_native_structured_failure_status_is_visible(self):
        self.add('chat', 1)
        history = self.history()
        history.execute("INSERT INTO thread_turns VALUES ('chat','failed',?,1,2,1)",
                        ('{"code":"usage_limit_reached"}',))
        history.commit()
        self.assertEqual(self.reader.read()[0][0]['status'], 'quota')

    def test_legacy_events_refresh_and_do_not_parse_quoted_errors(self):
        path = self.add('chat', 1, events=('task_started',))
        self.assertEqual(self.reader.read()[0][0]['status'], 'running')
        with path.open('a', encoding='utf-8') as file:
            file.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'agent_message',
                           'message': 'usage_limit_reached'}}) + '\n')
            file.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_complete'}}) + '\n')
            file.write('{"type":')
        self.assertEqual(self.reader.read()[0][0]['status'], 'completed')

    def test_missing_or_malformed_sessions_show_unknown(self):
        path = self.add('chat', 1)
        path.write_text('[]\n{"type":"event_msg","payload":[]}\n', encoding='utf-8')
        self.assertEqual(self.reader.read()[0][0]['status'], 'unknown')

    def test_missing_index_is_not_created_and_connections_cannot_write(self):
        before = self.index.read_bytes()
        self.reader.read()
        self.assertEqual(self.index.read_bytes(), before)
        with closing(_connect(self.index)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE threads SET title='changed'")
        empty = self.root / 'empty'
        empty.mkdir()
        rows, error = RecentActivities(empty).read()
        self.assertEqual(rows, [])
        self.assertTrue(error)
        self.assertEqual(list(empty.iterdir()), [])

    def test_database_lock_reports_unavailable_instead_of_idle(self):
        self.add('chat', 1)
        self.db.execute('BEGIN EXCLUSIVE')
        rows, error = self.reader.read()
        self.assertEqual(rows, [])
        self.assertIn('locked', error)
        self.db.rollback()

    def test_unsupported_history_falls_back_to_legacy_events(self):
        self.add('chat', 1, events=('task_complete',))
        with closing(sqlite3.connect(self.root / 'thread_history_1.sqlite')):
            pass
        rows, error = self.reader.read()
        self.assertEqual(rows[0]['status'], 'completed')
        self.assertIn('session events', error)

    def test_count_is_bounded_and_invalid_values_use_default(self):
        for value, expected in [(None, 5), ('bad', 5), (float('inf'), 5), (0, 1), (999, 100), ('7', 7)]:
            self.assertEqual(activity_limit(value), expected)
            self.assertEqual(clean_config({'recent_limit': value})['recent_limit'], expected)

    def test_app_watcher_polls_activities_even_without_a_visible_window(self):
        self.add('chat', 1, events=('task_started',))
        watcher = CodexWatcher(provider='codex_app')
        watcher.activities = self.reader
        watcher.running = True
        received = []
        watcher.recent_snapshot.connect(lambda rows, error: received.append((rows, error)))
        with patch.object(watcher.driver, 'terminal_handles', return_value=[]):
            watcher.tick()
        self.assertEqual(received[0][0][0]['status'], 'running')
        watcher.configure({'recent_limit': 2})
        self.assertEqual(len(received), 2)
        cli = CodexWatcher()
        self.assertIsNone(cli.activities)
