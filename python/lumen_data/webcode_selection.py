"""Bounded, resumable selection from pinned WebCode2M parquet code columns."""
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .contracts import canonical, case_id
from .renderable import required_assets, structural_identity, TAILWIND, TAILWIND_LICENSE
from .resource_guard import guard
from .sources import fetch_json, download

REPOSITORY = 'xcodemind/webcode2m_purified'


def select_row(row, *, revision, shard, row_id):
    if type(row.get('score')) is not int or row['score'] < 2:
        raise ValueError('source_quality_score')
    html = row.get('text')
    if not isinstance(html, str):
        raise ValueError('missing_html')
    assets = required_assets(html)
    structure = structural_identity(html)
    layout = 'grid' if re.search(r'display\s*:\s*(inline-)?grid', html) else (
        'flex' if re.search(r'display\s*:\s*(inline-)?flex', html) else 'flow')
    digest = hashlib.sha256(html.encode()).hexdigest()
    return {'html': html, 'base_scene_id': digest, 'template_lineage_id': None,
            'source_project_id': 'webcode2m', 'corpus': 'unclassified', 'render_mode': 'static',
            **structure, 'selection_stratum': structure['structural_family_id'] + '/' + layout,
            '_required_assets': sorted(assets),
            'provenance': {'repository': REPOSITORY, 'revision': revision, 'shard': shard,
                'row': row_id, 'license': 'cc-by-4.0', 'assets_rights': 'unknown',
                'public_release_status': 'research_only', 'source_quality_score': row['score']}}


def select_webcode(output, revision, *, per_stratum=2000, max_shards=None):
    import aiohttp
    import fsspec
    import pyarrow.parquet as pq

    if (not re.fullmatch('[a-f0-9]{40}', revision) or type(per_stratum) is not int or per_stratum < 1
            or (max_shards is not None and (type(max_shards) is not int or max_shards < 1))):
        raise ValueError('invalid selection budget or revision')
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.selection.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guard(root)
        info_path = root / 'upstream.json'
        if info_path.exists():
            info = json.loads(info_path.read_text())
        else:
            info = fetch_json(f'https://huggingface.co/api/datasets/{REPOSITORY}/revision/{revision}')
            info_path.write_text(canonical(info))
        if info['sha'] != revision:
            raise ValueError('source revision mismatch')
        shards = sorted(f['rfilename'] for f in info['siblings'] if f['rfilename'].endswith('.parquet'))
        if any(not re.fullmatch(r'data/[a-zA-Z0-9_.-]+\.parquet', shard) for shard in shards):
            raise ValueError('invalid shard path')
        db = sqlite3.connect(root / 'selection.sqlite')
        try:
            db.executescript('''CREATE TABLE IF NOT EXISTS contract(value TEXT);
                CREATE TABLE IF NOT EXISTS asset_map(value TEXT);
                CREATE TABLE IF NOT EXISTS shards(name TEXT PRIMARY KEY, stats TEXT);
                CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY, stratum TEXT, rank TEXT, record TEXT);
                CREATE INDEX IF NOT EXISTS strata_rank ON candidates(stratum,rank);''')
            contract = canonical({'revision': revision, 'per_stratum': per_stratum, 'shards': shards,
                                  'selector_version': 1})
            previous = db.execute('SELECT value FROM contract').fetchone()
            if previous and previous[0] != contract:
                raise ValueError('selection contract changed')
            with db:
                if not previous:
                    db.execute('INSERT INTO contract VALUES (?)', (contract,))
            asset_root = root / 'assets'
            asset_root.mkdir(exist_ok=True)
            saved_assets = db.execute('SELECT value FROM asset_map').fetchone()
            if saved_assets:
                assets = json.loads(saved_assets[0])
                for asset in assets.values():
                    path = asset_root / asset['sha256']
                    if hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
                        raise ValueError('source asset checksum')
            else:
                assets = {}
                for url, mime in [(TAILWIND, 'text/css'), (TAILWIND_LICENSE, 'text/plain')]:
                    temporary = asset_root / 'download.part'
                    download(url, temporary, max_bytes=8 * 2**20)
                    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                    temporary.replace(asset_root / digest)
                    assets[url] = {'sha256': digest, 'content_type': mime}
                assets[TAILWIND].update(license='MIT', license_sha256=assets[TAILWIND_LICENSE]['sha256'])
                with db:
                    db.execute('INSERT INTO asset_map VALUES (?)', (canonical(assets),))
            fs = fsspec.filesystem('https', block_size=65536, cache_type='none',
                                  client_kwargs={'timeout': aiohttp.ClientTimeout(total=60)})
            for shard in shards[:max_shards]:
                if db.execute('SELECT 1 FROM shards WHERE name=?', (shard,)).fetchone():
                    continue
                guard(root)
                stats = Counter()
                url = f'https://huggingface.co/datasets/{REPOSITORY}/resolve/{revision}/{shard}'
                with fs.open(url, 'rb') as handle:
                    parquet = pq.ParquetFile(handle, pre_buffer=False)
                    row_id = 0
                    for batch in parquet.iter_batches(columns=['text', 'score'], batch_size=64, use_threads=False):
                        guard(root)
                        with db:
                            for row in batch.to_pylist():
                                current_id, row_id = row_id, row_id + 1
                                stats['rows'] += 1
                                try:
                                    record = select_row(row, revision=revision, shard=shard, row_id=current_id)
                                except ValueError as error:
                                    stats[str(error)] += 1
                                    continue
                                stats['eligible'] += 1
                                record['assets'] = {url: assets[url] for url in record.pop('_required_assets')}
                                identity, stratum = record['base_scene_id'], record['selection_stratum']
                                rank = case_id({'selection': identity})
                                db.execute('INSERT OR IGNORE INTO candidates VALUES (?,?,?,?)',
                                           (identity, stratum, rank, canonical(record)))
                                db.execute('DELETE FROM candidates WHERE stratum=? AND id NOT IN '
                                           '(SELECT id FROM candidates WHERE stratum=? ORDER BY rank,id LIMIT ?)',
                                           (stratum, stratum, per_stratum))
                with db:
                    db.execute('INSERT INTO shards VALUES (?,?)', (shard, canonical(dict(stats))))
                print(canonical({'shard_completed': shard, **dict(stats)}), flush=True)
            totals = Counter()
            for (value,) in db.execute('SELECT stats FROM shards'):
                totals.update(json.loads(value))
            report = {'shards_completed': db.execute('SELECT count(*) FROM shards').fetchone()[0],
                      'shards_total': len(shards), 'counts': dict(totals),
                      'selected': db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                      'strata': dict(db.execute('SELECT stratum,count(*) FROM candidates GROUP BY stratum')),
                      'revision': revision}
            with (root / 'manifest.part').open('w') as output:
                for (value,) in db.execute('SELECT record FROM candidates ORDER BY id'):
                    output.write(value + '\n')
            (root / 'manifest.part').replace(root / 'manifest.jsonl')
            (root / 'summary.part').write_text(canonical(report) + '\n')
            (root / 'summary.part').replace(root / 'summary.json')
            print(canonical(report), flush=True)
            return json.loads((root / 'summary.json').read_text())
        finally:
            db.close()
