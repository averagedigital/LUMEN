import sqlite3
import os
import stat

import pytest

from lumen_data.storage import Store


def test_content_addressed_writes_and_dedup(tmp_path):
    store = Store(tmp_path)
    digest = store.put(b'abc', 'bin')
    assert store.put(b'abc', 'bin') == digest
    assert store.read(digest, 'bin') == b'abc'
    assert len(list((tmp_path / 'objects').rglob('*.bin'))) == 1
    with pytest.raises(ValueError):
        store.read('../outside', 'bin')


def test_leases_recover_but_stale_workers_cannot_commit(tmp_path):
    store = Store(tmp_path)
    store.enqueue('one', {'seed': 1})
    first = store.claim(now=10, lease_seconds=5)
    assert first is not None
    assert store.claim(now=11) is None
    second = store.claim(now=16)
    assert second is not None
    assert first['token'] != second['token']
    with pytest.raises(ValueError, match='lease'):
        store.finish('one', first['token'], 'rejected', {'reason': 'stale'}, now=17)
    store.finish('one', second['token'], 'rejected', {'reason': 'no_visible_effect'}, now=17)
    assert store.claim(now=18) is None
    store.enqueue('one', {'seed': 1})
    assert store.db.execute('select count(*) from tasks').fetchone()[0] == 1


def test_one_current_image_cannot_be_accepted_twice(tmp_path):
    store = Store(tmp_path)
    for key in ['one', 'two']:
        store.enqueue(key, {'seed': key})
        task = store.claim(now=10)
        record = {'case_id': key, 'assets': {'current': 'same'}}
        if key == 'one':
            store.finish(key, task['token'], 'accepted', record, now=11)
        else:
            with pytest.raises(sqlite3.IntegrityError):
                store.finish(key, task['token'], 'accepted', record, now=11)
    assert store.count() == 1


def test_retry_limit_is_explicit_terminal_rejection(tmp_path):
    store = Store(tmp_path)
    store.enqueue('crash', {})
    for now in [0, 10, 20]:
        assert store.claim(now=now, lease_seconds=1, max_attempts=3)
    assert store.claim(now=30, max_attempts=3) is None
    row = store.db.execute('select status, result from tasks').fetchone()
    assert row[0] == 'rejected'
    assert 'lease_expired' in row[1]


def test_batch_heartbeat_is_atomic_and_cannot_revive_expired_leases(tmp_path):
    store = Store(tmp_path)
    tasks = []
    for key in ('one', 'two'):
        store.enqueue(key, {})
        tasks.append(store.claim(now=10, lease_seconds=5))
    store.heartbeat_many(tasks, now=14, lease_seconds=10)
    assert store.db.execute('SELECT DISTINCT lease FROM tasks').fetchall() == [(24,)]
    stale = [tasks[0], {**tasks[1], 'token': 'stale'}]
    with pytest.raises(ValueError, match='lost lease'):
        store.heartbeat_many(stale, now=15, lease_seconds=20)
    assert store.db.execute('SELECT DISTINCT lease FROM tasks').fetchall() == [(24,)]
    with pytest.raises(ValueError, match='lost lease'):
        store.heartbeat_many(tasks, now=24)


def test_directory_durability_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    store = Store(tmp_path)
    original = os.fsync

    def failing_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory sync failed')
        original(fd)

    monkeypatch.setattr(os, 'fsync', failing_directory_sync)
    with pytest.raises(OSError, match='directory sync'):
        store.put(b'new image', 'bin')
