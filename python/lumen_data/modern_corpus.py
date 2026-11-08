"""Import pinned WebUI snapshots without treating screenshots as defect labels."""
import fcntl
import hashlib
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from xml.etree.ElementTree import SubElement

import html5lib

from .contracts import canonical, case_id
from .renderable import structural_identity
from .resource_guard import guard

VIEWPORTS = {'desktop': [1280, 720], 'tablet': [768, 1024], 'mobile': [375, 812]}


def webui_scene(row, *, revision, split):
    for name in ('sample_id', 'html', 'source_name', 'source_url'):
        if not isinstance(row.get(name), str) or not row[name]:
            raise ValueError(f'invalid {name}')
    if not re.fullmatch('[a-f0-9]{40}', revision) or split not in ('train', 'validation', 'test'):
        raise ValueError('invalid source revision or split')
    for name in ('css', 'js'):
        if not isinstance(row.get(name), str) or len(row[name].encode()) > 8 * 2**20:
            raise ValueError(f'invalid {name}')
    url = urlsplit(row['source_url'])
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('invalid source_url')
    if row.get('viewport') not in VIEWPORTS:
        raise ValueError('invalid viewport')
    structure = structural_identity(row['html'])
    tree = html5lib.parse(row['html'], treebuilder='etree', namespaceHTMLElements=False)
    assets, bodies = {}, {}
    for name, tag, attr, mime, parent in [('css', 'link', 'href', 'text/css', 'head'),
                                         ('js', 'script', 'src', 'text/javascript', 'body')]:
        content = row[name].encode()
        if content:
            digest = hashlib.sha256(content).hexdigest()
            address = f'https://lumen.invalid/assets/{digest}'
            attributes = {attr: address}
            if name == 'css':
                attributes['rel'] = 'stylesheet'
            SubElement(tree.find(parent), tag, attributes)
            assets[address] = {'sha256': digest, 'content_type': mime}
            bodies[digest] = content
    html = html5lib.serialize(tree, tree='etree', quote_attr_values='always', omit_optional_tags=False)
    if len(html.encode()) > 2_000_000:
        raise ValueError('assembled HTML size limit')
    identity = case_id({'dataset': 'ronantakizawa/webui', 'revision': revision, 'sample_id': row['sample_id']})
    # Group the whole source host, including theme/query and route variants.
    group = f'web-origin:{url.hostname.lower()}'
    return {'html': html, 'assets': assets, '_asset_bodies': bodies, 'render_mode': 'offline-js',
            'base_scene_id': identity, 'source_project_id': 'webui-code', 'corpus': 'unclassified',
            'template_lineage_id': group, 'split_group': group,
            'structural_family_id': structure['structural_family_id'], 'source_viewport': VIEWPORTS[row['viewport']],
            'provenance': {'repository': 'ronantakizawa/webui', 'revision': revision,
                'sample_id': row['sample_id'], 'source_name': row['source_name'],
                'source_url': row['source_url'], 'upstream_split': split, 'license': 'mixed',
                'assets_rights': 'unverified', 'public_release_status': 'research_only',
                'framework_claim': row.get('framework'), 'component_type_claim': row.get('component_type')}}


