from dataclasses import dataclass
import hashlib
import json
import math
import re

GRAMMAR_V2 = ('dashboard', 'crm', 'admin', 'shop', 'product', 'checkout', 'settings', 'chat',
              'inbox', 'calendar', 'kanban', 'docs', 'article', 'pricing', 'files', 'editor',
              'spreadsheet', 'wizard', 'search', 'media')


def asset_suffix(content_type):
    suffix = {'text/css': 'css', 'font/woff2': 'woff2', 'image/jpeg': 'jpg',
              'image/png': 'png', 'image/svg+xml': 'svg', 'image/webp': 'webp'}.get(content_type)
    if suffix is None:
        raise ValueError('unsupported source asset')
    return suffix


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def case_id(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def group_split(group):
    if group.startswith('lumen-grammar-v2/'):
        family = group.removeprefix('lumen-grammar-v2/')
        if family not in GRAMMAR_V2:
            raise ValueError('unknown grammar group')
        # Frozen SHA-ranked 18/1/1 assignment for this finite catalog.
        return {'chat': 'val', 'calendar': 'test'}.get(family, 'train')
    bucket = int(case_id({'group': group})[:8], 16) % 100
    return 'train' if bucket < 90 else 'val' if bucket < 95 else 'test'


@dataclass(frozen=True)
class Box:
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self):
        if not all(math.isfinite(v) for v in self.xyxy) or self.x2 <= self.x1 or self.y2 <= self.y1:
            raise ValueError('invalid xyxy box')

    @property
    def xyxy(self):
        return self.x1, self.y1, self.x2, self.y2

    @classmethod
    def from_xywh(cls, values):
        x, y, w, h = values
        return cls(x, y, x + w, y + h)

    @classmethod
    def parse(cls, values, fmt):
        if fmt == 'xyxy':
            return cls(*values)
        if fmt == 'xywh':
            return cls.from_xywh(values)
        raise ValueError('unsupported bbox format')

    def transform(self, *, dpr=1, scroll=(0, 0), crop=(0, 0)):
        if not math.isfinite(dpr) or dpr <= 0:
            raise ValueError('invalid DPR')
        return Box(*((v - scroll[i % 2]) * dpr - crop[i % 2]
                     for i, v in enumerate(self.xyxy)))

    def within(self, width, height):
        return 0 <= self.x1 < self.x2 <= width and 0 <= self.y1 < self.y2 <= height

    def yolo(self, width, height):
        if not self.within(width, height):
            raise ValueError('bbox outside image')
        return ((self.x1 + self.x2) / (2 * width), (self.y1 + self.y2) / (2 * height),
                (self.x2 - self.x1) / width, (self.y2 - self.y1) / height)


FAMILIES = ('text_geometry', 'occlusion', 'layout', 'readability', 'resources', 'components', 'rendering')


def validate_case(record, public_safe=False):
    required = {'schema_version', 'case_id', 'base_scene_id', 'template_lineage_id',
                'source_project_id', 'structural_family_id', 'split_group', 'split',
                'capture', 'assets', 'has_change', 'has_single_frame_defect',
                'annotation_completeness', 'verification', 'issues', 'provenance'}
    if required - record.keys():
        raise ValueError(f'missing fields: {sorted(required - record.keys())}')
    if record['schema_version'] != 'lumen.case.v1':
        raise ValueError('unsupported schema')
    if 'corpus' in record and record['corpus'] not in ('lumen-synth', 'lumen-modern', 'unclassified'):
        raise ValueError('invalid synthetic case corpus')
    if 'defect_origin' in record and record['defect_origin'] != 'synthetic':
        raise ValueError('invalid synthetic defect origin')
    if not isinstance(record['case_id'], str) or not re.fullmatch('[a-f0-9]{64}', record['case_id']):
        raise ValueError('invalid case_id')
    if record['split'] != group_split(record['split_group']):
        raise ValueError('group split mismatch')
    if record['annotation_completeness'] != 'complete_within_declared_scope':
        raise ValueError('incomplete annotation')
    if record['verification'] not in ('auto_verified', 'human_verified'):
        raise ValueError('unverified case')
    provenance = record['provenance']
    if not provenance.get('license') or not provenance.get('revision'):
        raise ValueError('missing provenance')
    if public_safe and provenance.get('public_release_status') != 'approved':
        raise ValueError('source not approved for public release')
    width, height = record['capture']['image_px']
    if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 16_777_216):
        raise ValueError('image dimensions exceed contract')
    if type(record['has_single_frame_defect']) is not bool or type(record['has_change']) is not bool:
        raise ValueError('flags must be booleans')
    if bool(record['issues']) != record['has_single_frame_defect']:
        raise ValueError('issue flag mismatch')
    if record['issues'] and not record['has_change']:
        raise ValueError('positive must have a change')
    for issue in record['issues']:
        if issue['observability'] != 'single_frame' or issue['family'] not in FAMILIES:
            raise ValueError('invalid issue class')
        for field in ('bbox_xyxy_px', 'context_bbox_xyxy_px'):
            if not Box(*issue[field]).within(width, height):
                raise ValueError('bbox outside image')
        box, context = Box(*issue['bbox_xyxy_px']), Box(*issue['context_bbox_xyxy_px'])
        if not (context.x1 <= box.x1 < box.x2 <= context.x2 and
                context.y1 <= box.y1 < box.y2 <= context.y2):
            raise ValueError('context must contain issue')
        evidence = issue['evidence']
        if (evidence.get('baseline_passed') is not True or evidence.get('after_failed') is not True
                or not isinstance(evidence.get('oracle_id'), str) or not evidence['oracle_id']):
            raise ValueError('missing semantic evidence')
    for name in ('reference', 'current', 'raw_change_mask'):
        digest = record['assets'][name]
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid asset hash')
