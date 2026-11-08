import base64
from collections import Counter
import json
import threading
import time

import numpy as np
import pytest
import yaml

from lumen_data import pipeline
from lumen_data.storage import Store
from test_pipeline import png


def test_source_mix_weights_advance_each_source_without_skipping_scenes():
    config = {'source_manifest': 'sources.jsonl', 'source_mix': ['grammar', 'grammar', 'grammar', 'external']}
    assert [pipeline.source_slot(i, config) for i in range(8)] == [
        ('grammar', 0), ('grammar', 1), ('grammar', 2), ('external', 0),
        ('grammar', 3), ('grammar', 4), ('grammar', 5), ('external', 1)]
    assert pipeline.source_slot(5, {'source_manifest': None}) == ('grammar', 5)
    assert pipeline.source_slot(5, {'source_manifest': 'sources.jsonl'}) == ('external', 2)


@pytest.mark.parametrize('mix', [[], ['unknown'], 'grammar', ['external']])
def test_invalid_or_unavailable_source_mix_is_explicit(mix):
    with pytest.raises(ValueError, match='source mix'):
        pipeline.source_slot(0, {'source_manifest': None, 'source_mix': mix})


def fake_workers(monkeypatch, workers, fatal=None):
    state = {'active': 0, 'peak': 0, 'closed': 0, 'fatal': fatal}
    lock = threading.Lock()
    barrier = threading.Barrier(workers)

    class FakeWorker:
        def __init__(self, asset_root):
            pass

        def request(self, payload):
            index = payload['seed'] - 100
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
            try:
                if index < workers:
                    try:
                        barrier.wait(timeout=.5)
                    except threading.BrokenBarrierError:
                        pass
                time.sleep(.01 * (workers - index % workers))
                if index == state['fatal']:
                    return {'status': 'resource_error', 'issues': [], 'error': 'fixture failure'}
                if index == 1:
                    return {'status': 'inapplicable', 'issues': []}
                width, height = payload['viewport']
                before = np.full((height, width, 3), 255, np.uint8)
                current = before.copy()
                current[10:30, 10:50] = 10 + (0 if index == 3 else index)
                operator = payload['operator']
                issues = []
                if operator == 'clean':
                    before = current.copy()
                    status = 'accepted_clean'
                elif operator.startswith('benign_'):
                    status = 'accepted_benign_change'
                else:
                    status = 'accepted_bug'
                    issues = [{'id': 'target', 'family': 'text_geometry', 'symptom': operator,
                        'bbox': [10, 10, 50, 30], 'evidence': {'baseline_passed': True,
                            'after_failed': True, 'oracle_id': 'fixture'}}]
                return {'status': status, 'issues': issues, 'browser': 'fixture',
                    'reference': base64.b64encode(png(before)).decode(),
                    'current': base64.b64encode(png(current)).decode(),
                    'scene': {'html': '<h1>Fixture</h1>', 'page': {'archetype': 'dashboard'}}}
            finally:
                with lock:
                    state['active'] -= 1

        def close(self):
            state['closed'] += 1

    monkeypatch.setattr(pipeline, 'Worker', FakeWorker)
    return state


def config(tmp_path, workers, target=5, max_attempts=10):
    path = tmp_path / f'config-{workers}.yaml'
    path.write_text(yaml.safe_dump({'seed': 100, 'workers': workers, 'target': target,
        'max_attempts': max_attempts, 'variants_per_scene': 10, 'source_manifest': None,
        'min_disk_gib': 0, 'min_ram_gib': 0}))
    return path


def test_worker_counts_preserve_accepted_prefix_and_duplicates(tmp_path, monkeypatch):
    baseline = None
    for workers in (1, 2, 4):
        state = fake_workers(monkeypatch, workers)
        run = tmp_path / f'run-{workers}'
        result = pipeline.generate(config(tmp_path, workers), run)
        assert result['accepted'] == 5
        store = Store(run)
        records = [(r['case_id'], r['assets'], r['issues'], r['split']) for r in store.records()]
        rejected = Counter(json.loads(row[0])['reason'] for row in
            store.db.execute("SELECT result FROM tasks WHERE status='rejected'"))
        assert rejected == {'inapplicable': 1, 'duplicate_image': 1}
        if baseline is None:
            baseline = records
        assert records == baseline
        assert state['peak'] == workers and state['closed'] == workers


