import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from .contracts import canonical


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'state.sqlite', timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              token TEXT, lease REAL, attempts INTEGER NOT NULL DEFAULT 0, result TEXT);
            CREATE TABLE IF NOT EXISTS cases (
              id TEXT PRIMARY KEY, current_hash TEXT NOT NULL UNIQUE, record TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS task_status ON tasks(status, lease);
        ''')

    def path(self, digest, suffix='png'):
        if not re.fullmatch('[a-f0-9]{64}', digest) or not re.fullmatch('[a-z0-9]+', suffix):
            raise ValueError('invalid object address')
        return self.root / 'objects' / digest[:2] / f'{digest}.{suffix}'

    def put(self, content, suffix='png'):
        digest = hashlib.sha256(content).hexdigest()
        path = self.path(digest, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError('object checksum conflict')
            self.sync_directories(path.parent)
            return digest
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(temporary, path)
                self.sync_directories(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
        return digest

    def sync_directories(self, directory):
        for path in (directory, directory.parent, self.root):
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def read(self, digest, suffix='png'):
        content = self.path(digest, suffix).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError('object checksum mismatch')
        return content

    def enqueue(self, task_id, payload):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO tasks(id,payload) VALUES (?,?)',
                            (task_id, canonical(payload)))

    def claim(self, now=None, lease_seconds=120, max_attempts=3):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('''UPDATE tasks SET status='rejected', result=?
                WHERE status='running' AND lease<=? AND attempts>=?''',
                            (canonical({'reason': 'lease_expired'}), now, max_attempts))
            row = self.db.execute('''SELECT id,payload FROM tasks WHERE status='pending'
                OR (status='running' AND lease<=? AND attempts<?) ORDER BY rowid LIMIT 1''',
                                  (now, max_attempts)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.db.execute('''UPDATE tasks SET status='running',token=?,lease=?,attempts=attempts+1
                WHERE id=?''', (token, now + lease_seconds, row[0]))
            return {'id': row[0], 'payload': json.loads(row[1]), 'token': token}

    def heartbeat(self, task_id, token, now=None, lease_seconds=120):
        self.heartbeat_many([{'id': task_id, 'token': token}], now, lease_seconds)

    def heartbeat_many(self, tasks, now=None, lease_seconds=120):
        now = time.time() if now is None else now
        with self.db:
            for task in tasks:
                cursor = self.db.execute('''UPDATE tasks SET lease=? WHERE id=? AND token=?
                    AND status='running' AND lease>?''',
                    (now + lease_seconds, task['id'], task['token'], now))
                if cursor.rowcount != 1:
                    raise ValueError('lost lease')

    def finish(self, task_id, token, status, result, now=None):
        if status not in ('accepted', 'rejected'):
            raise ValueError('invalid terminal status')
        now = time.time() if now is None else now
        with self.db:
            cursor = self.db.execute('''UPDATE tasks SET status=?,result=? WHERE id=? AND token=?
                AND status='running' AND lease>?''', (status, canonical(result), task_id, token, now))
            if cursor.rowcount != 1:
                raise ValueError('lost lease')
            if status == 'accepted':
                self.db.execute('INSERT INTO cases VALUES (?,?,?)',
                                (result['case_id'], result['assets']['current'], canonical(result)))

    def count(self):
        return self.db.execute('SELECT count(*) FROM cases').fetchone()[0]

    def records(self):
        for (record,) in self.db.execute('SELECT record FROM cases ORDER BY id'):
            yield json.loads(record)
