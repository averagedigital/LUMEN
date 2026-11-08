import base64
import hashlib
import io
import json
import sys

import pytest
from PIL import Image, ImageDraw

from lumen_data.live_capture import import_live
from lumen_data.storage import Store
from lumen_data.pipeline import check_case_assets


def fixture(tmp_path, monkeypatch, mutate=None):
    import lumen_data.live_capture as module
    import lumen_data.pipeline as pipeline
    monkeypatch.setattr(module, 'guard', lambda *args: None)
    monkeypatch.setattr(pipeline, 'guard', lambda *args: None)
    root = tmp_path/'input'
    (root/'live-cases').mkdir(parents=True)
    (root/'assets').mkdir()
    image = Image.new('RGB', (240, 240), 'white')
    output = io.BytesIO()
    image.save(output, format='PNG')
    reference = output.getvalue()
    ImageDraw.Draw(image).rectangle((40, 40, 119, 79), fill='black')
    output = io.BytesIO()
    image.save(output, format='PNG')
    current = output.getvalue()
    html = '<html><body><span>Save account settings</span></body></html>'
    source = {'html': html, 'assets': {}, 'base_scene_id': 'c'*64,
        'source_project_id': 'demo', 'template_lineage_id': 'demo', 'split_group': 'web-project:demo',
        'structural_family_id': 'dashboard', 'corpus': 'unclassified',
        'capture_mode': 'live-session', 'render_mode': 'live-session',
        'source_sha256': hashlib.sha256(reference).hexdigest(),
        'provenance': {'license': 'MIT', 'public_release_status': 'research_only',
            'revision': hashlib.sha256(html.encode()).hexdigest(), 'collector_revision': 'a'*64}}
    records = []
    for positive in [True, False]:
        row = {'payload': {'source': source, 'scene': None, 'seed': 42+int(positive),
            'viewport': [240, 240], 'dpr': 1, 'locale': 'en-US', 'generator_revision': 'a'*64,
            'operator': 'text_clip_x' if positive else 'clean'},
            'result': {'status': 'accepted_bug' if positive else 'accepted_clean',
                'capture_mode': 'live-session', 'rollback': True, 'browser': 'fixture',
                'reference': base64.b64encode(reference).decode(),
                'current': base64.b64encode(current if positive else reference).decode(),
                'issues': [{'id': 'label', 'family': 'text_geometry', 'symptom': 'text_clip_x',
                    'bbox': [40, 40, 120, 80], 'evidence': {'baseline_passed': True,
                    'after_failed': True, 'oracle_id': 'fixture-clipping'}}] if positive else []}}
        if mutate:
            mutate(row)
        data = json.dumps(row).encode()
        digest = hashlib.sha256(data).hexdigest()
        (root/'live-cases'/f'{digest}.json').write_bytes(data)
        records.append({'sha256': digest, 'base_scene_id': source['base_scene_id']})
    manifest = root/'live-manifest.jsonl'
    manifest.write_text(''.join(json.dumps(r)+'\n' for r in records))
    return manifest, root/'assets', tmp_path/'run'


def test_live_pairs_use_standard_masks_boxes_and_idempotent_checkpoint(tmp_path, monkeypatch):
    manifest, assets, run = fixture(tmp_path, monkeypatch)
    report = import_live(manifest, assets, run)
    assert report['accepted'] == 2
    store = Store(run)
    records = list(store.records())
    assert {r['has_single_frame_defect'] for r in records} == {True, False}
    assert len({r['base_scene_id'] for r in records}) == 1
    assert len({r['split'] for r in records}) == 1
    for r in records:
        assert r['capture']['capture_mode'] == 'live-session'
        assert r['capture']['independent_replay'] is False
        assert r['provenance']['public_release_status'] == 'research_only'
        check_case_assets(store, r)
    assert import_live(manifest, assets, run)['new_cases'] == 0
    assert Store(run).count() == 2