def test_fatal_batch_resumes_existing_tasks_when_enqueue_budget_is_exhausted(tmp_path, monkeypatch):
    state = fake_workers(monkeypatch, 4, fatal=1)
    path = config(tmp_path, 4, target=4, max_attempts=4)
    run = tmp_path / 'run'
    with pytest.raises(RuntimeError, match='resource_error'):
        pipeline.generate(path, run)
    store = Store(run)
    assert store.count() == 1
    assert state['closed'] == 4
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 4
    with store.db:
        store.db.execute("UPDATE tasks SET lease=0 WHERE status='running'")
    fake_workers(monkeypatch, 4)
    pipeline.generate(path, run, resume=True)
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 4
    assert store.db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] == 0
    assert store.count() == 2


def test_source_exhaustion_preserves_valid_prefix_before_error(tmp_path, monkeypatch):
    manifest = tmp_path / 'empty.jsonl'
    manifest.write_text('')
    for workers in (1, 4):
        fake_workers(monkeypatch, workers)
        path = config(tmp_path, workers, target=4, max_attempts=4)
        settings = yaml.safe_load(path.read_text())
        settings.update(variants_per_scene=1, source_manifest=str(manifest))
        path.write_text(yaml.safe_dump(settings))
        run = tmp_path / f'run-{workers}'
        with pytest.raises(RuntimeError, match='source scenes exhausted'):
            pipeline.generate(path, run)
        store = Store(run)
        assert store.count() == 1
        assert store.db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0] == 0


def test_target_smaller_than_pool_does_not_enqueue_extra_recipes(tmp_path, monkeypatch):
    state = fake_workers(monkeypatch, 4)
    run = tmp_path / 'run'
    pipeline.generate(config(tmp_path, 4, target=1, max_attempts=1), run)
    store = Store(run)
    assert store.count() == 1
    assert store.db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
    assert state['closed'] == 4


@pytest.mark.parametrize('workers', [0, 5, True, 1.5])
def test_invalid_worker_count_is_rejected_before_creating_run(tmp_path, workers):
    run = tmp_path / 'run'
    with pytest.raises(ValueError, match='workers'):
        pipeline.generate(config(tmp_path, workers), run)
    assert not run.exists()


def test_resource_reserve_is_checked_before_launching_workers(tmp_path, monkeypatch):
    def exhausted(*args):
        raise RuntimeError('memory reserve reached')

    def forbidden_worker(*args):
        pytest.fail('worker launched before checking memory reserve')

    monkeypatch.setattr(pipeline, 'guard', exhausted)
    monkeypatch.setattr(pipeline, 'Worker', forbidden_worker)
    with pytest.raises(RuntimeError, match='memory reserve'):
        pipeline.generate(config(tmp_path, 4), tmp_path / 'run')


def test_explicit_operator_mix_is_respected_and_rejects_unknown_mechanisms():
    from lumen_data.pipeline import recipe
    import pytest

    config = {'seed': 7, 'variants_per_scene': 4,
              'operators': ['font_size_regression', 'clean', 'clean']}
    draws = [recipe(i, config, 'test')['operator'] for i in range(100)]
    assert set(draws) == {'font_size_regression', 'clean'}
    assert draws.count('clean') > draws.count('font_size_regression')
    for invalid in [[], ['unknown'], 'clean', [1]]:
        with pytest.raises(ValueError, match='operator'):
            recipe(0, {**config, 'operators': invalid}, 'test')


def test_resume_rejects_changed_operator_order_and_weights(tmp_path, monkeypatch):
    fake_workers(monkeypatch, 1)
    path = config(tmp_path, 1, target=1, max_attempts=1)
    settings = yaml.safe_load(path.read_text())
    settings['operators'] = ['clean', 'font_size_regression']
    path.write_text(yaml.safe_dump(settings))
    run = tmp_path / 'run'
    pipeline.generate(path, run)
    settings['operators'] = ['font_size_regression', 'clean']
    path.write_text(yaml.safe_dump(settings))
    with pytest.raises(ValueError, match='contract changed'):
        pipeline.generate(path, run, resume=True)


def test_invalid_operator_mix_does_not_create_a_run(tmp_path):
    path = config(tmp_path, 1)
    settings = yaml.safe_load(path.read_text())
    settings['operators'] = ['not_an_operator']
    path.write_text(yaml.safe_dump(settings))
    run = tmp_path / 'run'
    with pytest.raises(ValueError, match='operator'):
        pipeline.generate(path, run)
    assert not run.exists()
