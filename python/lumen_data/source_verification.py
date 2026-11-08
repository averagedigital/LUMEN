"""Check source reconstruction before mutation, independently of defect labels."""
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import io
import json
from pathlib import Path
import sqlite3

import numpy as np
from PIL import Image

from .contracts import canonical
from .pipeline import Worker, revision
from .resource_guard import guard


def reference_fidelity(original, rendered):
    with Image.open(io.BytesIO(original)) as source, Image.open(io.BytesIO(rendered)) as current:
        if source.size != current.size:
            return {'passed': False, 'reason': 'dimensions'}
        a = np.asarray(source.convert('RGB'), dtype=np.int16)
        b = np.asarray(current.convert('RGB'), dtype=np.int16)
    difference = np.abs(a - b)
    mean = float(difference.mean() / 255)
    fraction = float(np.any(difference > 32, axis=2).mean())
    return {'passed': mean <= .04 and fraction <= .10, 'mean_absolute_error': mean,
            'changed_fraction_32': fraction, 'thresholds': {'mean': .04, 'fraction': .10}}


def verify_sources(manifest, asset_root, references, output, *, workers=2, limit=None):
    if type(workers) is not int or not 1 <= workers <= 4 or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('invalid verification budget')
    manifest, root = Path(manifest), Path(output)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.verification.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with manifest.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        db = sqlite3.connect(root / 'verification.sqlite')
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS contract(value TEXT);
                CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, status TEXT, evidence TEXT, record TEXT);''')
            contract = canonical({'manifest_sha256': digest, 'generator_revision': revision(),
                                  'fidelity_version': 1})
            previous = db.execute('SELECT value FROM contract').fetchone()
            if previous and previous[0] != contract:
                raise ValueError('verification contract changed')
            with db:
                if not previous:
                    db.execute('INSERT INTO contract VALUES (?)', (contract,))
            def check(record):
                guard(root)
                worker = Worker(asset_root)
                try:
                    payload = {'html': record['html'], 'assets': record['assets'],
                               'render_mode': record.get('render_mode', 'static'), 'operator': 'clean',
                               'seed': 42, 'viewport': record['source_viewport'], 'dpr': 1}
                    result = worker.request(payload)
                    if result['status'] == 'resource_error':
                        raise RuntimeError('browser resource_error; verification stopped')
                    if result['status'] != 'accepted_clean':
                        return record, result['status'], result.get('evidence', result.get('error'))
                    second = worker.request(payload)
                    if second['status'] == 'resource_error':
                        raise RuntimeError('browser resource_error; verification stopped')
                    if second.get('reference') != result['reference'] or second['status'] != 'accepted_clean':
                        return record, 'not_reproducible', None
                    source_hash = record['source_reference_sha256']
                    if not isinstance(source_hash, str) or len(source_hash) != 64 or any(c not in '0123456789abcdef' for c in source_hash):
                        raise ValueError('invalid reference hash')
                    original = (Path(references) / f'{source_hash}.png').read_bytes()
                    if hashlib.sha256(original).hexdigest() != source_hash:
                        raise ValueError('reference checksum mismatch')
                    fidelity = reference_fidelity(original, base64.b64decode(result['reference'], validate=True))
                    return record, 'verified' if fidelity['passed'] else 'reference_mismatch', fidelity
                finally:
                    worker.close()
            with ThreadPoolExecutor(max_workers=workers) as executor, manifest.open() as source:
                batch, visited = [], 0
                for line in source:
                    if limit is not None and visited >= limit:
                        break
                    visited += 1
                    record = json.loads(line)
                    if db.execute('SELECT 1 FROM results WHERE id=?', (record['base_scene_id'],)).fetchone():
                        continue
                    batch.append(executor.submit(check, record))
                    if len(batch) == workers:
                        for future in batch:
                            record, status, evidence = future.result()
                            with db:
                                db.execute('INSERT INTO results VALUES (?,?,?,?)',
                                           (record['base_scene_id'], status, canonical(evidence), canonical(record)))
                        batch.clear()
                for future in batch:
                    record, status, evidence = future.result()
                    with db:
                        db.execute('INSERT INTO results VALUES (?,?,?,?)',
                                   (record['base_scene_id'], status, canonical(evidence), canonical(record)))
            with (root / 'manifest.part').open('w') as handle:
                for record, evidence in db.execute("SELECT record,evidence FROM results WHERE status='verified' ORDER BY id"):
                    record = json.loads(record)
                    record['source_verification'] = {'fidelity': json.loads(evidence), 'replay_equal': True}
                    handle.write(canonical(record) + '\n')
            (root / 'manifest.part').replace(root / 'manifest.jsonl')
            report = {'counts': dict(db.execute('SELECT status,count(*) FROM results GROUP BY status')),
                      'manifest': str(root / 'manifest.jsonl')}
            (root / 'summary.part').write_text(canonical(report) + '\n')
            (root / 'summary.part').replace(root / 'summary.json')
            return report
        finally:
            db.close()
