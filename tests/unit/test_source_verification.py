import io
import pytest
from PIL import Image
from lumen_data.source_verification import reference_fidelity


def png(color, size=(128, 72)):
    output = io.BytesIO()
    Image.new('RGB', size, color).save(output, format='PNG')
    return output.getvalue()


def test_matching_source_is_admitted_and_missing_style_is_rejected():
    assert reference_fidelity(png('white'), png('white'))['passed']
    assert not reference_fidelity(png('white'), png('black'))['passed']


def test_dimensions_and_large_local_changes_are_not_hidden_by_average():
    assert not reference_fidelity(png('white'), png('white', (72, 128)))['passed']
    before = png('white')
    image = Image.open(io.BytesIO(before))
    image.paste((0, 0, 0), (0, 0, 64, 72))
    output = io.BytesIO()
    image.save(output, format='PNG')
    assert not reference_fidelity(before, output.getvalue())['passed']


def test_verification_exports_only_replayed_faithful_sources_and_resumes(tmp_path, monkeypatch):
    import base64
    import hashlib
    import json
    from lumen_data.source_verification import verify_sources
    import lumen_data.source_verification as module
    monkeypatch.setattr(module, 'guard', lambda path: None)
    monkeypatch.setattr(module, 'revision', lambda: 'fixed-generator')
    original = png('white')
    digest = hashlib.sha256(original).hexdigest()
    (tmp_path / f'{digest}.png').write_bytes(original)
    rows = [{'base_scene_id': name, 'html': name, 'assets': {}, 'source_viewport': [128, 72],
             'source_reference_sha256': digest} for name in ['faithful', 'unstyled']]
    manifest = tmp_path / 'sources.jsonl'
    manifest.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    class Worker:
        def __init__(self, *args):
            pass
        def request(self, payload):
            content = original if payload['html'] == 'faithful' else png('black')
            return {'status': 'accepted_clean', 'reference': base64.b64encode(content).decode()}
        def close(self):
            pass
    monkeypatch.setattr(module, 'Worker', Worker)
    report = verify_sources(manifest, tmp_path, tmp_path, tmp_path / 'output')
    assert report['counts'] == {'verified': 1, 'reference_mismatch': 1}
    selected = json.loads((tmp_path / 'output/manifest.jsonl').read_text())
    assert selected['base_scene_id'] == 'faithful'
    assert selected['source_verification']['replay_equal']
    monkeypatch.setattr(module, 'Worker', lambda *args: pytest.fail('completed scene was rerendered'))
    assert verify_sources(manifest, tmp_path, tmp_path, tmp_path / 'output') == report


def test_worker_resource_failure_is_not_cached_as_source_rejection(tmp_path, monkeypatch):
    import base64
    import hashlib
    import json
    import lumen_data.source_verification as module
    monkeypatch.setattr(module, 'guard', lambda path: None)
    monkeypatch.setattr(module, 'revision', lambda: 'fixed-generator')
    original = png('white')
    digest = hashlib.sha256(original).hexdigest()
    (tmp_path / f'{digest}.png').write_bytes(original)
    manifest = tmp_path / 'sources.jsonl'
    manifest.write_text(json.dumps({'base_scene_id': 'one', 'html': 'source', 'assets': {},
        'source_viewport': [128, 72], 'source_reference_sha256': digest})+'\n')
    class Worker:
        failed = True
        def __init__(self, *args):
            pass
        def request(self, payload):
            if self.failed:
                return {'status': 'resource_error', 'error': 'renderer unavailable'}
            return {'status': 'accepted_clean', 'reference': base64.b64encode(original).decode()}
        def close(self):
            pass
    monkeypatch.setattr(module, 'Worker', Worker)
    with pytest.raises(RuntimeError, match='resource_error'):
        module.verify_sources(manifest, tmp_path, tmp_path, tmp_path/'out')
    Worker.failed = False
    assert module.verify_sources(manifest, tmp_path, tmp_path, tmp_path/'out')['counts'] == {'verified': 1}


@pytest.mark.parametrize('timeout_on', [1, 2])
def test_render_budget_rejection_is_explicit_on_either_capture(tmp_path, monkeypatch, timeout_on):
    import base64
    import json
    import sqlite3
    import lumen_data.source_verification as module
    monkeypatch.setattr(module, 'guard', lambda path: None)
    monkeypatch.setattr(module, 'revision', lambda: 'fixed-generator')
    manifest = tmp_path / 'sources.jsonl'
    manifest.write_text(json.dumps({'base_scene_id': 'one', 'html': 'source', 'assets': {},
        'source_viewport': [128, 72]})+'\n')
    evidence = {'reason': 'capture_deadline', 'limit_ms': 45000}
    class Worker:
        def __init__(self, *args):
            self.calls = 0
        def request(self, payload):
            self.calls += 1
            if self.calls == timeout_on:
                return {'status': 'render_timeout', 'evidence': evidence}
            return {'status': 'accepted_clean', 'reference': base64.b64encode(png('white')).decode()}
        def close(self):
            pass
    monkeypatch.setattr(module, 'Worker', Worker)
    report = module.verify_sources(manifest, tmp_path, tmp_path, tmp_path/'out')
    assert report['counts'] == {'render_timeout': 1}
    assert (tmp_path/'out/manifest.jsonl').read_text() == ''
    with sqlite3.connect(tmp_path/'out/verification.sqlite') as db:
        assert json.loads(db.execute('SELECT evidence FROM results').fetchone()[0]) == evidence
