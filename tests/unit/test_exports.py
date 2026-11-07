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


@pytest.mark.parametrize('format_name', ['diff-pairs', 'hf-local'])
def test_portable_export_contains_sidecars_baseline_and_scene(tmp_path, format_name):
    store = Store(tmp_path / 'run')
    record = valid_case()
    record['has_change'] = record['has_single_frame_defect'] = False
    record['issues'] = []
    image = store.put(png(np.full((100, 200, 3), 255, np.uint8)))
    mask = store.put(png(np.zeros((100, 200), np.uint8)))
    record['assets'] = {'reference': image, 'current': image, 'raw_change_mask': mask,
                        'scene_html': store.put(b'<h1>Test</h1>', 'html')}
    baseline = store.put(b'{"schema_version":"lumen.states.v1","elements":{}}', 'json')
    record['sidecars'] = {'states': store.put(json.dumps({'schema_version': 'lumen.state-delta.v1',
        'baseline': baseline, 'delta': {}}).encode(), 'json'),
        'elements': store.put(b'{"schema_version":"lumen.elements.v1","elements":[]}', 'json')}
    css = store.put(b'body {color: black;}', 'css')
    record['recipe'] = {'source': {'assets': {'https://example.invalid/style.css': {
        'sha256': css, 'content_type': 'text/css'}}}}
    record['provenance'].update(license='cc-by-4.0', public_release_status='research_only')
    accepted(store, record)
    output = tmp_path / 'out'
    export_run(store.root, output, format_name, strict=True)
    item = json.loads(next(output.glob('manifest-*.jsonl')).read_text())
    assert (output / item['files']['scene_html']).read_text() == '<h1>Test</h1>'
    assert json.loads((output / item['files']['elements']).read_text())['elements'] == []
    state = json.loads((output / item['files']['states']).read_text())
    assert state['baseline'] == baseline
    assert json.loads((output / item['files']['states_baseline']).read_text())['elements'] == {}
    assert (output / item['files'][f'css_{css}']).read_bytes() == b'body {color: black;}'
    if format_name == 'hf-local':
        card = (output / 'README.md').read_text()
        assert 'license: mit' not in card.lower()
        assert 'cc-by-4.0' in card and 'research_only' in card


def test_missing_sidecar_fails_before_export_directory_exists(tmp_path):
    store = Store(tmp_path / 'run')
    record = valid_case()
    record['has_change'] = record['has_single_frame_defect'] = False
    record['issues'] = []
    image = store.put(png(np.full((100, 200, 3), 255, np.uint8)))
    mask = store.put(png(np.zeros((100, 200), np.uint8)))
    record['assets'] = {'reference': image, 'current': image, 'raw_change_mask': mask}
    record['sidecars'] = {'elements': 'e' * 64}
    accepted(store, record)
    with pytest.raises(FileNotFoundError):
        export_run(store.root, tmp_path / 'out', 'diff-pairs', strict=True)
    assert not (tmp_path / 'out').exists()
