import io

import numpy as np
import pytest
from PIL import Image

from lumen_data.pipeline import build_record, image_artifacts
from lumen_data.pipeline import recipe, ARCHETYPES
from lumen_data.pipeline import register_sources, worker_payload
import json
from lumen_data.storage import Store


def png(array):
    stream = io.BytesIO()
    Image.fromarray(array).save(stream, format='PNG')
    return stream.getvalue()


def test_exact_change_mask_and_case_flags(tmp_path):
    before = np.full((100, 200, 3), 255, np.uint8)
    after = before.copy()
    after[10:20, 30:50] = 0
    store = Store(tmp_path)
    assets, stats = image_artifacts(store, png(before), png(after), [200, 100])
    mask = np.array(Image.open(io.BytesIO(store.read(assets['raw_change_mask']))))
    assert np.count_nonzero(mask) == 200
    assert stats['changed_pixels'] == 200
    assert assets['reference'] != assets['current']


def test_wrong_dimensions_and_no_local_effect_are_rejected(tmp_path):
    white = png(np.full((100, 200, 3), 255, np.uint8))
    with pytest.raises(ValueError, match='dimensions'):
        image_artifacts(Store(tmp_path), white, white, [200, 200])
    with pytest.raises(ValueError):
        build_record({}, {'status': 'unverified'}, {}, {})


def test_source_split_group_does_not_replace_provenance_or_lineage():
    from lumen_data.contracts import group_split
    payload = recipe(0, {'variants_per_scene': 10, 'seed': 123}, 'v1')
    payload['source'] = {'base_scene_id': 'base-one', 'source_project_id': 'websight',
        'template_lineage_id': None, 'structural_family_id': 'table-form',
        'split_group': 'dom-v1:independent-structure',
        'provenance': {'license': 'test', 'revision': 'source-v1'}}
    payload['scene'] = None
    record = build_record(payload, {'status': 'accepted_clean', 'issues': [], 'browser': 'fixture'},
                          dict.fromkeys(('reference', 'current', 'raw_change_mask'), 'a' * 64),
                          {'changed_pixels': 0})
    assert record['source_project_id'] == 'websight'
    assert record['template_lineage_id'] is None
    assert record['base_scene_id'] == 'base-one'
    assert record['structural_family_id'] == 'table-form'
    assert record['split_group'] == 'dom-v1:independent-structure'
    assert record['split'] == group_split(record['split_group'])


def test_component_operators_are_scheduled_without_removing_text_operators():
    from lumen_data.pipeline import OPERATORS
    positives = {op for op in OPERATORS if op != 'clean' and not op.startswith('benign_')}
    assert positives == {'text_clip_x', 'text_clip_y', 'text_contrast_failure',
        'viewport_partial_cutoff', 'sibling_occlusion', 'flex_overflow', 'grid_track_collapse',
        'container_height_collapse', 'header_collision', 'table_cell_overlap', 'dropdown_clipping',
        'modal_content_overflow', 'input_adornment_collision', 'image_aspect_distortion', 'svg_clipping',
        'font_size_regression', 'nowrap_regression', 'word_break_regression',
        'letter_spacing_regression', 'line_height_regression', 'font_family_regression',
        'text_indent_regression', 'relative_offset_regression', 'padding_box_regression'}


def test_cause_region_and_visible_scenario_metadata_are_separate_from_issue_boxes():
    payload = recipe(0, {'variants_per_scene': 10, 'seed': 123}, 'v3')
    payload.update(operator='grid_track_collapse', viewport=[800, 600], dpr=2)
    finding = {'id': 'grid', 'symptom': 'grid_track_collapse', 'family': 'layout', 'bbox': [20, 20, 80, 80],
        'evidence': {'oracle_id': 'component-grid-v1', 'baseline_passed': True, 'after_failed': True}}
    result = {'status': 'accepted_bug', 'issues': [finding], 'browser': 'fixture',
        'receipt': {'operator': payload['operator'], 'target': 'grid', 'params': {},
                    'cause_region_css': {'x': 5, 'y': 5, 'width': 300, 'height': 100}},
        'scenario_tags': ['sticky_header'],
        'scene': {'html': '<main><section><button>Save</button></section></main>',
                  'page': {'layout_variant': 'dashboard/detail'}, 'state': {'hard_negative_types': ['hidden_dialog']}}}
    record = build_record(payload, result,
                          dict.fromkeys(('reference', 'current', 'raw_change_mask'), 'a' * 64),
                          {'changed_pixels': 100})
    assert record['cause_region_xyxy_px'] == [10, 10, 610, 210]
    assert record['issues'][0]['bbox_xyxy_px'] == [20, 20, 80, 80]
    assert record['scenario_tags'] == ['sticky_header']
    assert 'component_geometry' in record['declared_scope']
    assert record['template_lineage_id'] == 'lumen-grammar-v3/dashboard'
    assert record['split_group'] == 'lineage:lumen-grammar-v3/dashboard'
    assert record['structure']['split_group'].startswith('dom-v1:')
    result.update(status='accepted_clean', issues=[], receipt=None)
    negative = build_record(payload, result, record['assets'], {'changed_pixels': 0})
    assert negative['scenario_tags'] == record['scenario_tags']
    assert 'cause_region_xyxy_px' not in negative


