import json
import re

import pytest

from lumen_data.review import build_review
from lumen_data.storage import Store


def add(store, index, **changes):
    record = {'case_id': f'{index:064x}', 'operator': 'clip', 'source_project_id': 'common',
              'structural_family_id': 'table', 'has_single_frame_defect': True,
              'capture': {'image_px': [1000, 1000], 'locale': 'en'},
              'issues': [{'symptom': 'text_clip_y', 'bbox_xyxy_px': [0, 0, 200, 200]}],
              'assets': dict.fromkeys(('reference', 'current', 'raw_change_mask'), 'a' * 64),
              'verification': 'auto_verified'}
    record.update(changes)
    with store.db:
        store.db.execute('INSERT INTO cases VALUES (?,?,?)',
                         (record['case_id'], f'{index:064x}', json.dumps(record)))


def records(path):
    html = path.read_text()
    return json.loads(re.search(r'const records=(.*?);let index=', html).group(1))


def test_review_covers_rare_sources_layouts_negatives_and_small_defects(tmp_path):
    store = Store(tmp_path)
    for index in range(600):
        add(store, index)
    add(store, 1000, operator='rare-op')
    add(store, 1001, source_project_id='rare-source')
    add(store, 1002, structural_family_id='nested-form')
    add(store, 1003, has_single_frame_defect=False, issues=[])
    add(store, 1004, capture={'image_px': [320, 720], 'locale': 'ar'})
    add(store, 1005, issues=[{'symptom': 'clip', 'bbox_xyxy_px': [0, 0, 10, 10]}])
    report = build_review(tmp_path, limit=500)
    selected = records(tmp_path / 'review.html')
    assert len(selected) == len({r['case_id'] for r in selected}) == 500
    assert {f'{i:064x}' for i in range(1000, 1006)} <= {r['case_id'] for r in selected}
    assert report['human_verified'] == 0
    assert all(row['uncovered'] == 0 for row in report['coverage'].values())
    first = (tmp_path / 'review.html').read_bytes()
    build_review(tmp_path, limit=500)
    assert (tmp_path / 'review.html').read_bytes() == first


def test_review_reports_uncovered_families_instead_of_claiming_complete_coverage(tmp_path):
    store = Store(tmp_path)
    for index in range(510):
        add(store, index, structural_family_id=f'family-{index}')
    report = build_review(tmp_path, limit=500)
    assert report['coverage']['family'] == {'available': 510, 'selected': 500, 'uncovered': 10}


@pytest.mark.parametrize('limit', [0, 499, 1001, True, 500.5])
def test_review_rejects_invalid_limits(tmp_path, limit):
    with pytest.raises(ValueError, match='500.*1000'):
        build_review(tmp_path, limit=limit)
    assert not (tmp_path / 'review.html').exists()


def test_review_escapes_embedded_content_and_keeps_owner_decision_contract(tmp_path):
    store = Store(tmp_path)
    add(store, 1, operator='</script><script>alert(1)</script>')
    report = build_review(tmp_path)
    html = (tmp_path / 'review.html').read_text()
    assert report['cases'] == 1
    assert '</script><script>alert(1)</script>' not in html
    assert records(tmp_path / 'review.html')[0]['operator'].startswith('</script>')
    assert 'lumen.review.v1' in html and 'review-decisions.jsonl' in html
    assert 'needs_review' in html and 'reviewed_at' in html
    assert 'id="defect"' in html
    assert 'id="jump"' in html
    assert 'image failed to load' in html


def test_review_covers_each_scenario_tag_on_positive_and_negative_cases(tmp_path):
    store = Store(tmp_path)
    for index in range(600):
        add(store, index)
    add(store, 1000, scenario_tags=['scrollable_table', 'sticky_header'])
    add(store, 1001, scenario_tags=['overlapping_avatars'], has_single_frame_defect=False, issues=[])
    report = build_review(tmp_path, limit=500)
    selected = records(tmp_path / 'review.html')
    assert {f'{i:064x}' for i in (1000, 1001)} <= {r['case_id'] for r in selected}
    assert report['coverage']['scenario'] == {'available': 3, 'selected': 3, 'uncovered': 0}


def test_review_excludes_unused_recipe_and_source_payload(tmp_path):
    store = Store(tmp_path)
    add(store, 1, recipe={'html': 'unused-private-source-' * 10000})
    build_review(tmp_path)
    html = (tmp_path / 'review.html').read_text()
    assert 'unused-private-source-' not in html
    assert 'recipe' not in records(tmp_path / 'review.html')[0]
    assert len(html.encode()) < 20000


def test_review_rejects_oversized_required_evidence_before_writing_html(tmp_path, monkeypatch):
    import lumen_data.review as review
    monkeypatch.setattr(review, 'MAX_REVIEW_BYTES', 2048, raising=False)
    store = Store(tmp_path)
    add(store, 1, issues=[{'symptom': 'clip', 'bbox_xyxy_px': [0, 0, 10, 10],
                         'evidence': {'details': 'x' * 4096}}])
    with pytest.raises(ValueError, match='review payload budget'):
        build_review(tmp_path)
    assert not (tmp_path / 'review.html').exists()


def test_review_rejects_aggregate_payload_over_budget(tmp_path, monkeypatch):
    import lumen_data.review as review
    monkeypatch.setattr(review, 'MAX_REVIEW_BYTES', 2048, raising=False)
    store = Store(tmp_path)
    for index in range(10):
        add(store, index)
    with pytest.raises(ValueError, match='review payload budget'):
        build_review(tmp_path)
    assert not (tmp_path / 'review.html').exists()
