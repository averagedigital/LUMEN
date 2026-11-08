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


def test_repeated_template_keeps_group_without_inventing_lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(renderable, 'guard', lambda path: None)
    monkeypatch.setattr(renderable, 'download', lambda url, path, **kw: path.write_text('body{}'))
    output = tmp_path / 'ready.jsonl'
    renderable.prepare_renderable(manifest(tmp_path), output, tmp_path / 'assets')
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]['base_scene_id'] != rows[1]['base_scene_id']
    assert rows[0]['template_lineage_id'] is rows[1]['template_lineage_id'] is None
    assert rows[0]['split_group'] == rows[1]['split_group']
    assert rows[0]['split_group'].startswith('dom-v1:')


def prepared(tmp_path, monkeypatch, records):
    monkeypatch.setattr(renderable, 'guard', lambda path: None)
    monkeypatch.setattr(renderable, 'download', lambda url, path, **kw: path.write_text('body{}'))
    path, output = tmp_path / 'input.jsonl', tmp_path / 'output.jsonl'
    path.write_text(''.join(json.dumps({'source': 'websight', 'provenance': {}, **row}) + '\n'
                            for row in records))
    report = renderable.prepare_renderable(path, output, tmp_path / 'assets')
    return [json.loads(line) for line in output.read_text().splitlines()], report


def test_groups_preserve_nesting_and_ignore_content_theme_and_source(tmp_path, monkeypatch):
    rows, _ = prepared(tmp_path, monkeypatch, [
        {'html': '<main><div><span>A</span></div><span>B</span></main>'},
        {'html': '<main><div><span>A</span><span>B</span></div></main>'},
        {'html': '<main id="random" class="bg-blue"><div><span>Long translation</span></div><span>Z</span></main>',
         'source': 'another-source'},
    ])
    assert rows[0]['split_group'] != rows[1]['split_group']
    assert rows[0]['split_group'] == rows[2]['split_group']
    assert rows[0]['base_scene_id'] != rows[2]['base_scene_id']
    assert rows[2]['source_project_id'] == 'another-source'
    assert rows[0]['structural_family_id'] == 'content'


def test_group_identity_is_order_independent_and_fields_keep_their_roles(tmp_path, monkeypatch):
    records = [{'html': '<nav><a>Go</a></nav><form><input type="email"></form>',
                'template_lineage_id': 'confirmed-template'},
               {'html': '<table><tr><td>Value</td></tr></table>'}]
    first, _ = prepared(tmp_path, monkeypatch, records)
    second, _ = prepared(tmp_path, monkeypatch, records[::-1])
    assert first == second[::-1]
    assert first[0]['template_lineage_id'] == 'confirmed-template'
    assert first[0]['structural_family_id'] == 'form+navigation'
    assert first[1]['structural_family_id'] == 'table'
    assert first[0]['split_group'] != first[1]['split_group']


@pytest.mark.parametrize('html', ['<div>' * 129 + '</div>' * 129, '<br>' * 10001],
                         ids=['depth', 'nodes'])
def test_unbounded_structure_is_explicitly_rejected(html):
    with pytest.raises(ValueError, match='structure'):
        renderable.structural_identity(html)


def test_invalid_structure_does_not_abort_other_source_rows(tmp_path, monkeypatch):
    rows, report = prepared(tmp_path, monkeypatch, [{'html': '<br>' * 10001},
                                                   {'html': '<main><p>Valid</p></main>'}])
    assert len(rows) == 1 and report['renderable_candidates'] == 1
    assert report['rejections'] == {'structure_limit': 1}


@pytest.mark.parametrize('implicit,explicit', [
    ('<table><tr><td>A</td></tr></table>', '<table><tbody><tr><td>A</td></tr></tbody></table>'),
    ('<ul><li>A<li>B</ul>', '<ul><li>A</li><li>B</li></ul>'),
    ('<div>', '<div></div>'),
])
def test_html5_equivalent_markup_has_one_group(implicit, explicit):
    assert renderable.structural_identity(implicit) == renderable.structural_identity(explicit)


def test_structural_fingerprint_does_not_validate_or_fetch_external_assets():
    first = '<img src="https://first.invalid/a.png"><link rel="stylesheet" href="https://first.invalid/a.css">'
    second = '<img src="https://second.invalid/b.png"><link rel="stylesheet" href="https://second.invalid/b.css">'
    assert renderable.structural_identity(first) == renderable.structural_identity(second)
    with pytest.raises(ValueError, match='unresolved'):
        required_assets(first)


def test_confirmed_lineage_cannot_be_split_by_different_structure(tmp_path, monkeypatch):
    output = tmp_path / 'output.jsonl'
    output.write_text('previous')
    with pytest.raises(ValueError, match='lineage group conflict'):
        prepared(tmp_path, monkeypatch, [
            {'html': '<form><input></form>', 'template_lineage_id': 'same-template'},
            {'html': '<table><tr><td>A</td></tr></table>', 'template_lineage_id': 'same-template'},
        ])
    assert output.read_text() == 'previous'


def test_independent_structures_reach_all_splits_and_variants_stay_together():
    from lumen_data.contracts import group_split
    groups = set()
    for value in range(256):
        tags = ['section' if value & (1 << bit) else 'div' for bit in range(8)]
        html = ''.join(f'<{tag}>' for tag in tags) + '<button>Save</button>'
        html += ''.join(f'</{tag}>' for tag in reversed(tags))
        identity = renderable.structural_identity(html)
        recolored = html.replace('<button>', '<button id="generated" style="color:blue">')
        assert renderable.structural_identity(recolored.replace('Save', 'Сохранить')) == identity
        groups.add(identity['split_group'])
    assert len(groups) == 256
    assert {group_split(group) for group in groups} == {'train', 'val', 'test'}


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
