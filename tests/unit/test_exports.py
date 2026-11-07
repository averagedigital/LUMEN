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
    notice = store.put(b'MIT License\nCopyright example\nPermission notice', 'txt')
    record['recipe'] = {'source': {'assets': {'https://example.invalid/style.css': {
        'sha256': css, 'content_type': 'text/css', 'license_sha256': notice}}}}
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
    assert (output / item['files'][f'license_{notice}']).read_bytes().startswith(b'MIT License')
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


def test_yolo_tiles_exclude_partial_issues_and_keep_original(tmp_path):
    from PIL import Image
    store = Store(tmp_path / 'run')
    record = valid_case()
    before = np.full((100, 200, 3), 255, np.uint8)
    after = before.copy()
    after[10:30, 10:50] = 0
    mask = np.any(before != after, axis=2).astype(np.uint8) * 255
    record['assets'] = {'reference': store.put(png(before)), 'current': store.put(png(after)),
                        'raw_change_mask': store.put(png(mask))}
    accepted(store, record)
    output = tmp_path / 'tiles'
    result = export_run(store.root, output, 'yolo-singleclass', tile_size=80, tile_overlap=20)
    assert result['cases'] == 1 and result['tiles'] == 5
    assert next((output / 'originals').rglob('*.png')).read_bytes() == png(after)
    entries = [json.loads(line) for line in (output / 'tiles.jsonl').read_text().splitlines()]
    assert len(entries) == 5
    assert all(entry['case_id'] == record['case_id'] and entry['split'] == record['split'] for entry in entries)
    labels = [path.read_text().split() for path in (output / 'labels').rglob('*.txt')]
    assert sum(bool(label) for label in labels) == 1
    assert next(label for label in labels if label) == ['0', '0.375000000', '0.250000000', '0.500000000', '0.250000000']
    for path in (output / 'images').rglob('*.png'):
        with Image.open(path) as image:
            assert image.size == (80, 80)


@pytest.mark.parametrize('size,overlap', [(0, 0), (64, 64), (64, -1)])
def test_invalid_tile_geometry_is_rejected_before_output(tmp_path, size, overlap):
    with pytest.raises(ValueError, match='tile'):
        export_run(tmp_path / 'run', tmp_path / 'out', 'yolo-singleclass', tile_size=size, tile_overlap=overlap)
    assert not (tmp_path / 'out').exists()


def test_tiles_reject_uncovered_issues_and_bound_excessive_work():
    from lumen_data.exports import tile_regions
    record = valid_case()
    record['capture']['image_px'] = [1920, 1080]
    record['issues'][0]['bbox_xyxy_px'] = [850, 10, 1100, 100]
    with pytest.raises(ValueError, match='coverage'):
        tile_regions(record, 1024, 128)
    with pytest.raises(ValueError, match='256'):
        tile_regions(record, 32, 31)


def test_tile_edges_are_half_open_and_small_images_are_not_padded():
    from lumen_data.exports import tile_regions
    record = valid_case()
    record['capture']['image_px'] = [100, 50]
    record['issues'][0]['bbox_xyxy_px'] = [0, 0, 50, 50]
    assert tile_regions(record, 50, 0) == [([0, 0, 50, 50], [0]), ([50, 0, 100, 50], [])]
    assert tile_regions(record, 1024, 128) == [([0, 0, 100, 50], [0])]


def test_partial_second_issue_excludes_tile_with_first_issue_inside():
    from lumen_data.exports import tile_regions
    record = valid_case()
    record['capture']['image_px'] = [150, 50]
    record['issues'] = [{'bbox_xyxy_px': [60, 10, 80, 30]}, {'bbox_xyxy_px': [90, 10, 120, 30]}]
    assert tile_regions(record, 100, 50) == [([50, 0, 150, 50], [0, 1])]


def test_positive_tile_offsets_match_cropped_pixels(tmp_path):
    from PIL import Image
    store = Store(tmp_path / 'run')
    record = valid_case()
    record['issues'][0]['bbox_xyxy_px'] = [130, 50, 170, 70]
    record['issues'][0]['context_bbox_xyxy_px'] = [110, 30, 190, 90]
    before = np.full((100, 200, 3), 255, np.uint8)
    after = before.copy()
    after[50:70, 130:170] = 0
    mask = np.any(before != after, axis=2).astype(np.uint8) * 255
    record['assets'] = {'reference': store.put(png(before)), 'current': store.put(png(after)),
                        'raw_change_mask': store.put(png(mask))}
    accepted(store, record)
    output = tmp_path / 'tiles'
    export_run(store.root, output, 'yolo-singleclass', tile_size=80, tile_overlap=20)
    identity = record['case_id'] + '-120-20'
    label = (output / 'labels' / record['split'] / f'{identity}.txt').read_text().split()
    assert list(map(float, label)) == [0, .375, .5, .5, .25]
    with Image.open(output / 'images' / record['split'] / f'{identity}.png') as image:
        assert np.array_equal(np.array(image), after[20:100, 120:200])