@pytest.mark.parametrize('variants', [1, 2, 7, 10, 16])
def test_scene_family_does_not_determine_positive_or_negative(variants):
    classes = {family: set() for family in ARCHETYPES}
    for index in range(4000):
        payload = recipe(index, {'variants_per_scene': variants, 'seed': 123}, 'v1')
        classes[payload['scene']['archetype']].add(payload['operator'].startswith(('clean', 'benign')))
    assert all(values == {True, False} for values in classes.values())


@pytest.mark.parametrize('source_manifest', [None, 'prepared.jsonl'])
def test_each_family_can_reach_odd_even_and_both_layout_states(source_manifest):
    states = {family: set() for family in ARCHETYPES}
    for base in range(1000):
        if source_manifest and base % 2:
            continue
        payload = recipe(base * 10, {'variants_per_scene': 10, 'seed': 123,
                                    'source_manifest': source_manifest}, 'v3')
        scene = payload['scene']
        states[scene['archetype']].add((scene['seed'] % 2, scene['seed'] // 3 % 2))
    assert all(values == {(0, 0), (0, 1), (1, 0), (1, 1)} for values in states.values())


def test_prepared_source_html_is_shared_and_resume_does_not_duplicate(tmp_path):
    store = Store(tmp_path / 'run')
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps({'base_scene_id': 'source-one', 'html': '<h1>Account</h1>', 'assets': {}}) + '\n')
    register_sources(store, path)
    register_sources(store, path)
    rows = store.db.execute('SELECT record FROM source_scenes').fetchall()
    assert len(rows) == 1
    source = json.loads(rows[0][0])
    assert 'html' not in source
    request = worker_payload(store, {'source': source, 'scene': None})
    assert request['html'] == '<h1>Account</h1>'


def test_source_registration_stops_before_next_write_at_disk_reserve(tmp_path, monkeypatch):
    from lumen_data import pipeline
    store = Store(tmp_path / 'run')
    path = tmp_path / 'source.jsonl'
    path.write_text(''.join(json.dumps({'base_scene_id': str(i), 'html': '<p>Text</p>'}) + '\n' for i in range(3)))

    def guard(root):
        assert root == store.root
        if store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 1:
            raise RuntimeError('disk reserve reached')

    monkeypatch.setattr(pipeline, 'guard', guard)
    with pytest.raises(RuntimeError, match='disk reserve'):
        register_sources(store, path)
    assert store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 1


def test_mixed_sources_keep_all_own_scene_families():
    config = {'variants_per_scene': 10, 'seed': 123, 'source_manifest': 'prepared.jsonl'}
    own = {recipe(base * 10, config, 'v1')['scene']['archetype'] for base in range(0, 80, 2)}
    assert own == set(ARCHETYPES)


@pytest.mark.parametrize('content_type,suffix,options', [
    ('text/css', 'css', {}), ('font/woff2', 'woff2', {}), ('image/jpeg', 'jpg', {}),
    ('image/png', 'png', {}), ('image/svg+xml', 'svg', {}), ('image/webp', 'webp', {}),
    ('image/webp', 'webp', {'lossless': True}), ('image/webp', 'webp', {'icc_profile': b'test'})])
def test_registered_source_retains_verified_asset(tmp_path, monkeypatch, content_type, suffix, options):
    import hashlib
    store = Store(tmp_path / 'run')
    source_assets = tmp_path / 'assets'
    source_assets.mkdir()
    content = b'body { color: black; }'
    if suffix in ('jpg', 'png', 'webp'):
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream,
            format={'jpg': 'JPEG', 'png': 'PNG', 'webp': 'WEBP'}[suffix], **options)
        content = stream.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    (source_assets / digest).write_bytes(content)
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(source_assets))
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps({'base_scene_id': 'one', 'html': '<h1>Title</h1>',
        'assets': {'https://example.invalid/asset': {'sha256': digest, 'content_type': content_type}}}) + '\n')
    register_sources(store, path)
    assert (store.root / 'assets' / digest).read_bytes() == content
    assert store.read(digest, suffix) == content


@pytest.mark.parametrize('content_type,content,error', [
    ('text/html', b'original', 'unsupported source asset'),
    ('image/svg+xml', b'tampered', 'source asset checksum')])
def test_source_asset_rejected_before_scene_registration(tmp_path, monkeypatch, content_type, content, error):
    import hashlib
    store = Store(tmp_path / 'run')
    digest = hashlib.sha256(b'original').hexdigest()
    (tmp_path / digest).write_bytes(content)
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(tmp_path))
    manifest = tmp_path / 'source.jsonl'
    manifest.write_text(json.dumps({'base_scene_id': 'one', 'html': '<h1>Title</h1>',
        'assets': {'https://example.invalid/asset': {'sha256': digest, 'content_type': content_type}}}) + '\n')
    with pytest.raises(ValueError, match=error):
        register_sources(store, manifest)
    assert store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['oversized', 'animated', 'wrong_format'])
