import os
import json
from collections import Counter
from pathlib import Path
import shutil
from contextlib import nullcontext
from PIL import Image

from .contracts import Box, FAMILIES, canonical, validate_case, asset_suffix
from .storage import Store
from .pipeline import check_case_assets
from .resource_guard import guard


def link(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    guard(destination.parent)
    if os.stat(source).st_dev != os.stat(destination.parent).st_dev:
        shutil.copyfile(source, destination)
    else:
        os.link(source, destination)


def portable_objects(store, record):
    objects = [(name, digest, 'json', f'annotations/{name}/{digest}.json')
               for name, digest in record.get('sidecars', {}).items()]
    if record['assets'].get('scene_html'):
        digest = record['assets']['scene_html']
        objects.append(('scene_html', digest, 'html', f'scenes/{digest}.html'))
    if record.get('sidecars', {}).get('states'):
        state = json.loads(store.read(record['sidecars']['states'], 'json'))
        if state.get('baseline'):
            digest = state['baseline']
            objects.append(('states_baseline', digest, 'json', f'annotations/states/{digest}.json'))
    for asset in (record.get('recipe', {}).get('source') or {}).get('assets', {}).values():
        digest = asset['sha256']
        suffix = asset_suffix(asset['content_type'])
        objects.append((f'{suffix}_{digest}', digest, suffix, f'assets/{digest}'))
        if asset.get('license_sha256'):
            digest = asset['license_sha256']
            objects.append((f'license_{digest}', digest, 'txt', f'licenses/{digest}.txt'))
    for name, digest, suffix, relative in objects:
        if not name.replace('_', '').isalnum():
            raise ValueError('invalid sidecar name')
        store.read(digest, suffix)
    return objects


def tile_regions(record, size, overlap, *, allow_original=False):
    width, height = record['capture']['image_px']
    axes = []
    for length in (width, height):
        last = max(0, length - size)
        axes.append(sorted(set(range(0, last + 1, size - overlap)) | {last}))
    if len(axes[0]) * len(axes[1]) > 256:
        raise ValueError('tile count exceeds 256 per case')
    regions, covered = [], set()
    for y in axes[1]:
        for x in axes[0]:
            crop = [x, y, min(width, x + size), min(height, y + size)]
            inside, partial = [], False
            for i, issue in enumerate(record['issues']):
                a, b, c, d = issue['bbox_xyxy_px']
                if max(a, crop[0]) >= min(c, crop[2]) or max(b, crop[1]) >= min(d, crop[3]):
                    continue
                if not (x <= a < c <= crop[2] and y <= b < d <= crop[3]):
                    partial = True
                else:
                    inside.append(i)
            if not partial:
                regions.append((crop, inside))
                covered.update(inside)
    if len(covered) != len(record['issues']):
        if allow_original:
            return [([0, 0, width, height], list(range(len(record['issues']))))]
        raise ValueError(f'tile coverage incomplete for {record["case_id"]}; increase size or overlap')
    return regions


def export_run(run, output, format_name, strict=False, public_safe=False, tile_size=None,
               tile_overlap=128, tile_uncovered='error'):
    if format_name not in ('yolo-singleclass', 'yolo-families', 'coco', 'diff-pairs', 'hf-local'):
        raise ValueError('unsupported export format')
    if tile_size is not None and (not format_name.startswith('yolo') or type(tile_size) is not int
                                 or type(tile_overlap) is not int or not 0 <= tile_overlap < tile_size):
        raise ValueError('invalid tile configuration')
    if tile_uncovered not in ('error', 'original') or (tile_size is None and tile_uncovered != 'error'):
        raise ValueError('invalid tile uncovered policy')
    store, output = Store(run), Path(output)
    store.db.execute('BEGIN')
    provenance = set()
    capture_modes = Counter()
    if output.exists():
        raise ValueError('export destination already exists')
    # Validate the entire selection before creating any training labels.
    for record in store.records():
        validate_case(record, public_safe=public_safe)
        capture_modes[record['capture'].get('capture_mode', 'unspecified')] += 1
        check_case_assets(store, record)
        if tile_size is not None:
            tile_regions(record, tile_size, tile_overlap, allow_original=tile_uncovered == 'original')
        if format_name in ('diff-pairs', 'hf-local'):
            portable_objects(store, record)
        provenance.add((record['source_project_id'], record['provenance']['license'],
                        record['provenance'].get('public_release_status', 'unapproved')))
    output.mkdir(parents=True)
    count = 0
    tile_count, background_tiles, original_cases = 0, 0, 0
    if format_name.startswith('yolo'):
        for split in ('train', 'val', 'test'):
            (output / 'images' / split).mkdir(parents=True)
            (output / 'labels' / split).mkdir(parents=True)
        names = ['ui_defect'] if format_name == 'yolo-singleclass' else list(FAMILIES)
        with (output / 'tiles.jsonl').open('w') if tile_size else nullcontext() as manifest:
            for record in store.records():
                key, split = record['case_id'], record['split']
                original = store.path(record['assets']['current'])
                if tile_size:
                    link(original, output / 'originals' / split / f'{key}.png')
                    regions = tile_regions(record, tile_size, tile_overlap,
                                           allow_original=tile_uncovered == 'original')
                else:
                    regions = [([0, 0, *record['capture']['image_px']], list(range(len(record['issues']))))]
                with Image.open(original) if tile_size else nullcontext() as image:
                    for crop, indices in regions:
                        x, y, right, bottom = crop
                        identity = f'{key}-{x}-{y}' if tile_size else key
                        destination = output / 'images' / split / f'{identity}.png'
                        if tile_size:
                            guard(output)
                            image.crop(crop).save(destination)
                            is_original = right - x > tile_size or bottom - y > tile_size
                            manifest.write(canonical({'case_id': key, 'tile_id': identity,
                                'split': split, 'crop_xyxy_px': crop, 'issue_indices': indices,
                                'kind': 'original' if is_original else 'tile'}) + '\n')
                            original_cases += is_original
                            tile_count += not is_original
                            background_tiles += not is_original and not indices
                        else:
                            link(original, destination)
                        lines = []
                        for index in indices:
                            issue = record['issues'][index]
                            class_id = 0 if len(names) == 1 else FAMILIES.index(issue['family'])
                            coordinates = Box(*issue['bbox_xyxy_px']).transform(crop=(x, y)).yolo(right - x, bottom - y)
                            lines.append(str(class_id) + ' ' + ' '.join(f'{value:.9f}' for value in coordinates))
                        (output / 'labels' / split / f'{identity}.txt').write_text('\n'.join(lines) + ('\n' if lines else ''))
                count += 1
        import yaml
        (output / 'data.yaml').write_text(yaml.safe_dump({'path': str(output.resolve()),
            'train': 'images/train', 'val': 'images/val', 'test': 'images/test',
            'names': dict(enumerate(names))}, sort_keys=False))
    elif format_name == 'coco':
        for selected_split in (None, 'train', 'val', 'test'):
            filename = f'annotations-{selected_split}.json' if selected_split else 'annotations.json'
            with (output / filename).open('w') as handle:
                handle.write('{"images":[')
                records = (r for r in store.records() if selected_split is None or r['split'] == selected_split)
                for i, record in enumerate(records):
                    if i:
                        handle.write(',')
                    name = f'images/{record["case_id"]}.png'
                    if selected_split is None:
                        link(store.path(record['assets']['current']), output / name)
                        count += 1
                    width, height = record['capture']['image_px']
                    handle.write(canonical({'id': i, 'file_name': name, 'width': width,
                        'height': height, 'split': record['split'], 'split_group': record['split_group']}))
                handle.write('],"annotations":[')
                annotation = 0
                records = (r for r in store.records() if selected_split is None or r['split'] == selected_split)
                for i, record in enumerate(records):
                    for issue in record['issues']:
                        if annotation:
                            handle.write(',')
                        box = Box(*issue['bbox_xyxy_px'])
                        handle.write(canonical({'id': annotation, 'image_id': i, 'category_id': 0,
                            'bbox': [box.x1, box.y1, box.x2 - box.x1, box.y2 - box.y1],
                            'area': (box.x2 - box.x1) * (box.y2 - box.y1), 'iscrowd': 0}))
                        annotation += 1
                handle.write('],"categories":[{"id":0,"name":"ui_defect"}]}\n')
    else:
        handle = None
        try:
            for record in store.records():
                if count % 1000 == 0:
                    if handle:
                        handle.close()
                    handle = (output / f'manifest-{count // 1000:05d}.jsonl').open('w')
                item = dict(record)
                item['files'] = {}
                for name in ('reference', 'current', 'raw_change_mask'):
                    relative = f'{name}/{record["assets"][name]}.png'
                    destination = output / relative
                    if not destination.exists():
                        link(store.path(record['assets'][name]), destination)
                    item['files'][name] = relative
                for name, digest, suffix, relative in portable_objects(store, record):
                    source = store.path(digest, suffix)
                    if not (output / relative).exists():
                        link(source, output / relative)
                    item['files'][name] = relative
                handle.write(canonical(item) + '\n')
                count += 1
        finally:
            if handle:
                handle.close()
        if format_name == 'hf-local':
            sources = '\n'.join(f'- {source}: {license_name}; {status}.'
                                for source, license_name, status in sorted(provenance))
            (output / 'README.md').write_text('# LUMEN\n\nSynthetic UI defects and visual changes. Automatically verified; not human-labelled.\n\n'
                'Source licenses and release status:\n\n' + sources + '\n\n'
                'Load manifest shards with the JSON datasets loader. File paths are relative to this directory.\n')
    report = {'format': format_name, 'cases': count, 'strict': strict, 'public_safe': public_safe,
              'capture_modes': dict(capture_modes)}
    if tile_size:
        report.update(tiles=tile_count, background_tiles=background_tiles, tile_size=tile_size,
                      original_cases=original_cases, images=tile_count + original_cases,
                      tile_overlap=tile_overlap, uncovered_issue_policy=tile_uncovered,
                      partial_issue_policy='exclude_tile_require_full_coverage')
    (output / 'export.json').write_text(canonical(report) + '\n')
    return {**report, 'output': str(output)}
