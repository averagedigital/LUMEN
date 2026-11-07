import os
from pathlib import Path
import shutil

from .contracts import Box, FAMILIES, canonical, validate_case
from .storage import Store
from .pipeline import check_case_assets


def link(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.stat(source).st_dev != os.stat(destination.parent).st_dev:
        shutil.copyfile(source, destination)
    else:
        os.link(source, destination)


def export_run(run, output, format_name, strict=False, public_safe=False):
    if format_name not in ('yolo-singleclass', 'yolo-families', 'coco', 'diff-pairs', 'hf-local'):
        raise ValueError('unsupported export format')
    store, output = Store(run), Path(output)
    store.db.execute('BEGIN')
    if output.exists():
        raise ValueError('export destination already exists')
    # Validate the entire selection before creating any training labels.
    for record in store.records():
        validate_case(record, public_safe=public_safe)
        check_case_assets(store, record)
    output.mkdir(parents=True)
    count = 0
    if format_name.startswith('yolo'):
        for split in ('train', 'val', 'test'):
            (output / 'images' / split).mkdir(parents=True)
            (output / 'labels' / split).mkdir(parents=True)
        names = ['ui_defect'] if format_name == 'yolo-singleclass' else list(FAMILIES)
        for record in store.records():
            key, split = record['case_id'], record['split']
            link(store.path(record['assets']['current']), output / 'images' / split / f'{key}.png')
            labels = output / 'labels' / split / f'{key}.txt'
            labels.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            for issue in record['issues']:
                class_id = 0 if len(names) == 1 else FAMILIES.index(issue['family'])
                coordinates = Box(*issue['bbox_xyxy_px']).yolo(*record['capture']['image_px'])
                lines.append(str(class_id) + ' ' + ' '.join(f'{value:.9f}' for value in coordinates))
            labels.write_text('\n'.join(lines) + ('\n' if lines else ''))
            count += 1
        import yaml
        (output / 'data.yaml').write_text(yaml.safe_dump({'path': str(output.resolve()),
            'train': 'images/train', 'val': 'images/val', 'test': 'images/test',
            'names': dict(enumerate(names))}, sort_keys=False))
    elif format_name == 'coco':
        with (output / 'annotations.json').open('w') as handle:
            handle.write('{"images":[')
            for i, record in enumerate(store.records()):
                if i:
                    handle.write(',')
                name = f'images/{record["case_id"]}.png'
                link(store.path(record['assets']['current']), output / name)
                width, height = record['capture']['image_px']
                handle.write(canonical({'id': i, 'file_name': name, 'width': width, 'height': height}))
                count += 1
            handle.write('],"annotations":[')
            annotation = 0
            for i, record in enumerate(store.records()):
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
                handle.write(canonical(item) + '\n')
                count += 1
        finally:
            if handle:
                handle.close()
        if format_name == 'hf-local':
            (output / 'README.md').write_text('---\nlicense: mit\ntask_categories:\n- object-detection\n---\n# LUMEN\n\nSynthetic UI defects and visual changes. Automatically verified; not human-labelled.\n\nLoad manifest shards with the JSON datasets loader. Image paths are relative to this directory.\n')
    (output / 'export.json').write_text(canonical({'format': format_name, 'cases': count,
                                                 'strict': strict, 'public_safe': public_safe}) + '\n')
    return {'cases': count, 'output': str(output)}
