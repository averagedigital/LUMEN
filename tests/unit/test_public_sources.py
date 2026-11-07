import base64
import hashlib
import io
import json

from PIL import Image
import pytest

from lumen_data import public_sources
from lumen_data.pipeline import register_sources
from lumen_data.storage import Store


def candidate(url='https://demo.example.invalid/hero'):
    return {'url': url, 'allowed_hosts': ['demo.example.invalid'], 'kind': 'block',
            'source_project_id': 'demo', 'template_lineage_id': 'demo/hero',
            'structural_family_id': 'hero', 'provenance': {'license': 'unknown'}}


def captured(html='<h1>Settings</h1>'):
    image = io.BytesIO()
    Image.new('RGB', (16, 16), 'white').save(image, format='PNG')
    content = image.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    return {'status': 'captured', 'html': html, 'scene_id': hashlib.sha256(html.encode()).hexdigest(),
            'reference': base64.b64encode(content).decode(), 'assets': {},
            'source_sha256': digest, 'restored_sha256': digest,
            'source_url': candidate()['url'], 'final_url': candidate()['url'],
            'captured_at': '2026-11-07T00:00:00Z', 'defect_status': 'unverified',
            'elements_status': 'captured', 'elements': [{'element_id': 'el-1', 'role': 'heading'}],
            'viewport': [16, 16], 'dpr': 1}


def setup_capture(tmp_path, monkeypatch, results):
    calls = []

    class Worker:
        def __init__(self, **kwargs):
            pass

        def request(self, payload):
            calls.append(payload)
            return results[len(calls) - 1]

        def close(self):
            pass

    monkeypatch.setattr(public_sources, 'Worker', Worker)
    monkeypatch.setattr(public_sources, 'guard', lambda path: None)
    manifest = tmp_path / 'candidates.jsonl'
    manifest.write_text(json.dumps(candidate()) + '\n')
    return manifest, tmp_path / 'captures', calls


def test_capture_checkpoint_rebuilds_manifest_without_recapturing(tmp_path, monkeypatch):
    manifest, root, calls = setup_capture(tmp_path, monkeypatch, [captured()])
    result = public_sources.capture_sources(manifest, root, limit=1)
    assert result['captured'] == 1
    record = json.loads((root / 'manifest.jsonl').read_text())
    assert record['html'] == '<h1>Settings</h1>'
    assert record['provenance']['public_release_status'] == 'research_only'
    sidecar = json.loads((root / 'annotations' / 'elements' / f"{record['base_scene_id']}.json").read_text())
    assert sidecar['scene_id'] == record['base_scene_id']
    assert sidecar['elements'][0]['element_id'] == 'el-1'
    (root / 'manifest.jsonl').write_text('interrupted projection')
    resumed = public_sources.capture_sources(manifest, root, limit=1)
    assert resumed['captured'] == 1
    assert len(calls) == 1
    assert json.loads((root / 'manifest.jsonl').read_text()) == record
    monkeypatch.setenv('LUMEN_ASSET_ROOT', str(root / 'assets'))
    store = Store(tmp_path / 'generation')
    register_sources(store, root / 'manifest.jsonl')
    assert store.db.execute('SELECT count(*) FROM source_scenes').fetchone()[0] == 1


def test_capture_rejections_are_checkpointed_and_not_retried(tmp_path, monkeypatch):
    manifest, root, calls = setup_capture(tmp_path, monkeypatch,
                                         [{'status': 'rejected', 'error': 'unstable source pixels'}])
    for _ in range(2):
        result = public_sources.capture_sources(manifest, root, limit=1)
        assert result['rejected'] == 1
        assert result['captured'] == 0
    assert len(calls) == 1
    assert (root / 'manifest.jsonl').read_text() == ''


def test_capture_exact_duplicate_pixels_do_not_count_as_two_designs(tmp_path, monkeypatch):
    manifest, root, calls = setup_capture(tmp_path, monkeypatch, [captured(), captured('<h1>Other</h1>')])
    manifest.write_text('\n'.join(json.dumps(candidate(url)) for url in
                        ['https://demo.example.invalid/hero', 'https://demo.example.invalid/copy']) + '\n')
    result = public_sources.capture_sources(manifest, root, limit=2)
    assert len(calls) == 2
    assert result['captured'] == 1
    assert result['duplicates'] == 1
    assert len((root / 'manifest.jsonl').read_text().splitlines()) == 1


@pytest.mark.parametrize('field,value', [('source_sha256', '0' * 64), ('scene_id', '0' * 64),
                                       ('restored_sha256', '0' * 64)])
