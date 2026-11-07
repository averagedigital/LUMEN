import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
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
import tempfile
import time

import cv2
import numpy as np
from PIL import Image
import yaml

from .contracts import Box, canonical, case_id, group_split, validate_case, GRAMMAR_V2, asset_suffix
from .resource_guard import guard
from .storage import Store
from .asset_validation import raster_pixels

ARCHETYPES = GRAMMAR_V2
OPERATORS = (('text_clip_x', 'text_clip_y', 'text_contrast_failure', 'viewport_partial_cutoff',
              'sibling_occlusion') * 2 + ('text_clip_x', 'text_clip_y') + ('clean',) * 5 +
             ('benign_theme', 'benign_action', 'benign_action'))
REQUEST_TIMEOUT_SECONDS = 60


class Worker:
    def __init__(self, asset_root=None, *, entry='browser/worker.ts', timeout=None):
        self.timeout = REQUEST_TIMEOUT_SECONDS if timeout is None else timeout
        env = dict(os.environ)
        if asset_root is not None:
            env['LUMEN_ASSET_ROOT'] = str(Path(asset_root).resolve())
        self.process = subprocess.Popen(['node', '--experimental-strip-types', entry],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        os.set_blocking(self.process.stdin.fileno(), False)

    def request(self, payload):
        deadline = time.monotonic() + self.timeout
        message = memoryview((canonical(payload) + '\n').encode())
        sent = 0
        while sent < len(message):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise RuntimeError('browser worker deadline')
            try:
                sent += os.write(self.process.stdin.fileno(), message[sent:])
            except BlockingIOError:
                continue
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
    gray = cv2.resize(cv2.cvtColor(before, cv2.COLOR_RGB2GRAY), (9, 8), interpolation=cv2.INTER_AREA)
    return np.packbits(gray[:, 1:] > gray[:, :-1]).tobytes().hex()


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
    source = payload.get('source')
    scene_id = source['base_scene_id'] if source else case_id(payload['scene'])
    archetype = source['structural_family_id'] if source else payload['scene']['archetype']
    group = source['template_lineage_id'] if source else f'lumen-grammar-v2/{archetype}'
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
              'source_project_id': source['source_project_id'] if source else 'lumen-grammar', 'structural_family_id': archetype,
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
    if source:
        record['provenance'] = source['provenance']
        validate_case(record)
    return record


def register_sources(store, manifest):
    store.db.execute('CREATE TABLE IF NOT EXISTS source_scenes(id INTEGER PRIMARY KEY, scene_id TEXT UNIQUE, record TEXT)')
    with Path(manifest).open() as handle:
        for line in handle:
            if len(line) > 2_100_000:
                raise ValueError('source row exceeds size limit')
            record = json.loads(line)
            scene_id = record['base_scene_id']
            if store.db.execute('SELECT 1 FROM source_scenes WHERE scene_id=?', (scene_id,)).fetchone():
                continue
            guard(store.root)
            if len(record.get('assets', {})) > 128:
                raise ValueError('source asset budget: count')
            asset_bytes, raster_pixel_count = 0, 0
            for asset in record.get('assets', {}).values():
                digest = asset['sha256']
                suffix = asset_suffix(asset['content_type'])
                stored = store.path(digest, suffix)
                original = stored if stored.exists() else Path(os.environ['LUMEN_ASSET_ROOT']) / digest
                size = original.stat().st_size
                if size > 8 * 2**20:
                    raise ValueError('source asset size limit')
                asset_bytes += size
                if asset_bytes > 32 * 2**20:
                    raise ValueError('source asset budget: bytes')
                content = original.read_bytes()
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError('source asset checksum')
                total_pixels = raster_pixels(content, suffix)
                raster_pixel_count += total_pixels
                if raster_pixel_count > 64 * 2**20:
                    raise ValueError('source asset budget: pixels')
                if not stored.exists():
                    store.put(content, suffix)
                target = store.root / 'assets' / digest
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    os.link(stored, target)
                if asset.get('license_sha256'):
                    notice = asset['license_sha256']
                    if not store.path(notice, 'txt').exists():
                        original = Path(os.environ['LUMEN_ASSET_ROOT']) / notice
                        if original.stat().st_size > 65536:
                            raise ValueError('source license size limit')
                        content = original.read_bytes()
                        if hashlib.sha256(content).hexdigest() != notice:
                            raise ValueError('source license checksum')
                        store.put(content, 'txt')
            record['html_hash'] = store.put(record.pop('html').encode(), 'html')
            with store.db:
                store.db.execute('INSERT INTO source_scenes(scene_id,record) VALUES (?,?)',
                                 (scene_id, canonical(record)))


def worker_payload(store, payload):
    result = dict(payload)
    if payload.get('source'):
        result['html'] = store.read(payload['source']['html_hash'], 'html').decode()
        result['assets'] = payload['source']['assets']
    return result


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
    family_index = base // 2 if config.get('source_manifest') else base
    return {'scene': {'seed': config['seed'] + base, 'archetype': ARCHETYPES[family_index % 20],
                      'locale': locale, 'width': width}, 'seed': config['seed'] + index,
            'operator': OPERATORS[operator_draw % len(OPERATORS)], 'viewport': [width, 720 + entropy[2] % 4 * 80],
            'dpr': 1, 'locale': locale, 'generator_revision': version}


def save_result(store, task, result):
    payload = task['payload']
    if not result['status'].startswith('accepted'):
        evidence = result.get('evidence')
        detail = evidence.get('reason') if isinstance(evidence, dict) else None
        if detail is None and result.get('error'):
            detail = result['error']
        store.finish(task['id'], task['token'], 'rejected', {'reason': result['status'],
            'detail': detail, 'operator': payload['operator']})
        if result['status'] == 'resource_error':
            raise RuntimeError('browser resource_error; run stopped')
        return
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
        return
    if result.get('scene'):
        record['assets']['scene_html'] = store.put(result['scene']['html'].encode(), 'html')
        scene_metadata = {k: v for k, v in result['scene'].items() if k != 'html'}
        record['sidecars'] = {'scene': store.put(canonical(scene_metadata).encode(), 'json')}
    else:
        record['assets']['scene_html'] = payload['source']['html_hash']
        record['sidecars'] = {}
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


def generate(config_path, run, target=None, resume=False):
    config = yaml.safe_load(Path(config_path).read_text())
    if config['variants_per_scene'] not in range(1, 17):
        raise ValueError('variants_per_scene must be between 1 and 16')
    worker_count = config.get('workers', 1)
    if type(worker_count) is not int or worker_count not in range(1, 5):
        raise ValueError('workers must be between 1 and 4')
    target = config['target'] if target is None else target
    if target <= 0 or config['max_attempts'] < target:
        raise ValueError('invalid target or attempt budget')
    root = Path(run)
    root.mkdir(parents=True, exist_ok=True)
    lock = (root / 'scheduler.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = Store(root)
    contract = {key: config[key] for key in ('seed', 'variants_per_scene', 'source_manifest')}
    contract['generator_revision'] = revision()
    if config.get('source_manifest'):
        with Path(config['source_manifest']).open('rb') as handle:
            contract['source_manifest_sha256'] = hashlib.file_digest(handle, 'sha256').hexdigest()
    contract_file = root / 'run.json'
    if contract_file.exists():
        if not resume or json.loads(contract_file.read_text()) != contract:
            raise ValueError('run exists or generation contract changed')
    else:
        contract_file.write_text(canonical(contract) + '\n')
    if config.get('source_manifest'):
        register_sources(store, config['source_manifest'])
    started = time.monotonic()
    initial_count = store.count()
    workers = []
    try:
        for _ in range(worker_count):
            guard(root, config['min_disk_gib'], config['min_ram_gib'])
            workers.append(Worker(store.root / 'assets'))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            attempts = store.db.execute('SELECT count(*) FROM tasks').fetchone()[0]
            while store.count() < target:
                guard(root, config['min_disk_gib'], config['min_ram_gib'])
                batch = []
                source_exhausted = False
                for _ in range(min(worker_count, target - store.count())):
                    task = store.claim()
                    if task is None:
                        running = store.db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
                        if running > len(batch):
                            raise RuntimeError('active lease remains; resume after its deadline')
                        if attempts >= config['max_attempts']:
                            break
                        payload = recipe(attempts, config, contract['generator_revision'])
                        base = attempts // config['variants_per_scene']
                        if config.get('source_manifest') and base % 2:
                            row = store.db.execute('SELECT record FROM source_scenes ORDER BY id LIMIT 1 OFFSET ?', (base // 2,)).fetchone()
                            if not row:
                                source_exhausted = True
                                break
                            payload['source'] = json.loads(row[0])
                            payload['scene'] = None
                            payload['locale'] = 'en-US'
                        store.enqueue(case_id(payload), payload)
                        attempts += 1
                        task = store.claim()
                    batch.append(task)
                if not batch:
                    if source_exhausted:
                        raise RuntimeError('prepared source scenes exhausted')
                    break
                futures = [executor.submit(worker.request, worker_payload(store, task['payload']))
                           for worker, task in zip(workers, batch)]
                # Commit recipe order, independent of render completion order.
                for index, (task, future) in enumerate(zip(batch, futures)):
                    store.heartbeat_many(batch[index:])
                    while not wait([future], timeout=30).done:
                        guard(root, config['min_disk_gib'], config['min_ram_gib'])
                        store.heartbeat_many(batch[index:])
                    store.heartbeat_many(batch[index:])
                    guard(root, config['min_disk_gib'], config['min_ram_gib'])
                    save_result(store, task, future.result())
                    if store.count() % 50 == 0:
                        print(canonical({'accepted': store.count(), 'attempts': attempts}), flush=True)
                if source_exhausted:
                    raise RuntimeError('prepared source scenes exhausted')
    finally:
        for worker in workers:
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
    with tempfile.TemporaryDirectory(prefix='.qa-', dir=store.root) as scratch, closing(store.db):
        guard(store.root)
        store.db.execute('ATTACH DATABASE ? AS qa_work', (str(Path(scratch) / 'index.sqlite'),))
        return audit_cases(store)


def audit_cases(store):
    store.db.execute('PRAGMA temp_store=FILE')
    store.db.execute('BEGIN')
    store.db.execute('CREATE TABLE qa_work.signatures(reference TEXT, scene TEXT, split TEXT, signature TEXT, UNIQUE(reference,scene,split))')
    counters = {key: Counter() for key in ('operators', 'families', 'sources', 'splits', 'locales', 'viewports', 'rejections')}
    defect_families, bbox_sizes, issue_counts = Counter(), Counter(), Counter()
    positives = 0
    for index, record in enumerate(store.records()):
        if index % 100 == 0:
            guard(store.root)
            guard(tempfile.gettempdir())
        validate_case(record)
        signature = check_case_assets(store, record)
        store.db.execute('INSERT OR IGNORE INTO qa_work.signatures VALUES (?,?,?,?)',
            (record['assets']['reference'], record['base_scene_id'], record['split'], signature))
        counters['operators'][record['operator']] += 1
        counters['families'][record['structural_family_id']] += 1
        counters['sources'][record['source_project_id']] += 1
        counters['splits'][record['split']] += 1
        counters['locales'][record['capture']['locale']] += 1
        counters['viewports'][str(record['capture']['image_px'])] += 1
        positives += record['has_single_frame_defect']
        issue_counts[str(len(record['issues']))] += 1
        width, height = record['capture']['image_px']
        for issue in record['issues']:
            defect_families[issue['family']] += 1
            a, b, c, d = issue['bbox_xyxy_px']
            fraction = (c - a) * (d - b) / (width * height)
            bbox_sizes['tiny' if fraction < .001 else 'small' if fraction < .01 else 'medium' if fraction < .1 else 'large'] += 1
    guard(store.root)
    guard(tempfile.gettempdir())
    leakage = store.db.execute('SELECT count(*) FROM (SELECT reference FROM qa_work.signatures GROUP BY reference HAVING count(DISTINCT split)>1)').fetchone()[0]
    if leakage:
        raise ValueError(f'baseline split leakage: {leakage} images')
    missing = {'train', 'val', 'test'} - counters['splits'].keys()
    if missing:
        raise ValueError(f'missing dataset splits: {sorted(missing)}')
    near, cross = store.db.execute('''SELECT count(*), coalesce(sum(cross_split),0) FROM (
        SELECT signature,count(DISTINCT split)>1 AS cross_split FROM qa_work.signatures
        GROUP BY signature HAVING count(DISTINCT scene)>1)''').fetchone()
    for (value,) in store.db.execute("SELECT result FROM tasks WHERE status='rejected'"):
        counters['rejections'][json.loads(value)['reason']] += 1
    stats = {**counters, 'accepted_cases': store.count(), 'positives': positives,
             'negatives': store.count() - positives,
             'base_scenes': store.db.execute("SELECT count(DISTINCT json_extract(record,'$.base_scene_id')) FROM cases").fetchone()[0],
             'defect_families': defect_families, 'bbox_area_bins': bbox_sizes, 'issues_per_case': issue_counts,
             'near_duplicate_baseline_clusters': near, 'cross_split_near_duplicate_clusters': cross,
             'similarity_method': 'equal_dhash64_candidates_not_confirmed_duplicates',
             'human_verified': 0, 'qa': 'passed'}
    (store.root / 'qa.json').write_text(canonical(stats) + '\n')
    return stats