def test_raster_source_rejected_before_browser_decode(tmp_path, monkeypatch, kind):
    import hashlib
    import struct
    import zlib
    stream = io.BytesIO()
    first = Image.new('RGB', (2, 2), 'red')
    first.save(stream, format='PNG', save_all=True, append_images=[Image.new('RGB', (2, 2), 'blue')]
               if kind == 'animated' else [])
    content = stream.getvalue()
    if kind == 'oversized':
        # Change only IHDR: the rejection must not allocate the declared bitmap.
        header = b'IHDR' + struct.pack('>II', 5000, 5000) + content[24:29]
        content = content[:12] + header + struct.pack('>I', zlib.crc32(header)) + content[33:]
        with Image.open(io.BytesIO(content)) as image:
            assert image.size == (5000, 5000)
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / digest).write_bytes(content)
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(tmp_path))
    store = Store(tmp_path / 'run')
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps({'base_scene_id': 'one', 'html': '<h1>Title</h1>',
        'assets': {'https://example.invalid/image': {'sha256': digest,
            'content_type': 'image/jpeg' if kind == 'wrong_format' else 'image/png'}}}) + '\n')
    with pytest.raises(ValueError, match='source raster'):
        register_sources(store, path)
    assert store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 0


@pytest.mark.parametrize('kind', ['count', 'bytes', 'pixels'])
def test_source_asset_aggregate_budget(tmp_path, monkeypatch, kind):
    import hashlib
    import struct
    import zlib
    content = b' ' * (8 * 2**20) if kind == 'bytes' else png(np.zeros((2, 2, 3), np.uint8))
    if kind == 'pixels':
        header = b'IHDR' + struct.pack('>II', 4096, 4096) + content[24:29]
        content = content[:12] + header + struct.pack('>I', zlib.crc32(header)) + content[33:]
        with Image.open(io.BytesIO(content)) as image:
            assert image.size == (4096, 4096)
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / digest).write_bytes(content)
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(tmp_path))
    store = Store(tmp_path / 'run')
    path = tmp_path / 'source.jsonl'
    assets = {f'https://example.invalid/{i}': {'sha256': digest,
        'content_type': 'image/png' if kind == 'pixels' else 'text/css'}
        for i in range(129 if kind == 'count' else 5)}
    path.write_text(json.dumps({'base_scene_id': 'one', 'html': '<h1>Title</h1>', 'assets': assets}) + '\n')
    with pytest.raises(ValueError, match='source asset budget'):
        register_sources(store, path)
    assert store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 0


@pytest.mark.parametrize('chunk,mime', [('VP8 ', 'image/webp'), ('VP8L', 'image/webp'),
    ('VP8X', 'image/webp'), ('VP8X', 'image/png')])
def test_oversized_webp_rejected_before_decoder_allocation(tmp_path, monkeypatch, chunk, mime):
    import hashlib
    import struct
    from PIL import WebPImagePlugin
    width = height = 5000
    payloads = {
        'VP8 ': b'\x00\x00\x00\x9d\x01\x2a' + struct.pack('<HH', width, height),
        'VP8L': b'\x2f' + struct.pack('<I', (width - 1) | ((height - 1) << 14)),
        'VP8X': b'\x00' * 4 + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')}
    payload = payloads[chunk]
    body = b'WEBP' + chunk.encode() + struct.pack('<I', len(payload)) + payload + b'\x00' * (len(payload) % 2)
    content = b'RIFF' + struct.pack('<I', len(body)) + body
    digest = hashlib.sha256(content).hexdigest()
    (tmp_path / digest).write_bytes(content)
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(tmp_path))
    def forbidden_decoder(*args):
        raise AssertionError('oversized WebP reached decoder allocation')
    monkeypatch.setattr(WebPImagePlugin._webp, 'WebPAnimDecoder', forbidden_decoder)
    store = Store(tmp_path / 'run')
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps({'base_scene_id': 'one', 'html': '<h1>Title</h1>',
        'assets': {'https://example.invalid/image': {'sha256': digest, 'content_type': mime}}}) + '\n')
    with pytest.raises(ValueError, match='source raster'):
        register_sources(store, path)
    assert store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 0


def test_fatal_worker_error_is_preserved_in_checkpoint(tmp_path, monkeypatch):
    from lumen_data import pipeline
    import yaml

    class FailedWorker:
        def __init__(self, asset_root):
            pass

        def request(self, payload):
            return {'status': 'resource_error', 'issues': [], 'error': 'browser process exited'}

        def close(self):
            pass

    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'seed': 1, 'target': 1, 'max_attempts': 1,
        'variants_per_scene': 1, 'source_manifest': None, 'min_disk_gib': 0, 'min_ram_gib': 0}))
    monkeypatch.setattr(pipeline, 'Worker', FailedWorker)
    with pytest.raises(RuntimeError, match='resource_error'):
        pipeline.generate(config, tmp_path / 'run')
    store = Store(tmp_path / 'run')
    result = json.loads(store.db.execute('SELECT result FROM tasks').fetchone()[0])
    assert result['detail'] == 'browser process exited'
