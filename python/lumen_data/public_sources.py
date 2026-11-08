import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from .contracts import canonical, case_id
from .pipeline import Worker, decode_image
from .resource_guard import guard
from .storage import Store


def capture_sources(manifest, output, limit):
    if not isinstance(limit, int) or limit < 1:
        raise ValueError('capture limit must be positive')
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.capture.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guard(root)
        store = Store(root)
        worker = None
        try:
            store.db.executescript('''
                CREATE TABLE IF NOT EXISTS captures (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    image_hash TEXT UNIQUE, record TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS capture_scene ON captures(json_extract(record, '$.base_scene_id'))
                    WHERE status='captured';
            ''')
            with Path(manifest).open() as source:
                queued = 0
                while line := source.readline(100001):
                    if queued and queued % 100 == 0:
                        guard(root)
                    queued += 1
                    if len(line) > 100000:
                        raise ValueError('source candidate size limit')
                    candidate = json.loads(line)
                    if candidate.get('kind') not in ('screen', 'block'):
                        raise ValueError('source candidate must be a screen or block')
                    for field in ('url', 'source_project_id', 'template_lineage_id', 'structural_family_id'):
                        if not isinstance(candidate.get(field), str) or not candidate[field]:
                            raise ValueError(f'missing source candidate {field}')
                    hosts = candidate.get('allowed_hosts')
                    if not isinstance(hosts, list) or not hosts or not all(isinstance(h, str) for h in hosts):
                        raise ValueError('missing source allowed_hosts')
                    if not isinstance(candidate.get('provenance'), dict) or not candidate['provenance'].get('license'):
                        raise ValueError('missing source license')
                    with store.db:
                        store.db.execute('INSERT OR IGNORE INTO captures(id,request) VALUES (?,?)',
                                         (case_id(candidate), canonical(candidate)))
            for _ in range(limit):
                row = store.db.execute("SELECT id,request FROM captures WHERE status='pending' ORDER BY rowid LIMIT 1").fetchone()
                if row is None:
                    break
                guard(root)
                candidate = json.loads(row[1])
                if worker is None:
                    worker = Worker(entry='browser/source_worker.ts', timeout=75)
                result = worker.request({'url': candidate['url'], 'allowed_hosts': candidate['allowed_hosts'],
                                         'asset_root': str(root / 'assets')})
                if result['status'] == 'rejected':
                    with store.db:
                        store.db.execute("UPDATE captures SET status='rejected',record=? WHERE id=?",
                                         (canonical(result), row[0]))
                    continue
                if result['status'] == 'resource_error':
                    raise RuntimeError(result['error'])
                if result['status'] != 'captured':
                    raise ValueError('unknown source capture status')
                html = result.pop('html')
                png = base64.b64decode(result.pop('reference'), validate=True)
                image_hash = hashlib.sha256(png).hexdigest()
                scene_id = hashlib.sha256(html.encode()).hexdigest()
                if (scene_id != result['scene_id'] or image_hash != result['source_sha256']
                        or image_hash != result['restored_sha256']):
                    raise ValueError('source capture checksum')
                decode_image(png, [round(v * result['dpr']) for v in result['viewport']])
                if store.db.execute('SELECT 1 FROM captures WHERE image_hash=?', (image_hash,)).fetchone():
                    with store.db:
                        store.db.execute("UPDATE captures SET status='duplicate',record=? WHERE id=?",
                                         (canonical({'duplicate_image_hash': image_hash}), row[0]))
                    continue
                if store.db.execute("SELECT 1 FROM captures WHERE status='captured' AND json_extract(record, '$.base_scene_id')=?",
                                    (scene_id,)).fetchone():
                    with store.db:
                        store.db.execute("UPDATE captures SET status='rejected',record=? WHERE id=?",
                                         (canonical({'error': 'source scene identity conflict',
                                                     'scene_id': scene_id, 'image_hash': image_hash}), row[0]))
                    continue
                guard(root)
                elements = result.pop('elements', None)
                sidecars = {}
                if elements is not None:
                    sidecar = canonical({'schema_version': 'lumen.elements.v1', 'scene_id': scene_id, 'elements': elements})
                    digest = store.put(sidecar.encode(), 'json')
                    sidecars['elements'] = digest
                    path = root / 'annotations' / 'elements' / f'{scene_id}.json'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.part')
                    try:
                        os.link(store.path(digest, 'json'), temporary)
                        os.replace(temporary, path)
                        store.sync_directories(path.parent)
                    finally:
                        temporary.unlink(missing_ok=True)
                record = {**result, 'corpus': 'lumen-modern', 'html_hash': store.put(html.encode(), 'html'),
                          'image_hash': store.put(png), 'base_scene_id': scene_id,
                          'sidecars': sidecars,
                          **{key: candidate[key] for key in ('kind', 'source_project_id',
                             'template_lineage_id', 'structural_family_id')},
                          'provenance': {**candidate['provenance'], 'revision': scene_id,
                                         'source_url': candidate['url'], 'public_release_status': 'research_only'}}
                with store.db:
                    store.db.execute("UPDATE captures SET status='captured',image_hash=?,record=? WHERE id=?",
                                     (image_hash, canonical(record), row[0]))
            target = root / 'manifest.jsonl'
            temporary = root / 'manifest.part'
            try:
                with temporary.open('w') as handle:
                    for index, (record,) in enumerate(store.db.execute("SELECT record FROM captures WHERE status='captured' ORDER BY rowid")):
                        if index % 100 == 0:
                            guard(root)
                        record = json.loads(record)
                        record['html'] = store.read(record.pop('html_hash'), 'html').decode()
                        handle.write(canonical(record) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                store.sync_directories(root)
            finally:
                temporary.unlink(missing_ok=True)
            counts = dict(store.db.execute('SELECT status,count(*) FROM captures GROUP BY status'))
            return {'captured': counts.get('captured', 0), 'rejected': counts.get('rejected', 0),
                    'duplicates': counts.get('duplicate', 0), 'pending': counts.get('pending', 0),
                    'manifest': str(target)}
        finally:
            if worker is not None:
                worker.close()
            store.db.close()
