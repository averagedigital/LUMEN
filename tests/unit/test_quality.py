import numpy as np
import pytest

from lumen_data.contracts import case_id, group_split
from lumen_data.pipeline import qa
from lumen_data.storage import Store
from test_contracts import valid_case
from test_exports import accepted
from test_pipeline import png


def add_case(store, index, group, baseline, base_scene=None):
    record = valid_case()
    record.update(case_id=case_id({'index': index}), base_scene_id=base_scene or f'scene-{index}',
                  split_group=group, split=group_split(group), operator='text_clip_x',
                  structural_family_id=group, source_project_id='lumen-grammar')
    record['capture'].update(locale='en')
    current = baseline.copy()
    current[10:20, 10 + index:20 + index] = 0
    mask = np.any(baseline != current, axis=2).astype(np.uint8) * 255
    record['assets'] = {'reference': store.put(png(baseline)), 'current': store.put(png(current)),
                        'raw_change_mask': store.put(png(mask))}
    accepted(store, record)


def test_qa_rejects_same_base_scene_across_splits_with_different_pixels(tmp_path):
    store = Store(tmp_path)
    for index, family in enumerate(('chat', 'calendar', 'admin')):
        add_case(store, index, f'lumen-grammar-v2/{family}',
                 np.full((100, 200, 3), 240 + index, np.uint8), base_scene='shared-scene')
    with pytest.raises(ValueError, match='base scene split leakage'):
        qa(tmp_path)


def test_qa_rejects_identical_baselines_across_splits(tmp_path):
    store = Store(tmp_path)
    baseline = np.full((100, 200, 3), 255, np.uint8)
    add_case(store, 0, 'lumen-grammar-v2/chat', baseline)
    add_case(store, 1, 'lumen-grammar-v2/calendar', baseline)
    with pytest.raises(ValueError, match='baseline split leakage'):
        qa(tmp_path)


def test_qa_reports_similar_baselines_without_dropping_subtle_positives(tmp_path):
    store = Store(tmp_path)
    for index, group in enumerate(['lumen-grammar-v2/chat', 'lumen-grammar-v2/calendar', 'lumen-grammar-v2/admin']):
        baseline = np.full((100, 200, 3), 240 + index, np.uint8)
        add_case(store, index, group, baseline)
    report = qa(tmp_path)
    assert report['accepted_cases'] == 3 and report['positives'] == 3
    assert report['near_duplicate_baseline_clusters'] == 1
    assert report['cross_split_near_duplicate_clusters'] == 1
    assert report['qa'] == 'passed'


def test_qa_rejects_missing_evaluation_split(tmp_path):
    store = Store(tmp_path)
    add_case(store, 0, 'lumen-grammar-v2/admin', np.full((100, 200, 3), 255, np.uint8))
    with pytest.raises(ValueError, match='missing dataset splits'):
        qa(tmp_path)


def test_qa_stops_at_resource_reserve_and_removes_scratch(tmp_path, monkeypatch):
    from lumen_data import pipeline
    store = Store(tmp_path)
    for index, group in enumerate(['lumen-grammar-v2/chat', 'lumen-grammar-v2/calendar', 'lumen-grammar-v2/admin']):
        add_case(store, index, group, np.full((100, 200, 3), 240 + index, np.uint8))

    def full(path):
        raise RuntimeError('disk reserve reached')

    monkeypatch.setattr(pipeline, 'guard', full)
    with pytest.raises(RuntimeError, match='disk reserve'):
        qa(tmp_path)
    assert not list(tmp_path.glob('.qa-*'))
