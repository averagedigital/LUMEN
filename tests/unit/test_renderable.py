import pytest
import json
from lumen_data import renderable

from lumen_data.renderable import required_assets


def test_static_html_identifies_required_assets_and_ignores_navigation_links():
    html = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css"><a href="https://example.com">Open</a>'
    assert required_assets(html) == {'https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css'}


@pytest.mark.parametrize('html', ['<script>alert(1)</script>', '<iframe src="https://example.com"></iframe>',
    '<img src="https://source.unsplash.com/random">', '<style>body{background:url(https://example.com/a.png)}</style>'])
def test_unresolved_or_active_sources_are_explicitly_rejected(html):
    with pytest.raises(ValueError):
        required_assets(html)


def manifest(tmp_path):
    path = tmp_path / 'input.jsonl'
    path.write_text(''.join(json.dumps({'source': 'websight', 'html': html,
        'provenance': {'row': i}}) + '\n' for i, html in enumerate([
            '<ul><li>One</li></ul>', '<ul><li>Two</li><li>Three</li></ul>'])))
    return path


def test_unknown_template_lineage_stays_in_one_source_group(tmp_path, monkeypatch):
    monkeypatch.setattr(renderable, 'guard', lambda path: None)
    monkeypatch.setattr(renderable, 'download', lambda url, path, **kw: path.write_text('body{}'))
    output = tmp_path / 'ready.jsonl'
    renderable.prepare_renderable(manifest(tmp_path), output, tmp_path / 'assets')
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]['base_scene_id'] != rows[1]['base_scene_id']
    assert rows[0]['template_lineage_id'] == rows[1]['template_lineage_id']


def test_preparation_retains_stylesheet_license(tmp_path, monkeypatch):
    monkeypatch.setattr(renderable, 'guard', lambda path: None)
    def download(url, path, **kw):
        path.write_text('MIT License\nCopyright example\nPermission notice' if url.endswith('/LICENSE') else 'body{}')
    monkeypatch.setattr(renderable, 'download', download)
    path = tmp_path / 'input.jsonl'
    path.write_text(json.dumps({'source': 'websight', 'provenance': {},
        'html': f'<link rel="stylesheet" href="{renderable.TAILWIND}"><h1>Title</h1>'}) + '\n')
    output = tmp_path / 'ready.jsonl'
    renderable.prepare_renderable(path, output, tmp_path / 'assets')
    asset = json.loads(output.read_text())['assets'][renderable.TAILWIND]
    assert (tmp_path / 'assets' / asset['license_sha256']).read_text().startswith('MIT License')


def test_renderable_rechecks_output_disk_and_preserves_previous_manifest(tmp_path, monkeypatch):
    output = tmp_path / 'ready.jsonl'
    output.write_text('previous')
    checks = 0

    def guard(path):
        nonlocal checks
        if path == output.parent:
            checks += 1
            if checks == 2:
                raise RuntimeError('disk reserve reached')

    monkeypatch.setattr(renderable, 'guard', guard)
    monkeypatch.setattr(renderable, 'download', lambda url, path, **kw: path.write_text('body{}'))
    with pytest.raises(RuntimeError, match='disk reserve'):
        renderable.prepare_renderable(manifest(tmp_path), output, tmp_path / 'assets')
    assert output.read_text() == 'previous'
