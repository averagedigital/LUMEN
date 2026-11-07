import base64
from collections import Counter
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import select
import sqlite3
import subprocess
import time

import cv2
import numpy as np
from PIL import Image
import yaml

from .contracts import Box, canonical, case_id, group_split, validate_case
from .resource_guard import guard
from .storage import Store

ARCHETYPES = ('dashboard', 'crm', 'admin', 'shop', 'product', 'checkout', 'settings', 'chat',
              'inbox', 'calendar', 'kanban', 'docs', 'article', 'pricing', 'files', 'editor',
              'spreadsheet', 'wizard', 'search', 'media')
OPERATORS = (('text_clip_x', 'text_clip_y', 'text_contrast_failure', 'viewport_partial_cutoff',
              'sibling_occlusion') * 2 + ('text_clip_x', 'text_clip_y') + ('clean',) * 5 +
             ('benign_theme', 'benign_action', 'benign_action'))


class Worker:
    def __init__(self):
        self.process = subprocess.Popen(['node', '--experimental-strip-types', 'browser/worker.ts'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def request(self, payload):
        self.process.stdin.write((canonical(payload) + '\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 60
        parts, size = [], 0
        while time.monotonic() < deadline:
            ready, _, _ = select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('browser worker exited')
            size += len(chunk)
            if size > 32 * 2**20:
                raise RuntimeError('browser response size limit')
            parts.append(chunk)
            if chunk.endswith(b'\n'):
                return json.loads(b''.join(parts))
        raise RuntimeError('browser worker deadline')

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdout.close()


def decode_image(data, expected):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if list(image.size) != expected:
                raise ValueError('image dimensions mismatch')
            return np.array(image.convert('RGB'))
    except OSError as error:
        raise ValueError('invalid image') from error


def check_case_assets(store, record):
    image_px = record['capture']['image_px']
    before = decode_image(store.read(record['assets']['reference']), image_px)
    after = decode_image(store.read(record['assets']['current']), image_px)
    actual = np.any(before != after, axis=2)
    mask_image = decode_image(store.read(record['assets']['raw_change_mask']), image_px)
    mask = mask_image[:, :, 0]
    if (not np.all((mask == 0) | (mask == 255)) or
            not np.all(mask_image == mask[:, :, None]) or not np.array_equal(actual, mask != 0)):
        raise ValueError('raw change mask invariant failed')
    if bool(np.any(actual)) != record['has_change']:
        raise ValueError('has_change does not match image changes')
    for issue in record['issues']:
        x1, y1, x2, y2 = issue['bbox_xyxy_px']
        if np.count_nonzero(actual[int(y1):int(np.ceil(y2)), int(x1):int(np.ceil(x2))]) < 8:
            raise ValueError('issue has no local visible change')


def image_artifacts(store, reference, current, image_px):
    before, after = decode_image(reference, image_px), decode_image(current, image_px)
    mask = np.any(before != after, axis=2).astype(np.uint8) * 255
    ok, encoded = cv2.imencode('.png', mask)
    if not ok:
        raise ValueError('mask encoding failed')
    stats = {'changed_pixels': int(np.count_nonzero(mask)), 'image_bytes': len(current),
             'mask_bytes': int(encoded.size)}
    return {'reference': store.put(reference), 'current': store.put(current),
            'raw_change_mask': store.put(encoded.tobytes())}, stats


def build_record(payload, result, assets, stats):
    if result.get('status') not in ('accepted_bug', 'accepted_clean', 'accepted_benign_change'):
        raise ValueError('unverified result')
    width, height = [v * payload['dpr'] for v in payload['viewport']]
    scene_id = case_id(payload['scene'])
    archetype = payload['scene']['archetype']
    group = f'lumen-grammar-v1/{archetype}'
    issues = []
    for index, finding in enumerate(result['issues']):
        box = Box(*finding['bbox'])
        issues.append({'id': f'issue-{index}', 'element_id': finding['id'], 'class_id': 0,
                       'family': finding['family'], 'symptom': finding['symptom'],
                       'observability': 'single_frame', 'bbox_xyxy_px': list(box.xyxy),
                       'context_bbox_xyxy_px': [max(0, box.x1 - 24), max(0, box.y1 - 24),
                                               min(width, box.x2 + 24), min(height, box.y2 + 24)],
                       'severity': 'unknown', 'evidence': finding['evidence']})
    record = {'schema_version': 'lumen.case.v1', 'case_id': case_id(payload),
              'base_scene_id': scene_id, 'scene_id': scene_id, 'template_lineage_id': group,
              'source_project_id': 'lumen-grammar', 'structural_family_id': archetype,
              'split_group': group, 'split': group_split(group), 'backend': 'browser',
              'seed': payload['seed'], 'recipe': payload, 'operator': payload['operator'],
              'capture': {'viewport_css': payload['viewport'], 'image_px': [width, height],
                          'device_scale_factor': payload['dpr'], 'browser': result['browser'],
                          'locale': payload['locale'], 'timezone': 'UTC', 'scroll': result.get('scrollBefore', [0, 0])},
              'assets': assets, 'has_change': stats['changed_pixels'] > 0,
              'has_single_frame_defect': bool(issues), 'issues': issues,
              'annotation_completeness': 'complete_within_declared_scope',
              'declared_scope': ['text_clipping', 'text_contrast', 'text_occlusion', 'viewport_cutoff'],
              'verification': 'auto_verified', 'mutations': [result['receipt']] if result.get('receipt') else [],
              'provenance': {'license': 'MIT', 'revision': payload['generator_revision'],
                             'public_release_status': 'approved', 'origin': 'synthetic'},
              'metrics': stats, 'timings_ms': result.get('timings', {})}
    validate_case(record)
    return record


def revision():
    digest = hashlib.sha256()
    for directory, extension in [('browser', '*.ts'), ('scenes', '*.ts'), ('python/lumen_data', '*.py')]:
        for path in sorted(Path(directory).rglob(extension)):
            digest.update(str(path).encode())
            digest.update(path.read_bytes())
    for name in ('package-lock.json', 'uv.lock', 'Dockerfile'):
        digest.update(Path(name).read_bytes())
    return digest.hexdigest()


def recipe(index, config, version):
    base = index // config['variants_per_scene']
    # Separate draws avoid correlating labels with scene families or screen sizes.
    entropy = bytes.fromhex(case_id({'seed': config['seed'], 'base': base}))
    locale = ('en', 'ru', 'de', 'es', 'zh', 'ar')[entropy[0] % 6]
    width = (320, 360, 390, 414, 768, 1024, 1280, 1440, 1920)[entropy[1] % 9]
    operator_draw = int(case_id({'seed': config['seed'], 'index': index, 'draw': 'operator'})[:8], 16)
    return {'scene': {'seed': config['seed'] + base, 'archetype': ARCHETYPES[base % 20],
                      'locale': locale, 'width': width}, 'seed': config['seed'] + index,
            'operator': OPERATORS[operator_draw % len(OPERATORS)], 'viewport': [width, 720 + entropy[2] % 4 * 80],
            'dpr': 1, 'locale': locale, 'generator_revision': version}


def generate(config_path, run, target=None, resume=False):
    config = yaml.safe_load(Path(config_path).read_text())
    if config['variants_per_scene'] not in range(1, 17):
        raise ValueError('variants_per_scene must be between 1 and 16')
    target = config['target'] if target is None else target
    if target <= 0 or config['max_attempts'] < target:
        raise ValueError('invalid target or attempt budget')
    if config.get('source_manifest'):
        raise ValueError('external scenes require validated local asset preparation')
    root = Path(run)
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / 'scheduler.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = Store(root)
    contract = {key: config[key] for key in ('seed', 'variants_per_scene', 'source_manifest')}
    contract['generator_revision'] = revision()
    contract_file = root / 'run.json'
    if contract_file.exists():
        if not resume or json.loads(contract_file.read_text()) != contract:
            raise ValueError('run exists or generation contract changed')
    else:
        contract_file.write_text(canonical(contract) + '\n')
    started = time.monotonic()
    initial_count = store.count()
    worker = Worker()
    try:
        attempts = store.db.execute('SELECT count(*) FROM tasks').fetchone()[0]
        while store.count() < target:
            guard(root, config['min_disk_gib'], config['min_ram_gib'])
            task = store.claim()
            if task is None:
                running = store.db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
                if running:
                    raise RuntimeError('active lease remains; resume after its deadline')
                if attempts >= config['max_attempts']:
                    break
                payload = recipe(attempts, config, contract['generator_revision'])
                store.enqueue(case_id(payload), payload)
                attempts += 1
                task = store.claim()
            payload = task['payload']
            result = worker.request(payload)
            store.heartbeat(task['id'], task['token'])
            if not result['status'].startswith('accepted'):
                evidence = result.get('evidence')
                detail = evidence.get('reason') if isinstance(evidence, dict) else None
                store.finish(task['id'], task['token'], 'rejected', {'reason': result['status'],
                    'detail': detail, 'operator': payload['operator']})
                if result['status'] == 'resource_error':
                    raise RuntimeError('browser resource_error; run stopped')
                continue
            reference = base64.b64decode(result.pop('reference'), validate=True)
            current = base64.b64decode(result.pop('current'), validate=True)
            image_px = [v * payload['dpr'] for v in payload['viewport']]
            assets, stats = image_artifacts(store, reference, current, image_px)
            record = build_record(payload, result, assets, stats)
            mask = decode_image(store.read(assets['raw_change_mask']), image_px)[:, :, 0]
            if any(np.count_nonzero(mask[max(0, int(b[1])):int(np.ceil(b[3])),
                                         max(0, int(b[0])):int(np.ceil(b[2]))]) < 8
                   for b in (i['bbox_xyxy_px'] for i in record['issues'])):
                store.finish(task['id'], task['token'], 'rejected', {'reason': 'no_local_visible_effect'})
                continue
            record['assets']['scene_html'] = store.put(result['scene']['html'].encode(), 'html')
            scene_metadata = {k: v for k, v in result['scene'].items() if k != 'html'}
            record['sidecars'] = {'scene': store.put(canonical(scene_metadata).encode(), 'json')}
            if result.get('elements') is not None:
                elements = [{k: v for k, v in element.items() if k != 'state'} for element in result['elements']]
                states = {element['element_id']: element['state'] for element in result['elements']}
                delta = {element['element_id']: element['state'] for element in result['statesAfter']
                         if states.get(element['element_id']) != element['state']}
                record['sidecars']['elements'] = store.put(canonical({'schema_version': 'lumen.elements.v1',
                    'scene_id': record['scene_id'], 'elements': elements}).encode(), 'json')
                baseline = store.put(canonical({'schema_version': 'lumen.states.v1',
                    'scene_id': record['scene_id'], 'elements': states}).encode(), 'json')
                record['sidecars']['states'] = store.put(canonical({'schema_version': 'lumen.state-delta.v1',
                    'case_id': record['case_id'], 'scene_id': record['scene_id'],
                    'baseline': baseline, 'delta': delta, 'scroll_before': result['scrollBefore'],
                    'scroll_after': result['scrollAfter']}).encode(), 'json')
                record['sidecars']['tasks'] = store.put(canonical({'schema_version': 'lumen.tasks.v1',
                    'scene_id': record['scene_id'], 'task': result['scene']['task']}).encode(), 'json')
                if result.get('interaction'):
                    record['sidecars']['interactions'] = store.put(canonical({'schema_version': 'lumen.interactions.v1',
                        'case_id': record['case_id'], 'scene_id': record['scene_id'],
                        **result['interaction']}).encode(), 'json')
            try:
                store.finish(task['id'], task['token'], 'accepted', record)
            except sqlite3.IntegrityError:
                store.finish(task['id'], task['token'], 'rejected', {'reason': 'duplicate_image'})
            if store.count() % 50 == 0:
                print(canonical({'accepted': store.count(), 'attempts': attempts}), flush=True)
    finally:
        worker.close()
        elapsed = time.monotonic() - started
        stats = {'accepted': store.count(), 'new_cases': store.count() - initial_count,
                 'elapsed_seconds': elapsed, 'accepted_cases_per_minute': (store.count() - initial_count) * 60 / elapsed,
                 'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        (root / 'profile.json').write_text(canonical(stats) + '\n')
        lock.close()
    return stats


def qa(run):
    store = Store(run)
    store.db.execute('BEGIN')
    counters = {key: Counter() for key in ('operators', 'families', 'sources', 'splits', 'locales', 'viewports', 'rejections')}
    positives = 0
    for record in store.records():
        validate_case(record)
        check_case_assets(store, record)
        counters['operators'][record['operator']] += 1
        counters['families'][record['structural_family_id']] += 1
        counters['sources'][record['source_project_id']] += 1
        counters['splits'][record['split']] += 1
        counters['locales'][record['capture']['locale']] += 1
        counters['viewports'][str(record['capture']['image_px'])] += 1
        positives += record['has_single_frame_defect']
    for (value,) in store.db.execute("SELECT result FROM tasks WHERE status='rejected'"):
        counters['rejections'][json.loads(value)['reason']] += 1
    stats = {**counters, 'accepted_cases': store.count(), 'positives': positives,
             'negatives': store.count() - positives,
             'base_scenes': store.db.execute("SELECT count(DISTINCT json_extract(record,'$.base_scene_id')) FROM cases").fetchone()[0],
             'human_verified': 0, 'qa': 'passed'}
    (store.root / 'qa.json').write_text(canonical(stats) + '\n')
    return stats