def test_capture_refuses_corrupted_worker_payload(tmp_path, monkeypatch, field, value):
    response = captured()
    response[field] = value
    manifest, root, _ = setup_capture(tmp_path, monkeypatch, [response])
    with pytest.raises(ValueError, match='checksum'):
        public_sources.capture_sources(manifest, root, limit=1)


def test_capture_rejects_component_candidates_before_browser(tmp_path, monkeypatch):
    manifest, root, calls = setup_capture(tmp_path, monkeypatch, [])
    manifest.write_text(json.dumps({**candidate(), 'kind': 'icon'}) + '\n')
    with pytest.raises(ValueError, match='screen or block'):
        public_sources.capture_sources(manifest, root, limit=1)
    assert calls == []


def test_resource_failure_stops_capture_and_preserves_pending_for_explicit_resume(tmp_path, monkeypatch):
    manifest, root, calls = setup_capture(tmp_path, monkeypatch,
        [{'status': 'resource_error', 'error': 'source disk reserve'}, captured()])
    with pytest.raises(RuntimeError, match='source disk reserve'):
        public_sources.capture_sources(manifest, root, limit=1)
    resumed = public_sources.capture_sources(manifest, root, limit=1)
    assert resumed['captured'] == 1
    assert resumed['rejected'] == 0
    assert len(calls) == 2


def test_ingestion_checks_reserve_before_writing_an_entire_large_queue(tmp_path, monkeypatch):
    manifest, root, calls = setup_capture(tmp_path, monkeypatch, [])
    manifest.write_text(''.join(json.dumps(candidate(f'https://demo.example.invalid/{i}'))+'\n'
                                for i in range(201)))
    checks = 0

    def guard(path):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError('resource_error: disk reserve reached')

    monkeypatch.setattr(public_sources, 'guard', guard)
    with pytest.raises(RuntimeError, match='disk reserve'):
        public_sources.capture_sources(manifest, root, limit=1)
    store = Store(root)
    assert 0 < store.db.execute('SELECT count(*) FROM captures').fetchone()[0] <= 100
    assert calls == []


def test_conflicting_screenshots_for_one_scene_are_not_counted_twice(tmp_path, monkeypatch):
    first, second = captured(), captured()
    image = io.BytesIO()
    Image.new('RGB', (16, 16), 'black').save(image, format='PNG')
    second['reference'] = base64.b64encode(image.getvalue()).decode()
    second['source_sha256'] = second['restored_sha256'] = hashlib.sha256(image.getvalue()).hexdigest()
    manifest, root, _ = setup_capture(tmp_path, monkeypatch, [first, second])
    manifest.write_text('\n'.join(json.dumps(candidate(url)) for url in
                        ['https://demo.example.invalid/hero', 'https://demo.example.invalid/copy']) + '\n')
    result = public_sources.capture_sources(manifest, root, limit=2)
    assert result['captured'] == 1
    assert result['rejected'] == 1
    store = Store(root)
    error = json.loads(store.db.execute("SELECT record FROM captures WHERE status='rejected'").fetchone()[0])
    assert error['error'] == 'source scene identity conflict'


def test_resume_replaces_annotation_alias_from_uncommitted_capture(tmp_path, monkeypatch):
    first, second = captured(), captured()
    second['elements'] = [{'element_id': 'el-1', 'role': 'button'}]
    manifest, root, _ = setup_capture(tmp_path, monkeypatch, [first, second])
    original_put = Store.put

    def fail_before_checkpoint(self, content, suffix='png'):
        if suffix == 'png':
            raise RuntimeError('interrupted before checkpoint')
        return original_put(self, content, suffix)

    monkeypatch.setattr(Store, 'put', fail_before_checkpoint)
    with pytest.raises(RuntimeError, match='interrupted before checkpoint'):
        public_sources.capture_sources(manifest, root, limit=1)
    alias = root / 'annotations' / 'elements' / f"{second['scene_id']}.json"
    assert json.loads(alias.read_text())['elements'][0]['role'] == 'heading'
    monkeypatch.setattr(Store, 'put', original_put)
    assert public_sources.capture_sources(manifest, root, limit=1)['captured'] == 1
    record = json.loads((root / 'manifest.jsonl').read_text())
    assert json.loads(alias.read_text())['elements'][0]['role'] == 'button'
    assert hashlib.sha256(alias.read_bytes()).hexdigest() == record['sidecars']['elements']
