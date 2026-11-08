import pytest
from lumen_data.modern_corpus import webui_scene


def row(**updates):
    return dict(sample_id='abc123', html='<main id="app"></main>', css='main{color:black}',
                js='document.getElementById("app").textContent="Account settings";',
                source_name='github', source_url='https://example.org/project', viewport='desktop',
                **updates)


def test_webui_assembly_preserves_code_and_groups_viewports():
    first = row()
    a = webui_scene(first, revision='a' * 40, split='train')
    first['viewport'] = 'mobile'
    b = webui_scene(first, revision='a' * 40, split='train')
    assert a['base_scene_id'] == b['base_scene_id']
    assert a['split_group'] == b['split_group']
    assert a['render_mode'] == 'offline-js'
    assert first['js'].encode() in a['_asset_bodies'].values()
    assert first['css'].encode() in a['_asset_bodies'].values()
    assert all(url in a['html'] for url in a['assets'])
    assert a['provenance']['upstream_split'] == 'train'
    assert a['provenance']['public_release_status'] == 'research_only'


def test_distinct_samples_from_same_page_stay_in_one_group():
    first = row()
    a = webui_scene(first, revision='a' * 40, split='train')
    first['sample_id'] = 'different'
    first['source_url'] += '?theme=dark#preview'
    b = webui_scene(first, revision='a' * 40, split='train')
    assert a['base_scene_id'] != b['base_scene_id']
    assert a['split_group'] == b['split_group']


@pytest.mark.parametrize('field,value', [('html', None), ('css', []), ('js', 3),
                                        ('source_url', 'file:///etc/passwd'), ('sample_id', '')])
def test_bad_source_fields_fail_explicitly(field, value):
    value_row = row()
    value_row[field] = value
    with pytest.raises(ValueError):
        webui_scene(value_row, revision='a' * 40, split='train')


def test_script_terminator_is_not_silently_rewritten():
    value_row = row()
    value_row['js'] = 'const x="</script><img src=x>";'
    result = webui_scene(value_row, revision='a' * 40, split='train')
    assert '</script><img src=x>' not in result['html']
    assert result['assets']


def test_worker_payload_keeps_execution_mode(tmp_path):
    from lumen_data.pipeline import worker_payload
    from lumen_data.storage import Store
    store = Store(tmp_path)
    try:
        digest = store.put(b'<p>Ready</p>', 'html')
        payload = worker_payload(store, {'source': {'html_hash': digest, 'assets': {},
                                                   'render_mode': 'offline-js'}})
        assert payload['render_mode'] == 'offline-js'
    finally:
        store.db.close()


def test_import_all_shards_resume_and_reject_changed_contract(tmp_path, monkeypatch):
    import json
    import pyarrow as pa
    import pyarrow.parquet as pq
    from lumen_data.modern_corpus import import_webui
    monkeypatch.setattr('lumen_data.modern_corpus.guard', lambda path: None)
    snapshot = tmp_path / 'snapshot'
    (snapshot / 'data').mkdir(parents=True)
    rows = []
    for viewport in ['mobile', 'desktop', 'tablet']:
        value = row()
        value['viewport'] = viewport
        value['image'] = {'bytes': b'fixture-image', 'path': None}
        rows.append(value)
    pq.write_table(pa.Table.from_pylist(rows), snapshot / 'data/train-00000.parquet')
    output = tmp_path / 'ready'
    result = import_webui(snapshot, output, 'a' * 40)
    assert result['samples'] == 1 and result['viewports'] == 3
    record = json.loads((output / 'manifest.jsonl').read_text())
    assert '_asset_bodies' not in record
    assert all((output / 'assets' / value['sha256']).exists() for value in record['assets'].values())
    assert import_webui(snapshot, output, 'a' * 40) == result
    with pytest.raises(ValueError, match='contract changed'):
        import_webui(snapshot, output, 'b' * 40)


def test_mobile_view_cannot_hide_cross_split_sample(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from lumen_data.modern_corpus import import_webui
    monkeypatch.setattr('lumen_data.modern_corpus.guard', lambda path: None)
    (tmp_path / 'input/data').mkdir(parents=True)
    for split, viewport in [('test', 'mobile'), ('train', 'desktop')]:
        value = row()
        value.update(viewport=viewport, image={'bytes': b'image', 'path': None})
        pq.write_table(pa.Table.from_pylist([value]), tmp_path / f'input/data/{split}-00000.parquet')
    with pytest.raises(ValueError, match='crosses upstream splits'):
        import_webui(tmp_path / 'input', tmp_path / 'output', 'a' * 40)


def test_resume_rejects_partial_shard_replacement(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import sqlite3
    from lumen_data.modern_corpus import import_webui
    root = tmp_path / 'output'
    def stop_after_batch(path):
        if (root / 'import.sqlite').exists():
            with sqlite3.connect(root / 'import.sqlite') as db:
                if db.execute("SELECT count(*) FROM sqlite_master WHERE name='samples'").fetchone()[0]:
                    if db.execute('SELECT count(*) FROM samples').fetchone()[0]:
                        raise RuntimeError('interruption')
    monkeypatch.setattr('lumen_data.modern_corpus.guard', stop_after_batch)
    (tmp_path / 'input/data').mkdir(parents=True)
    rows = []
    for i in range(10):
        value = row()
        value.update(sample_id=str(i), image={'bytes': b'image', 'path': None})
        rows.append(value)
    shard = tmp_path / 'input/data/train-00000.parquet'
    pq.write_table(pa.Table.from_pylist(rows), shard)
    with pytest.raises(RuntimeError, match='interruption'):
        import_webui(tmp_path / 'input', root, 'a' * 40)
    pq.write_table(pa.Table.from_pylist(rows[:1]), shard)
    monkeypatch.setattr('lumen_data.modern_corpus.guard', lambda path: None)
    with pytest.raises(ValueError, match='shard changed'):
        import_webui(tmp_path / 'input', root, 'a' * 40)


def test_existing_corrupt_asset_is_not_silently_used(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from lumen_data.modern_corpus import import_webui
    monkeypatch.setattr('lumen_data.modern_corpus.guard', lambda path: None)
    (tmp_path / 'input/data').mkdir(parents=True)
    (tmp_path / 'output/assets').mkdir(parents=True)
    value = row()
    value['image'] = {'bytes': b'image', 'path': None}
    record = webui_scene(value, revision='a' * 40, split='train')
    digest = next(iter(record['_asset_bodies']))
    (tmp_path / 'output/assets' / digest).write_bytes(b'truncated')
    pq.write_table(pa.Table.from_pylist([value]), tmp_path / 'input/data/train-00000.parquet')
    with pytest.raises(ValueError, match='asset checksum'):
        import_webui(tmp_path / 'input', tmp_path / 'output', 'a' * 40)
