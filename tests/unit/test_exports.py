import json

import pytest

from lumen_data.exports import export_run
from lumen_data.storage import Store
from test_contracts import valid_case
from test_pipeline import png
import numpy as np


def accepted(store, record):
    store.enqueue(record['case_id'], {})
    task = store.claim()
    store.finish(task['id'], task['token'], 'accepted', record)


def test_yolo_uses_actual_normalized_boxes_and_coco_xywh(tmp_path):
    store = Store(tmp_path / 'run')
    record = valid_case()
    before = np.full((100, 200, 3), 255, np.uint8)
    after = before.copy()
    after[10:30, 10:50] = 0
    mask = np.any(before != after, axis=2).astype(np.uint8) * 255
    record['assets'] = {'reference': store.put(png(before)), 'current': store.put(png(after)),
                        'raw_change_mask': store.put(png(mask))}
    accepted(store, record)
    export_run(store.root, tmp_path / 'yolo', 'yolo-singleclass', strict=True)
    label = next((tmp_path / 'yolo' / 'labels').rglob('*.txt')).read_text().split()
    assert list(map(float, label)) == [0, .15, .2, .2, .2]
    export_run(store.root, tmp_path / 'coco', 'coco', strict=True)
    coco = json.loads((tmp_path / 'coco' / 'annotations.json').read_text())
    assert coco['annotations'][0]['bbox'] == [10, 10, 40, 20]


def test_strict_export_never_converts_unknown_annotation_to_background(tmp_path):
    store = Store(tmp_path / 'run')
    record = valid_case()
    record['annotation_completeness'] = 'unknown'
    accepted(store, record)
    with pytest.raises(ValueError, match='incomplete'):
        export_run(store.root, tmp_path / 'out', 'yolo-singleclass', strict=True)


def test_strict_export_rejects_corrupt_image(tmp_path):
    store = Store(tmp_path / 'run')
    record = valid_case()
    invalid = store.put(b'not a PNG')
    record['assets'] = dict.fromkeys(('reference', 'current', 'raw_change_mask'), invalid)
    accepted(store, record)
    with pytest.raises(ValueError, match='image'):
        export_run(store.root, tmp_path / 'out', 'yolo-singleclass', strict=True)


def test_strict_export_rejects_positive_with_unchanged_pixels(tmp_path):
    store = Store(tmp_path / 'run')
    record = valid_case()
    image = store.put(png(np.full((100, 200, 3), 255, np.uint8)))
    mask = store.put(png(np.zeros((100, 200), np.uint8)))
    record['assets'] = {'reference': image, 'current': image, 'raw_change_mask': mask}
    accepted(store, record)
    with pytest.raises(ValueError, match='change'):
        export_run(store.root, tmp_path / 'out', 'yolo-singleclass', strict=True)
