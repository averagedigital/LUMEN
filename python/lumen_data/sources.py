import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import sqlite3
import os
import fcntl
from urllib.parse import quote, urljoin, urlsplit

import yaml

from .contracts import Box, canonical
from .resource_guard import guard


HOSTS = {'huggingface.co', 'datasets-server.huggingface.co', 'cdn-lfs.huggingface.co',
         'cdn-lfs.hf.co', 'cas-bridge.xethub.hf.co', 'cdn-lfs-us-1.hf.co',
         'cdn-lfs-eu-1.hf.co', 'raw.githubusercontent.com', 'codeload.github.com'}
HOSTS.add('us.aws.cdn.hf.co')
HOSTS.add('cdn.jsdelivr.net')


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or
            parsed.password or parsed.port not in (None, 443)):
        raise ValueError(f'unapproved source URL host: {parsed.hostname}')


def download(url, destination, max_bytes=16 * 2**20):
    """Follow only approved HTTPS redirects. curl honors the operator's proxy environment."""
    destination = Path(destination)
    for _ in range(6):
        validate_url(url)
        with tempfile.TemporaryDirectory() as temporary:
            headers = Path(temporary) / 'headers'
            process = subprocess.Popen(['curl', '--silent', '--show-error', '--max-time', '180',
                                        '--connect-timeout', '20', '--proto', '=https',
                                        '--dump-header', str(headers), url],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            size = 0
            try:
                with destination.open('wb') as output:
                    while chunk := process.stdout.read(256 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError('source response size limit')
                        output.write(chunk)
                if process.wait(timeout=10) != 0:
                    raise RuntimeError('source transport failed')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
            blocks = headers.read_text().strip().split('\n\n')
            lines = blocks[-1].splitlines()
            status = int(lines[0].split()[1])
            fields = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
            fields = {k.lower(): v.strip() for k, v in fields.items()}
            if status in (301, 302, 303, 307, 308):
                url = urljoin(url, fields['location'])
                continue
            if status != 200:
                raise RuntimeError(f'source HTTP {status}')
            return size
    raise RuntimeError('source redirect limit')


def fetch_json(url):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'response.json'
        download(url, path)
        return json.loads(path.read_text())


def audit(registry, output):
    results = []
    for source in yaml.safe_load(Path(registry).read_text())['sources']:
        record = {**source, 'status': 'not_fetched', 'decision': 'pending', 'rows': None,
                  'renderable': None, 'revision': None, 'schema_fields': []}
        if 'repository' in source:
            repository = source['repository']
            try:
                info = fetch_json(f'https://huggingface.co/api/datasets/{repository}')
                record['revision'] = info['sha']
                record['license'] = info.get('cardData', {}).get('license', source['license'])
                record['status'] = 'gated' if info.get('gated') else 'available'
                record['files'] = [item['rfilename'] for item in info.get('siblings', [])
                                   if item['rfilename'].endswith('.parquet')]
                sample = fetch_json('https://datasets-server.huggingface.co/first-rows?dataset=' +
                                    quote(repository, safe='') + '&config=' + source['config'] + '&split=train')
                record['schema_fields'] = [f['name'] for f in sample['features']]
                record['sample_rows_inspected'] = len(sample['rows'])
                record['sample_has_html'] = any('<' in str(r['row'].get('text', ''))
                                                for r in sample['rows'])
                size = fetch_json('https://datasets-server.huggingface.co/size?dataset=' + quote(repository, safe=''))
                record['rows_all_configs'] = size['size']['dataset']['num_rows']
                record['rows'] = sum(s['num_rows'] for s in size['size']['splits']
                                     if s['config'] == source['config'])
                record['decision'] = 'holdout' if source['role'] == 'external_benchmark' else 'research_only'
            except (RuntimeError, ValueError, KeyError) as error:
                record['error'] = str(error)[:200]
                record['decision'] = 'pending'
        results.append(record)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(results, indent=2, ensure_ascii=False) + '\n')
    return results


def decode_row(source, row):
    result = {'verification': 'unverified', 'has_single_frame_defect': None,
              'annotation_completeness': 'unknown'}
    if source in ('websight', 'webcode2m'):
        if not isinstance(row.get('text'), str) or len(row['text']) > 2_000_000:
            raise ValueError('invalid HTML row')
        result['html'] = row['text']
    elif source == 'screenparse':
        boxes, labels = row['bboxes'], row['labels']
        if len(boxes) != len(labels) or len(boxes) > 10000:
            raise ValueError('invalid element arrays')
        result['elements'] = []
        for i, (values, label) in enumerate(zip(boxes, labels)):
            box = Box.parse(values, 'xywh')
            if not box.within(row['width'], row['height']):
                raise ValueError('source bbox outside image')
            result['elements'].append({'element_id': str(i), 'role': label,
                                       'bbox_xyxy_px': list(box.xyxy)})
        result['annotation_completeness'] = 'elements_only'
    else:
        raise ValueError('source has no import adapter')
    return result


def prepare(audit_path, source_id, root, max_shards=1, max_rows=None):
    import pyarrow.parquet as parquet

    sources = json.loads(Path(audit_path).read_text())
    source = next(s for s in sources if s['id'] == source_id)
    if source['role'] in ('external_benchmark', 'native_domain_holdout', 'methodology_only'):
        raise ValueError('holdout source cannot enter generation')
    if (source['status'] != 'available' or not source['revision'] or
            source['decision'] != 'research_only' or source.get('error')):
        raise ValueError('source has not passed schema audit')
    if max_shards <= 0 or (max_rows is not None and max_rows <= 0):
        raise ValueError('invalid import budget')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    files = source.get('files', [])
    if source_id == 'websight':
        files = [f for f in files if source['config'] in f]
    if not files:
        raise ValueError('no pinned parquet shards discovered')
    lock = (root / 'import.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = sqlite3.connect(root / 'import.sqlite')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''CREATE TABLE IF NOT EXISTS rows(shard TEXT, row_id INTEGER, record TEXT,
        PRIMARY KEY(shard,row_id)); CREATE TABLE IF NOT EXISTS shards(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS contract(value TEXT);''')
    contract = canonical({key: source[key] for key in ('id', 'revision', 'config')})
    existing = db.execute('SELECT value FROM contract').fetchone()
    if existing and existing[0] != contract:
        raise ValueError('source revision changed')
    if not existing:
        with db:
            db.execute('INSERT INTO contract VALUES (?)', (contract,))
    imported = db.execute('SELECT count(*) FROM rows WHERE record IS NOT NULL').fetchone()[0]
    manifest = root / 'manifest.jsonl'
    try:
        for shard in sorted(files)[:max_shards]:
            if db.execute('SELECT 1 FROM shards WHERE name=?', (shard,)).fetchone():
                continue
            if max_rows is not None and imported >= max_rows:
                break
            guard(root)
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                path = Path(temporary) / 'shard.parquet'
                download(f'https://huggingface.co/datasets/{source["repository"]}/resolve/'
                         f'{source["revision"]}/{shard}', path, max_bytes=2 * 2**30)
                with path.open('rb') as handle:
                    checksum = hashlib.file_digest(handle, 'sha256').hexdigest()
                reader = parquet.ParquetFile(path, pre_buffer=False, buffer_size=65536)
                columns = ['text'] if source_id in ('websight', 'webcode2m') else ['id', 'width', 'height', 'bboxes', 'labels', 'image']
                for i in range(reader.metadata.num_row_groups):
                    group = reader.metadata.row_group(i)
                    selected_bytes = sum(group.column(j).total_uncompressed_size for j in range(group.num_columns)
                                         if group.column(j).path_in_schema.split('.')[0] in columns)
                    if selected_bytes > 256 * 2**20:
                        raise ValueError('parquet row group exceeds import budget')
                for row_id, batch in enumerate(reader.iter_batches(batch_size=1, columns=columns, use_threads=False)):
                    if db.execute('SELECT 1 FROM rows WHERE shard=? AND row_id=?', (shard, row_id)).fetchone():
                        continue
                    guard(root)
                    row = batch.to_pylist()[0]
                    try:
                        record = decode_row(source_id, row)
                    except ValueError:
                        with db:
                            db.execute('INSERT INTO rows VALUES (?,?,NULL)', (shard, row_id))
                        continue
                    record['source'] = source_id
                    record['provenance'] = {key: source[key] for key in
                                             ('repository', 'revision', 'license', 'public_release_status')}
                    record['provenance'].update(shard=shard, shard_sha256=checksum)
                    if source_id == 'screenparse':
                        content = row['image']['bytes']
                        if not content or len(content) > 20 * 2**20:
                            raise ValueError('source image size limit')
                        digest = hashlib.sha256(content).hexdigest()
                        image_path = root / 'images' / digest[:2] / digest
                        image_path.parent.mkdir(parents=True, exist_ok=True)
                        if not image_path.exists():
                            temporary_image = image_path.with_suffix('.part')
                            temporary_image.write_bytes(content)
                            os.replace(temporary_image, image_path)
                        record['image'] = str(image_path.relative_to(root))
                    record['provenance']['row_id'] = row_id
                    with db:
                        db.execute('INSERT INTO rows VALUES (?,?,?)', (shard, row_id, canonical(record)))
                    imported += 1
                    if max_rows is not None and imported >= max_rows:
                        break
                else:
                    with db:
                        db.execute('INSERT OR IGNORE INTO shards VALUES (?)', (shard,))
                if max_rows is not None and imported >= max_rows:
                    break
    finally:
        temporary_manifest = manifest.with_suffix('.part')
        with temporary_manifest.open('w') as out:
            for (record,) in db.execute('SELECT record FROM rows WHERE record IS NOT NULL ORDER BY shard,row_id'):
                out.write(record + '\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary_manifest, manifest)
        rejected = db.execute('SELECT count(*) FROM rows WHERE record IS NULL').fetchone()[0]
        db.close()
        lock.close()
    return {'imported': imported, 'rejected': rejected, 'manifest': str(manifest)}
