"""Import session-verified browser pairs without claiming independent replay."""
import base64
import fcntl
import hashlib
import json
from pathlib import Path
import re
import resource
import time
import numpy as np

from .contracts import canonical, case_id
from .pipeline import OPERATORS, decode_image, prepare_source, revision, save_result, viewport_sweep
from .resource_guard import guard
from .storage import Store


def import_live(manifest, asset_root, run):
    manifest, root = Path(manifest), Path(run)
    root.mkdir(parents=True, exist_ok=True)
    with (root/'scheduler.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (root/'run.json').exists():
            raise ValueError('generation run cannot receive live captures')
        with manifest.open('rb') as handle:
            contract = canonical({'manifest_sha256': hashlib.file_digest(handle, 'sha256').hexdigest(),
                                  'importer_revision': revision()})
        contract_file = root/'live-import.json'
        if contract_file.exists():
            if contract_file.read_text() != contract:
                raise ValueError('live import contract changed')
        else:
            contract_file.write_text(contract)
        store = Store(root)
        started, initial, visited = time.monotonic(), store.count(), 0
        try:
            with manifest.open() as handle:
                for line in handle:
                    guard(root)
                    if len(line) > 1024:
                        raise ValueError('live manifest row size limit')
                    address = json.loads(line)
                    digest = address['sha256']
                    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                        raise ValueError('invalid live object digest')
                    path = manifest.parent/'live-cases'/f'{digest}.json'
                    if path.stat().st_size > 32*2**20:
                        raise ValueError('live object size limit')
                    content = path.read_bytes()
                    if hashlib.sha256(content).hexdigest() != digest:
                        raise ValueError('live object checksum mismatch')
                    bundle = json.loads(content)
                    payload, result = bundle['payload'], bundle['result']
                    source = payload['source']
                    for value in (source['base_scene_id'], payload['generator_revision']):
                        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
                            raise ValueError('invalid live identity digest')
                    if (source.get('capture_mode') != 'live-session' or
                            source.get('render_mode') != 'live-session' or
                            result.get('capture_mode') != 'live-session' or result.get('rollback') is not True or
                            result.get('status') not in ('accepted_bug', 'accepted_clean', 'accepted_benign_change')):
                        raise ValueError('unverified live capture')
                    viewport_sweep({'viewport_sweep': [{k: payload[k] for k in ('viewport', 'dpr', 'resize_path') if k in payload}]})
                    if (payload['operator'] not in OPERATORS or type(payload['seed']) is not int or
                            not 0 <= payload['seed'] < 2**53 or
                            source['base_scene_id'] != address['base_scene_id'] or
                            source['split_group'] != 'web-project:'+source['source_project_id'] or
                            source['provenance']['public_release_status'] != 'research_only' or
                            payload['generator_revision'] != source['provenance']['collector_revision']):
                        raise ValueError('inconsistent live capture metadata')
                    reference = base64.b64decode(result['reference'], validate=True)
                    if hashlib.sha256(reference).hexdigest() != source['source_sha256']:
                        raise ValueError('live baseline checksum mismatch')
                    current = base64.b64decode(result['current'], validate=True)
                    image_px = [n*payload['dpr'] for n in payload['viewport']]
                    changed = not np.array_equal(decode_image(reference, image_px), decode_image(current, image_px))
                    operator, status, issues = payload['operator'], result['status'], result['issues']
                    if not isinstance(issues, list):
                        raise ValueError('invalid live issue list')
                    consistent = ((status == 'accepted_clean' and operator == 'clean' and not changed and not issues) or
                        (status == 'accepted_bug' and operator != 'clean' and not operator.startswith('benign_') and changed and bool(issues)) or
                        (status == 'accepted_benign_change' and operator.startswith('benign_') and changed and not issues))
                    if not consistent:
                        raise ValueError('live status conflicts with operator, image changes or issues')
                    payload['source'] = prepare_source(store, source, asset_root)
                    if payload['source']['html_hash'] != source['provenance']['revision']:
                        raise ValueError('live DOM checksum mismatch')
                    identity = case_id(payload)
                    visited += 1
                    previous = store.db.execute('SELECT status FROM tasks WHERE id=?', (identity,)).fetchone()
                    if previous and previous[0] in ('accepted', 'rejected'):
                        continue
                    store.enqueue(identity, payload)
                    task = store.claim()
                    if task is None or task['id'] != identity:
                        raise RuntimeError('live import lease active; resume after its deadline')
                    save_result(store, task, result)
            elapsed = time.monotonic()-started
            report = {'stage': 'live-import', 'accepted': store.count(), 'new_cases': store.count()-initial,
                'raw_cases': visited, 'elapsed_seconds': elapsed,
                'accepted_cases_per_minute': (store.count()-initial)*60/max(elapsed, .001),
                'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
            (root/'profile.json').write_text(canonical(report)+'\n')
            return report
        finally:
            store.db.close()