@pytest.mark.parametrize('mutate', [
    lambda r: r['result'].update(rollback=False),
    lambda r: r['result'].update(capture_mode='offline-replay'),
    lambda r: r['payload']['source'].update(capture_mode='offline-replay'),
    lambda r: r['payload']['source'].update(source_sha256='0'*64),
    lambda r: r['payload']['source'].update(split_group='another-project'),
    lambda r: r['payload'].update(operator='unknown'),
    lambda r: r['payload'].update(dpr=3),
    lambda r: r['payload']['source'].update(base_scene_id='not-a-digest'),
    lambda r: r['result'].update(status='accepted_bug', issues=[]),
    lambda r: r['result'].update(current=r['result']['reference']),
    lambda r: (r['result'].update(status='accepted_clean', issues=[]), r['payload'].update(operator='clean')),
    lambda r: r['result'].update(status='accepted_benign_change'),
])
def test_import_rejects_unverified_or_inconsistent_pairs(tmp_path, monkeypatch, mutate):
    manifest, assets, run = fixture(tmp_path, monkeypatch, mutate)
    with pytest.raises(ValueError):
        import_live(manifest, assets, run)
    assert Store(run).count() == 0


def test_import_checks_raw_object_hash_and_rejects_path_escape(tmp_path, monkeypatch):
    manifest, assets, run = fixture(tmp_path, monkeypatch)
    row = json.loads(manifest.read_text().splitlines()[0])
    path = manifest.parent/'live-cases'/f'{row["sha256"]}.json'
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError, match='checksum'):
        import_live(manifest, assets, run)
    manifest.write_text(json.dumps({'sha256': '../outside', 'base_scene_id': 'c'*64})+'\n')
    with pytest.raises(ValueError, match='digest'):
        import_live(manifest, assets, tmp_path/'other-run')


def test_live_reproduce_fails_before_starting_an_offline_worker(tmp_path, monkeypatch, capsys):
    from lumen_data import cli
    manifest, assets, run = fixture(tmp_path, monkeypatch)
    import_live(manifest, assets, run)
    record = next(Store(run).records())
    monkeypatch.setattr(cli, 'Worker', lambda *a, **kw: pytest.fail('live pair attempted offline replay'))
    monkeypatch.setattr(sys, 'argv', ['lumen-data', 'reproduce', '--run', str(run), '--case', record['case_id']])
    assert cli.main() == 1
    assert 'independent replay' in capsys.readouterr().err


def test_import_does_not_mix_with_a_generation_run(tmp_path, monkeypatch):
    manifest, assets, run = fixture(tmp_path, monkeypatch)
    run.mkdir()
    (run/'run.json').write_text('{}')
    with pytest.raises(ValueError, match='generation'):
        import_live(manifest, assets, run)
    assert (run/'run.json').read_text() == '{}'


def test_generation_does_not_mix_with_a_live_import(tmp_path, monkeypatch):
    from lumen_data.pipeline import generate
    manifest, assets, run = fixture(tmp_path, monkeypatch)
    import_live(manifest, assets, run)
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'seed': 42, 'target': 1, 'max_attempts': 2,
        'variants_per_scene': 1, 'source_manifest': None}))
    with pytest.raises(ValueError, match='live-import'):
        generate(config, run)


@pytest.mark.parametrize('operation', ['import', 'generate'])
def test_run_type_is_checked_after_acquiring_shared_lock(tmp_path, monkeypatch, operation):
    import lumen_data.live_capture as module
    import lumen_data.pipeline as pipeline
    manifest, assets, run = fixture(tmp_path, monkeypatch)
    original = module.fcntl.flock
    def interleaved(file, flags):
        marker = run/('run.json' if operation == 'import' else 'live-import.json')
        marker.write_text('{}')
        return original(file, flags)
    monkeypatch.setattr(module.fcntl, 'flock', interleaved)
    if operation == 'import':
        with pytest.raises(ValueError, match='generation'):
            import_live(manifest, assets, run)
    else:
        config = tmp_path/'config.json'
        config.write_text(json.dumps({'seed': 42, 'target': 1, 'max_attempts': 2,
            'variants_per_scene': 1, 'source_manifest': None, 'min_disk_gib': 40, 'min_ram_gib': 1}))
        monkeypatch.setattr(pipeline, 'Worker', lambda *a, **kw: pytest.fail('mixed run started a worker'))
        with pytest.raises(ValueError, match='live-import'):
            pipeline.generate(config, run)
