import math

import pytest

from lumen_data.contracts import Box, case_id, group_split, validate_case


def test_coordinate_conversion_uses_half_open_image_pixels():
    box = Box.from_xywh([10, 20, 30, 40])
    assert box.xyxy == (10, 20, 40, 60)
    assert box.yolo(100, 100) == (0.25, 0.4, 0.3, 0.4)
    assert box.transform(dpr=2, scroll=(5, 10), crop=(2, 4)).xyxy == (8, 16, 68, 96)


@pytest.mark.parametrize('coords', [[0, 0, 0, 1], [2, 0, 1, 1], [0, 0, math.nan, 1]])
def test_rejects_invalid_boxes(coords):
    with pytest.raises(ValueError):
        Box(*coords)


def test_rejects_boxes_outside_image_and_unknown_format():
    with pytest.raises(ValueError):
        Box(-1, 0, 10, 10).yolo(20, 20)
    with pytest.raises(ValueError):
        Box.parse([1, 2, 3, 4], 'guess')


def test_split_is_by_group_not_case():
    assert group_split('project/template') == group_split('project/template')
    assert {group_split(str(i)) for i in range(1000)} == {'train', 'val', 'test'}
    assert case_id({'seed': 3, 'scene': 'x'}) == case_id({'scene': 'x', 'seed': 3})
    assert case_id({'seed': 3}) != case_id({'seed': 4})


def test_strict_case_rejects_missing_or_incomplete_evidence():
    with pytest.raises(ValueError):
        validate_case({})


def valid_case():
    return {
        'schema_version': 'lumen.case.v1', 'case_id': 'a' * 64,
        'base_scene_id': 'scene', 'template_lineage_id': 'template',
        'source_project_id': 'source', 'structural_family_id': 'form',
        'split_group': 'source/template', 'split': group_split('source/template'),
        'capture': {'image_px': [200, 100]},
        'assets': {name: 'b' * 64 for name in ('reference', 'current', 'raw_change_mask')},
        'has_change': True, 'has_single_frame_defect': True,
        'annotation_completeness': 'complete_within_declared_scope',
        'verification': 'auto_verified',
        'provenance': {'license': 'MIT', 'revision': 'v1', 'public_release_status': 'approved'},
        'issues': [{'observability': 'single_frame', 'family': 'text_geometry',
                    'bbox_xyxy_px': [10, 10, 50, 30], 'context_bbox_xyxy_px': [0, 0, 70, 50],
                    'evidence': {'oracle_id': 'clip-v1', 'baseline_passed': True,
                                 'after_failed': True}}],
    }


def test_valid_case_and_public_permission():
    record = valid_case()
    validate_case(record, public_safe=True)
    record['provenance']['public_release_status'] = 'research_only'
    with pytest.raises(ValueError, match='public'):
        validate_case(record, public_safe=True)


@pytest.mark.parametrize('field,value', [('baseline_passed', 'false'), ('after_failed', 1),
                                       ('oracle_id', '')])
def test_semantic_evidence_requires_typed_facts(field, value):
    record = valid_case()
    record['issues'][0]['evidence'][field] = value
    with pytest.raises(ValueError, match='semantic'):
        validate_case(record)


def test_context_crop_contains_issue():
    record = valid_case()
    record['issues'][0]['context_bbox_xyxy_px'] = [100, 50, 200, 100]
    with pytest.raises(ValueError, match='context'):
        validate_case(record)


@pytest.mark.parametrize('identity', ['../outside', '/absolute', '', 'g' * 64])
def test_case_identity_is_safe_for_export_paths(identity):
    record = valid_case()
    record['case_id'] = identity
    with pytest.raises(ValueError, match='case_id'):
        validate_case(record)
