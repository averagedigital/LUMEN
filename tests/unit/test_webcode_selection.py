import pytest
from lumen_data.webcode_selection import select_row


def test_selection_retains_structure_and_provenance_without_modern_claim():
    row = {'text': '<style>main{display:grid}</style><main><nav><a>Home</a></nav><form><input><button>Save</button></form></main>', 'score': 3}
    result = select_row(row, revision='a'*40, shard='data/00002.parquet', row_id=9)
    assert result['html'] == row['text']
    assert result['render_mode'] == 'static'
    assert result['corpus'] == 'unclassified'
    assert result['provenance']['row'] == 9
    assert result['structural_family_id'] == 'form+navigation'


def test_content_changes_keep_structure_group():
    a = {'text': '<main><p>Hello</p></main>', 'score': 2}
    b = {'text': '<main><p>Another text</p></main>', 'score': 2}
    assert select_row(a, revision='a'*40, shard='a', row_id=0)['split_group'] == select_row(b, revision='a'*40, shard='b', row_id=0)['split_group']


@pytest.mark.parametrize('row', [{'text': '<script>run()</script>', 'score': 3},
                                {'text': '<img src="https://external.invalid/a.png">', 'score': 3},
                                {'text': None, 'score': 3}, {'text': '<p>Hi</p>', 'score': 0}])
def test_unreplayable_or_low_score_rows_are_explicitly_rejected(row):
    with pytest.raises(ValueError):
        select_row(row, revision='a'*40, shard='a', row_id=0)


def selection_fixture(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import fsspec
    import lumen_data.webcode_selection as module
    revision = 'a' * 40
    monkeypatch.setattr(module, 'guard', lambda path: None)
    monkeypatch.setattr(module, 'fetch_json', lambda url: {'sha': revision, 'siblings': [
        {'rfilename': 'data/00001.parquet'}, {'rfilename': 'data/00002.parquet'}]})
    monkeypatch.setattr(module, 'download', lambda url, path, **kw: path.write_text('p{color:black}'))
    for i in [1, 2]:
        pq.write_table(pa.Table.from_pylist([{'text': f'<p>Page {i}</p>', 'score': 3}]),
                       tmp_path / f'0000{i}.parquet')
    class Files:
        def open(self, url, mode):
            return (tmp_path / url.rsplit('/', 1)[1]).open(mode)
    monkeypatch.setattr(fsspec, 'filesystem', lambda *args, **kwargs: Files())
    return module, revision


def test_resume_rebuilds_exports_after_committed_shard(tmp_path, monkeypatch):
    from pathlib import Path
    import json
    module, revision = selection_fixture(tmp_path, monkeypatch)
    original = Path.replace
    def interrupted(path, target):
        if path.name == 'manifest.part':
            raise OSError('interrupted export')
        return original(path, target)
    monkeypatch.setattr(Path, 'replace', interrupted)
    with pytest.raises(OSError, match='interrupted export'):
        module.select_webcode(tmp_path / 'output', revision, max_shards=1)
    monkeypatch.setattr(Path, 'replace', original)
    result = module.select_webcode(tmp_path / 'output', revision, max_shards=1)
    assert result['shards_completed'] == 1 and result['selected'] == 1
    assert json.loads((tmp_path / 'output/manifest.jsonl').read_text())['html'] == '<p>Page 1</p>'


def test_resume_reuses_pinned_assets_and_keeps_bounded_selection(tmp_path, monkeypatch):
    module, revision = selection_fixture(tmp_path, monkeypatch)
    module.select_webcode(tmp_path / 'output', revision, max_shards=1, per_stratum=1)
    def unexpected_download(*args, **kwargs):
        raise AssertionError('resume must use pinned assets')
    monkeypatch.setattr(module, 'download', unexpected_download)
    report = module.select_webcode(tmp_path / 'output', revision, max_shards=2, per_stratum=1)
    assert report['shards_completed'] == 2 and report['selected'] == 1
    assert report['counts']['rows'] == 2


def test_quality_threshold_filters_rows_and_is_pinned_on_resume(tmp_path, monkeypatch):
    import json
    import pyarrow as pa
    import pyarrow.parquet as pq
    module, revision = selection_fixture(tmp_path, monkeypatch)
    pq.write_table(pa.Table.from_pylist([{'text': '<main>High quality</main>', 'score': 5}]),
                   tmp_path/'00002.parquet')
    report = module.select_webcode(tmp_path/'output', revision, min_score=4)
    assert report['selected'] == 1
    assert report['counts']['rows'] == 2
    assert report['counts']['source_quality_score'] == 1
    assert report['counts']['metadata_pruned_rows'] == 1
    row = json.loads((tmp_path/'output/manifest.jsonl').read_text())
    assert row['provenance']['source_quality_score'] == 5
    assert module.select_webcode(tmp_path/'output', revision, min_score=4)['selected'] == 1
    with pytest.raises(ValueError, match='contract changed'):
        module.select_webcode(tmp_path/'output', revision, min_score=5)


@pytest.mark.parametrize('score,accepted', [(3, False), (4, True), (5, True)])
def test_row_quality_boundary(score, accepted):
    row = {'text': '<main>Application</main>', 'score': score}
    if accepted:
        assert select_row(row, revision='a'*40, shard='a', row_id=0,
                          min_score=4)['provenance']['source_quality_score'] == score
    else:
        with pytest.raises(ValueError, match='source_quality_score'):
            select_row(row, revision='a'*40, shard='a', row_id=0, min_score=4)


@pytest.mark.parametrize('statistics', [True, False])
def test_threshold_keeps_boundary_row_in_mixed_or_unindexed_shard(tmp_path, monkeypatch, statistics):
    import json
    import pyarrow as pa
    import pyarrow.parquet as pq
    module, revision = selection_fixture(tmp_path, monkeypatch)
    pq.write_table(pa.Table.from_pylist([
        {'text': '<p>Below threshold</p>', 'score': 3},
        {'text': '<p>At threshold</p>', 'score': 4}]),
        tmp_path/'00001.parquet', row_group_size=1, write_statistics=statistics)
    report = module.select_webcode(tmp_path/'output', revision, min_score=4, max_shards=1)
    assert report['selected'] == 1 and report['counts']['rows'] == 2
    assert report['counts']['source_quality_score'] == 1
    assert not report['counts'].get('metadata_pruned_rows')
    assert json.loads((tmp_path/'output/manifest.jsonl').read_text())['html'] == '<p>At threshold</p>'


@pytest.mark.parametrize('threshold', [True, 1, 6, 4.0])
def test_selection_rejects_invalid_threshold(tmp_path, threshold):
    from lumen_data.webcode_selection import select_webcode
    with pytest.raises(ValueError, match='selection budget'):
        select_webcode(tmp_path/'output', 'a'*40, min_score=threshold)
