import io
import re
import sqlite3
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .resource_guard import guard


THUMB_SIZE = 192
MAX_HAMMING = 4
MAX_NORMALIZED_MAE = .02
MIN_EDGE_AGREEMENT = .9
MIN_GRAY_STD = 4
MIN_EDGE_PIXELS = 32
MAX_ASPECT_DELTA = .01
BANDS = ((0, 13), (13, 13), (26, 13), (39, 13), (52, 12))


def image_features(store, reference):
    if store.path(reference).stat().st_size > 32 * 2**20:
        raise ValueError('similarity image byte limit exceeded')
    with Image.open(io.BytesIO(store.read(reference))) as image:
        width, height = image.size
        if width * height > 16 * 2**20:
            raise ValueError('similarity image pixel limit exceeded')
        thumb = np.array(image.convert('RGB').resize((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS))
    gray = cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY)
    informative = float(gray.std()) >= MIN_GRAY_STD and int(edges(thumb).sum()) >= MIN_EDGE_PIXELS
    return width, height, thumb.tobytes(), informative


def edges(thumb):
    gray = cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY).astype(np.int16)
    result = np.zeros(gray.shape, dtype=np.uint8)
    result[:, 1:] |= np.abs(np.diff(gray, axis=1)) >= 8
    result[1:, :] |= np.abs(np.diff(gray, axis=0)) >= 8
    return result


def compare(left, right):
    left = np.frombuffer(left, dtype=np.uint8).reshape(THUMB_SIZE, THUMB_SIZE, 3)
    right = np.frombuffer(right, dtype=np.uint8).reshape(THUMB_SIZE, THUMB_SIZE, 3)
    mae = float(np.abs(left.astype(np.float32) - right).mean() / 255)
    a, b = edges(left), edges(right)
    kernel = np.ones((3, 3), np.uint8)
    agreement = min(float((a & cv2.dilate(b, kernel)).sum() / max(1, a.sum())),
                    float((b & cv2.dilate(a, kernel)).sum() / max(1, b.sum())))
    return mae, agreement


def audit_cross_split(store, signatures, *, max_candidates=100000):
    """Audit (reference SHA, scene ID, split, dHash hex) rows without retaining image sets."""
    if type(max_candidates) is not int or max_candidates < 1:
        raise ValueError('candidate budget must be a positive integer')
    report = {'status': 'complete', 'reason': None, 'images': 0, 'indexed_pairs_examined': 0,
              'candidate_pairs': 0, 'confirmed_pairs': 0, 'unresolved_pairs': 0,
              'examples': [], 'method': 'dhash64_hamming_normalized_rgb_edges_v1',
              'thresholds': {'hamming': MAX_HAMMING, 'normalized_mae': MAX_NORMALIZED_MAE,
                             'edge_agreement': MIN_EDGE_AGREEMENT, 'thumbnail_size': THUMB_SIZE,
                             'minimum_gray_std': MIN_GRAY_STD, 'minimum_edge_pixels': MIN_EDGE_PIXELS,
                             'maximum_aspect_delta': MAX_ASPECT_DELTA}}
    guard(store.root)
    with tempfile.TemporaryDirectory(prefix='.similarity-', dir=store.root) as scratch:
        db = sqlite3.connect(Path(scratch) / 'index.sqlite')
        try:
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-4096')
            db.executescript('''
                CREATE TABLE images(id INTEGER PRIMARY KEY, reference TEXT, scene TEXT, split TEXT,
                    signature TEXT, width INTEGER, height INTEGER, thumb BLOB, informative INTEGER,
                    UNIQUE(reference,split));
                CREATE TABLE bands(image_id INTEGER, band INTEGER, value INTEGER);
                CREATE INDEX band_value ON bands(band,value,image_id);
            ''')
            for reference, scene, split, signature in signatures:
                if not isinstance(signature, str) or not re.fullmatch('[0-9a-fA-F]{16}', signature):
                    raise ValueError('invalid dHash signature')
                if db.execute('SELECT 1 FROM images WHERE reference=? AND split=?', (reference, split)).fetchone():
                    continue
                guard(store.root)
                feature = db.execute('SELECT width,height,thumb,informative FROM images WHERE reference=? LIMIT 1',
                                     (reference,)).fetchone()
                width, height, thumb, informative = feature or image_features(store, reference)
                report['images'] += 1
                value = int(signature, 16)
                band_values = [(band, (value >> offset) & ((1 << bits) - 1))
                               for band, (offset, bits) in enumerate(BANDS)]
                # Five disjoint blocks guarantee a shared block for at most four changed bits.
                conditions = ' OR '.join('(b.band=? AND b.value=?)' for _ in BANDS)
                rows = db.execute(f'''SELECT DISTINCT i.id,i.reference,i.scene,i.split,i.signature
                    FROM bands b JOIN images i ON i.id=b.image_id
                    WHERE i.split<>? AND ({conditions}) ORDER BY i.id''',
                    (split, *(item for pair in band_values for item in pair)))
                for previous_id, previous_reference, previous_scene, previous_split, previous_signature in rows:
                    if report['indexed_pairs_examined'] >= max_candidates:
                        report.update(status='incomplete', reason='candidate_budget_exhausted')
                        return report
                    report['indexed_pairs_examined'] += 1
                    if report['indexed_pairs_examined'] % 128 == 0:
                        guard(store.root)
                    distance = (value ^ int(previous_signature, 16)).bit_count()
                    if distance > MAX_HAMMING:
                        continue
                    report['candidate_pairs'] += 1
                    example = {'left': previous_reference, 'right': reference,
                               'left_scene': previous_scene, 'right_scene': scene,
                               'left_split': previous_split, 'right_split': split, 'hamming': distance}
                    if reference == previous_reference:
                        report['confirmed_pairs'] += 1
                        example['match'] = 'exact_reference'
                    else:
                        pw, ph, previous_thumb, previous_informative = db.execute(
                            'SELECT width,height,thumb,informative FROM images WHERE id=?', (previous_id,)).fetchone()
                        if abs(width * ph - pw * height) / max(width * ph, pw * height) > MAX_ASPECT_DELTA:
                            continue
                        if not informative or not previous_informative:
                            report['unresolved_pairs'] += 1
                            report.update(status='incomplete', reason='low_information')
                            example['match'] = 'unresolved_low_information'
                        else:
                            mae, agreement = compare(thumb, previous_thumb)
                            if mae > MAX_NORMALIZED_MAE or agreement < MIN_EDGE_AGREEMENT:
                                continue
                            report['confirmed_pairs'] += 1
                            example.update(match='normalized_content', normalized_mae=mae, edge_agreement=agreement)
                    if len(report['examples']) < 50:
                        report['examples'].append(example)
                cursor = db.execute('''INSERT INTO images(reference,scene,split,signature,width,height,thumb,informative)
                    VALUES (?,?,?,?,?,?,?,?)''', (reference, scene, split, signature, width, height, thumb, informative))
                db.executemany('INSERT INTO bands VALUES (?,?,?)',
                               ((cursor.lastrowid, band, number) for band, number in band_values))
        finally:
            db.close()
    return report