def import_webui(snapshot, output, revision):
    import pyarrow.parquet as pq

    snapshot, root = Path(snapshot), Path(output)
    if not re.fullmatch('[a-f0-9]{40}', revision):
        raise ValueError('invalid source revision')
    root.mkdir(parents=True, exist_ok=True)
    files = sorted((snapshot / 'data').glob('*.parquet'))
    if not files:
        raise ValueError('no WebUI parquet shards')
    with (root / '.import.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guard(root)
        db = sqlite3.connect(root / 'import.sqlite')
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS contract(value TEXT);
                CREATE TABLE IF NOT EXISTS shards(name TEXT PRIMARY KEY, sha256 TEXT, complete INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS identities(id TEXT PRIMARY KEY, split TEXT);
                CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, split TEXT, record TEXT);
                CREATE TABLE IF NOT EXISTS views(id TEXT, viewport TEXT, sha256 TEXT,
                    PRIMARY KEY(id,viewport));
                CREATE TABLE IF NOT EXISTS rejections(shard TEXT, row_id INTEGER, reason TEXT,
                    PRIMARY KEY(shard,row_id));''')
            contract = canonical({'repository': 'ronantakizawa/webui', 'revision': revision,
                                  'shards': [p.name for p in files]})
            previous = db.execute('SELECT value FROM contract').fetchone()
            if previous and previous[0] != contract:
                raise ValueError('WebUI import contract changed')
            with db:
                if not previous:
                    db.execute('INSERT INTO contract VALUES (?)', (contract,))
            (root / 'assets').mkdir(exist_ok=True)
            for path in files:
                guard(root)
                with path.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                done = db.execute('SELECT sha256,complete FROM shards WHERE name=?', (path.name,)).fetchone()
                if done:
                    if done[0] != digest:
                        raise ValueError('WebUI shard changed')
                    if done[1]:
                        continue
                else:
                    with db:
                        db.execute('INSERT INTO shards(name,sha256) VALUES (?,?)', (path.name, digest))
                split = path.name.split('-')[0]
                row_number = 0
                for batch in pq.ParquetFile(path).iter_batches(batch_size=8, use_threads=False,
                        columns=['sample_id', 'html', 'css', 'js', 'source_name', 'source_url',
                                 'viewport', 'image', 'framework', 'component_type']):
                    guard(root)
                    with db:
                        for row in batch.to_pylist():
                            row_number += 1
                            try:
                                record = webui_scene(row, revision=revision, split=split)
                            except ValueError as error:
                                db.execute('INSERT OR REPLACE INTO rejections VALUES (?,?,?)',
                                           (path.name, row_number, str(error)))
                                continue
                            sample = row['sample_id']
                            existing = db.execute('SELECT split FROM identities WHERE id=?', (sample,)).fetchone()
                            if existing and existing[0] != split:
                                raise ValueError('sample crosses upstream splits')
                            db.execute('INSERT OR IGNORE INTO identities VALUES (?,?)', (sample, split))
                            image = row['image']['bytes']
                            image_hash = hashlib.sha256(image).hexdigest()
                            old_view = db.execute('SELECT sha256 FROM views WHERE id=? AND viewport=?',
                                                  (sample, row['viewport'])).fetchone()
                            if old_view and old_view[0] != image_hash:
                                raise ValueError('conflicting sample viewport')
                            db.execute('INSERT OR IGNORE INTO views VALUES (?,?,?)',
                                       (sample, row['viewport'], image_hash))
                            # The source archive keeps every viewport; import one canonical desktop per sample.
                            if row['viewport'] != 'desktop':
                                continue
                            for asset_hash, body in record.pop('_asset_bodies').items():
                                target = root / 'assets' / asset_hash
                                if target.exists():
                                    if hashlib.sha256(target.read_bytes()).hexdigest() != asset_hash:
                                        raise ValueError('source asset checksum')
                                else:
                                    temporary = target.with_suffix('.part')
                                    with temporary.open('wb') as handle:
                                        handle.write(body)
                                        handle.flush()
                                        os.fsync(handle.fileno())
                                    temporary.replace(target)
                            (root / 'references').mkdir(exist_ok=True)
                            (root / 'references' / f'{image_hash}.png').write_bytes(image)
                            record['source_reference_sha256'] = image_hash
                            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?)',
                                       (sample, split, canonical(record)))
                with db:
                    db.execute('UPDATE shards SET complete=1 WHERE name=?', (path.name,))
            target = root / 'manifest.jsonl'
            with target.with_suffix('.part').open('w') as handle:
                for (record,) in db.execute('SELECT record FROM samples ORDER BY id'):
                    handle.write(record + '\n')
            target.with_suffix('.part').replace(target)
            report = {'samples': db.execute('SELECT count(*) FROM samples').fetchone()[0],
                      'viewports': db.execute('SELECT count(*) FROM views').fetchone()[0],
                      'rejections': dict(db.execute('SELECT reason,count(*) FROM rejections GROUP BY reason')),
                      'manifest': str(target), 'revision': revision}
            (root / 'summary.json').write_text(canonical(report) + '\n')
            return report
        finally:
            db.close()
