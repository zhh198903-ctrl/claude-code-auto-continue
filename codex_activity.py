"""Read local Codex Desktop activity without driving or writing to the app.

The desktop thread index supplies sidebar recency. Newer sessions project
turns into thread_history; older sessions record turn events in JSONL files.
All databases are opened read-only, with short lock timeouts.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

DEFAULT_LIMIT = 5
MAX_LIMIT = 100
TAIL_BYTES = 256 * 1024


def activity_limit(value):
    try:
        return min(MAX_LIMIT, max(1, int(value)))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LIMIT


def _database(root, prefix):
    candidates = []
    for path in root.glob(prefix + '_*.sqlite'):
        match = re.fullmatch(re.escape(prefix) + r'_(\d+)\.sqlite', path.name)
        if match:
            candidates.append((int(match[1]), path))
    return max(candidates, default=(0, None))[1]


def _connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.15)
    db.row_factory = sqlite3.Row
    return db


def _date(value, milliseconds=False):
    try:
        if isinstance(value, str) and not value.isdigit():
            return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
        return datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), timezone.utc) if value else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _error_status(error):
    # Read structured error fields, never matching assistant conversation text.
    text = json.dumps(error, ensure_ascii=False).casefold()
    if any(word in text for word in ('usage_limit', 'usage limit', 'rate_limit', '使用上限')):
        return 'quota'
    if any(word in text for word in ('network', 'stream disconnected', 'connection', 'timeout')):
        return 'network'
    return 'failed'


class RecentActivities:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else None
        self._headers = {}
        self._tails = {}

    def _metadata(self, path):
        key = str(path)
        if key not in self._headers:
            try:
                with path.open('rb') as file:
                    line = file.readline(TAIL_BYTES)
                item = json.loads(line)
                payload = item.get('payload', {}) if isinstance(item, dict) and item.get('type') == 'session_meta' else {}
                self._headers[key] = payload if isinstance(payload, dict) else {}
            except (OSError, ValueError):
                return {}
        return self._headers[key]

    def _rollout(self, path):
        """Bounded tail reads; unchanged files reuse their last observation."""
        key = str(path)
        try:
            stat = path.stat()
            stamp = (stat.st_size, stat.st_mtime_ns)
            cached = self._tails.get(key)
            if cached and cached[0] == stamp:
                return cached[1]
            status, updated = 'unknown', None
            with path.open('rb') as file:
                offset = max(0, stat.st_size - TAIL_BYTES)
                file.seek(offset)
                if offset:
                    file.readline()  # discard the partial first record
                lines = file.read(TAIL_BYTES).splitlines()
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # the app can be midway through appending a record
                if not isinstance(event, dict):
                    continue
                payload = event.get('payload', {})
                if event.get('type') != 'event_msg' or not isinstance(payload, dict):
                    continue
                kind = payload.get('type')
                if kind in {'task_started', 'turn_started'}:
                    status = 'running'
                elif kind in {'task_complete', 'turn_completed'}:
                    status = 'completed'
                elif kind in {'turn_aborted', 'task_interrupted'}:
                    status = 'interrupted'
                elif kind in {'error', 'turn_failed'}:
                    status = _error_status(payload)
                else:
                    continue
                updated = _date(event.get('timestamp')) or updated
            result = status, updated
            self._tails[key] = stamp, result
            return result
        except OSError:
            return 'unknown', None

    def _history(self, db, thread_id):
        turn = db.execute('SELECT status, error_json, started_at, completed_at '
                          'FROM thread_turns WHERE thread_id=? '
                          'ORDER BY rollout_ordinal DESC LIMIT 1', (thread_id,)).fetchone()
        if turn is None:
            return None
        status = {'inProgress': 'running', 'completed': 'completed',
                  'interrupted': 'interrupted', 'failed': 'failed'}.get(turn['status'], 'unknown')
        if status == 'failed' and turn['error_json']:
            status = _error_status(turn['error_json'])
        return status, _date(turn['completed_at'] or turn['started_at'])

    def thread_id_for_title(self, title):
        """Bind a native view only when exactly one Codex thread has its title.

        Search the full index, rather than just the recent page. Ambiguous
        titles remain unbound, so a thread's settings never reach its namesake.
        """
        root = self.home or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        index = _database(root, 'state')
        if index is None:
            return ''
        matches = []
        try:
            with closing(_connect(index)) as db:
                columns = {r['name'] for r in db.execute('PRAGMA table_info(threads)')}
                filters = "title=? AND archived=0 AND source IN ('vscode','desktop')"
                if 'thread_source' in columns:
                    filters += " AND (thread_source IS NULL OR thread_source='user')"
                for row in db.execute(f'SELECT * FROM threads WHERE {filters}', (title,)):
                    data = dict(row)
                    origin = data.get('originator') or self._metadata(Path(data['rollout_path'])).get('originator')
                    if origin == 'Codex Desktop':
                        matches.append(data['id'])
                        if len(matches) > 1:
                            return ''
            return matches[0] if matches else ''
        except (sqlite3.Error, OSError):
            return ''

    def thread(self, thread_id):
        root = self.home or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        index = _database(root, 'state')
        if index is None:
            return None
        try:
            with closing(_connect(index)) as db:
                row = db.execute("SELECT * FROM threads WHERE id=? AND archived=0 AND source IN ('vscode','desktop')",
                                 (thread_id,)).fetchone()
                if row is None:
                    return None
                data = dict(row)
                origin = data.get('originator') or self._metadata(Path(data['rollout_path'])).get('originator')
                if origin != 'Codex Desktop' or data.get('thread_source') not in (None, 'user'):
                    return None
                return data
        except (sqlite3.Error, OSError):
            return None

    def read(self, limit=DEFAULT_LIMIT):
        root = self.home or Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
        limit = activity_limit(limit)
        rows = []
        try:
            index = _database(root, 'state')
            if index is None:
                return [], 'Local Codex activity data is unavailable.'
            with closing(_connect(index)) as db:
                columns = {r['name'] for r in db.execute('PRAGMA table_info(threads)')}
                if not {'id', 'title', 'cwd', 'rollout_path', 'source', 'updated_at', 'archived'} <= columns:
                    return [], 'Local Codex thread index format is unsupported.'
                # Recency reflects user interaction, unlike updated_at, which
                # also changes during imports and settings migrations.
                recency = [f'NULLIF({c},0)' + (' * 1000' if c.endswith('_at') else '')
                           for c in ('recency_at_ms', 'recency_at', 'updated_at_ms', 'updated_at') if c in columns]
                order = 'COALESCE(' + ','.join(recency + ['0']) + ')'
                filters = "archived=0 AND source IN ('vscode', 'desktop')"
                if 'thread_source' in columns:
                    filters += " AND (thread_source IS NULL OR thread_source='user')"
                candidates = db.execute(f'SELECT * FROM threads WHERE {filters} ORDER BY {order} DESC, id DESC')
                history_path = _database(root, 'thread_history')
                history = None
                history_warning = ''
                try:
                    if history_path:
                        try:
                            history = _connect(history_path)
                        except sqlite3.Error:
                            history_warning = 'Turn history unavailable; reading session events.'
                    for thread in candidates:
                        data = dict(thread)
                        path = Path(data['rollout_path'])
                        originator = data.get('originator') or self._metadata(path).get('originator')
                        if originator != 'Codex Desktop':
                            continue
                        observed = None
                        if history:
                            try:
                                observed = self._history(history, data['id'])
                            except sqlite3.Error:
                                history.close()
                                history = None
                                history_warning = 'Turn history unavailable; reading session events.'
                        status, turn_time = observed or self._rollout(path)
                        last_activity = _date(data.get('updated_at_ms'), True) or _date(data['updated_at'])
                        if turn_time and (last_activity is None or turn_time > last_activity):
                            last_activity = turn_time
                        rows.append({'thread_id': data['id'], 'title': data['title'], 'cwd': data['cwd'],
                                     'status': status, 'updated_utc': last_activity,
                                     'model': data.get('model') or '', 'effort': data.get('reasoning_effort') or ''})
                        if len(rows) >= limit:
                            break
                finally:
                    if history:
                        history.close()
            # Keep caches bounded when the user's recent activity set changes.
            if len(self._headers) > 1000:
                self._headers.clear()
            if len(self._tails) > MAX_LIMIT * 2:
                self._tails.clear()
            return rows, history_warning
        except (sqlite3.Error, OSError) as exc:
            return [], f'Could not read local Codex activities: {exc}'
