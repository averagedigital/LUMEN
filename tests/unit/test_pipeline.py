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


@pytest.mark.parametrize('variants', [1, 2, 7, 10, 16])
def test_scene_family_does_not_determine_positive_or_negative(variants):
    classes = {family: set() for family in ARCHETYPES}
    for index in range(4000):
        payload = recipe(index, {'variants_per_scene': variants, 'seed': 123}, 'v1')
        classes[payload['scene']['archetype']].add(payload['operator'].startswith(('clean', 'benign')))
    assert all(values == {True, False} for values in classes.values())


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


def test_registered_source_retains_verified_stylesheet(tmp_path, monkeypatch):
    import hashlib
    store = Store(tmp_path / 'run')
    source_assets = tmp_path / 'assets'
    source_assets.mkdir()
    css = b'body { color: black; }'
    digest = hashlib.sha256(css).hexdigest()
    (source_assets / digest).write_bytes(css)
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(source_assets))
    path = tmp_path / 'source.jsonl'
    path.write_text(json.dumps({'base_scene_id': 'one', 'html': '<h1>Title</h1>',
        'assets': {'https://example.invalid/style.css': {'sha256': digest, 'content_type': 'text/css'}}}) + '\n')
    register_sources(store, path)
    assert (store.root / 'assets' / digest).read_bytes() == css
