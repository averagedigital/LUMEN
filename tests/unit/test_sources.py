import pytest
import json
import shutil

from lumen_data.sources import validate_url, decode_row


@pytest.mark.parametrize('url', ['http://huggingface.co/data', 'https://127.0.0.1/a',
                               'https://huggingface.co.evil.invalid/a',
                               'file:///etc/passwd', 'https://u:p@huggingface.co/a'])
def test_fetcher_rejects_unapproved_destinations(url):
    with pytest.raises(ValueError):
        validate_url(url)


def test_source_geometry_uses_explicit_schema():
    row = {'id': 'one', 'width': 100, 'height': 80, 'bboxes': [[10, 20, 30, 40]],
           'labels': ['Button'], 'texts': ['Save'], 'image': {'bytes': b'image'}}
    converted = decode_row('screenparse', row)
    assert converted['elements'][0]['bbox_xyxy_px'] == [10, 20, 40, 60]
    assert converted['annotation_completeness'] == 'elements_only'
    assert converted['has_single_frame_defect'] is None


def test_html_sources_are_not_automatically_clean():
    row = decode_row('websight', {'text': '<h1>Example</h1>'})
    assert row['html'] == '<h1>Example</h1>'
    assert row['verification'] == 'unverified'


def source_audit(tmp_path, decision='research_only'):
    path = tmp_path / 'audit.json'
    path.write_text(json.dumps([{'id': 'websight', 'role': 'browser_mutation_source',
        'status': 'available', 'revision': 'a' * 40, 'decision': decision,
        'repository': 'HuggingFaceM4/WebSight', 'license': 'cc-by-4.0',
        'public_release_status': 'research_only', 'config': 'v0.2',
        'files': ['v0.2/train.parquet']}]))
    return path


def test_pending_schema_audit_cannot_prepare(tmp_path):
    from lumen_data.sources import prepare
    with pytest.raises(ValueError, match='audit'):
        prepare(source_audit(tmp_path, 'pending'), 'websight', tmp_path / 'output')


def test_source_import_resumes_after_download_failure(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from lumen_data import sources

    parquet = tmp_path / 'fixture.parquet'
    pq.write_table(pa.Table.from_pylist([{'text': '<h1>Example</h1>'}]), parquet)
    calls = 0

    def download(url, destination, max_bytes):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('transport interrupted')
        shutil.copyfile(parquet, destination)

    monkeypatch.setattr(sources, 'download', download)
    monkeypatch.setattr(sources, 'guard', lambda path: None)
    audit = source_audit(tmp_path)
    output = tmp_path / 'output'
    with pytest.raises(RuntimeError):
        sources.prepare(audit, 'websight', output)
    result = sources.prepare(audit, 'websight', output)
    assert result['imported'] == 1
    again = sources.prepare(audit, 'websight', output)
    assert again['imported'] == 1
    assert len((output / 'manifest.jsonl').read_text().splitlines()) == 1


def test_manifest_projection_stops_at_disk_reserve_and_preserves_previous_manifest(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from lumen_data import sources

    parquet = tmp_path / 'fixture.parquet'
    pq.write_table(pa.Table.from_pylist([{'text': f'<h1>{i}</h1>'} for i in range(150)]), parquet)
    monkeypatch.setattr(sources, 'download', lambda url, destination, max_bytes: shutil.copyfile(parquet, destination))
    monkeypatch.setattr(sources, 'guard', lambda path: None)
    audit, output = source_audit(tmp_path), tmp_path / 'output'
    sources.prepare(audit, 'websight', output, max_rows=150)
    previous = (output / 'manifest.jsonl').read_bytes()
    checks = 0

    def exhausted_during_projection(path):
        nonlocal checks
        checks += 1
        if checks == 2:
            assert (output / 'manifest.part').stat().st_size > 0
            raise RuntimeError('disk reserve reached')

    monkeypatch.setattr(sources, 'guard', exhausted_during_projection)
    with pytest.raises(RuntimeError, match='disk reserve'):
        sources.prepare(audit, 'websight', output, max_rows=150)
    assert (output / 'manifest.jsonl').read_bytes() == previous
    assert not (output / 'manifest.part').exists()
    monkeypatch.setattr(sources, 'guard', lambda path: None)
    assert sources.prepare(audit, 'websight', output, max_rows=150)['imported'] == 150
