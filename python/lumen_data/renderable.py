from collections import Counter
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import tempfile

import html5lib

from .contracts import canonical, case_id
from .resource_guard import guard
from .sources import download

TAILWIND = 'https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css'
TAILWIND_LICENSE = 'https://raw.githubusercontent.com/tailwindlabs/tailwindcss/v2.2.19/LICENSE'
METADATA_TAGS = frozenset(('head', 'style', 'title', 'meta', 'link', 'script', 'noscript', 'template'))


class ElementCounter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 10000:
            raise ValueError('structure_limit')


class StaticHTML(ElementCounter):
    def __init__(self):
        super().__init__()
        self.assets = set()

    def handle_starttag(self, tag, attrs):
        super().handle_starttag(tag, attrs)
        values = dict(attrs)
        if tag in ('script', 'iframe', 'object', 'embed', 'video', 'audio'):
            raise ValueError('active_or_unsupported_content')
        if any(key.lower().startswith('on') for key in values):
            raise ValueError('active_event_handler')
        if tag == 'link' and values.get('rel') == 'stylesheet':
            if values.get('href') != TAILWIND:
                raise ValueError('unresolved_stylesheet')
            self.assets.add(TAILWIND)
        for attribute in ('src', 'srcset', 'poster'):
            if attribute in values and not str(values[attribute]).startswith('data:image/'):
                raise ValueError('unresolved_media')


def required_assets(html):
    if len(html) > 2_000_000 or re.search(r'url\s*\(|@import', html, re.IGNORECASE):
        raise ValueError('unresolved_css_resource')
    parser = StaticHTML()
    parser.feed(html)
    parser.close()
    return parser.assets


def structural_identity(html):
    if not isinstance(html, str) or len(html) > 2_000_000:
        raise ValueError('structure_size')
    counter = ElementCounter()
    counter.feed(html)
    counter.close()
    tree = html5lib.parse(html, treebuilder='etree', namespaceHTMLElements=False)
    families, fingerprints = set(), {}
    pending, count = [(tree, 0, False, False)], 0
    while pending:
        node, depth, visited, ignored = pending.pop()
        if not isinstance(node.tag, str):
            continue
        tag = node.tag.rsplit('}', 1)[-1]
        ignored = ignored or tag in METADATA_TAGS
        if not visited:
            count += 1
            if count > 10000 or depth > 128:
                raise ValueError('structure_limit')
            pending.append((node, depth, True, ignored))
            pending.extend((child, depth + 1, False, ignored) for child in reversed(node))
            continue
        if ignored:
            continue
        role = node.get('role', '')
        if tag == 'table' or role in ('table', 'grid'):
            families.add('table')
        if tag in ('form', 'input', 'select', 'textarea') or role == 'form':
            families.add('form')
        if tag == 'nav' or role == 'navigation':
            families.add('navigation')
        children = []
        for child in node:
            fingerprint = fingerprints.pop(child, None)
            if fingerprint is not None and (not children or children[-1] != fingerprint):
                children.append(fingerprint)
        if tag not in METADATA_TAGS:
            label = (tag, role, node.get('type', 'text') if tag == 'input' else '')
            fingerprints[node] = case_id((label, children))
    return {'structural_family_id': '+'.join(sorted(families)) or 'content',
            'split_group': f'dom-v1:{fingerprints[tree]}', 'structure_version': 'dom-v1'}


def prepare_renderable(manifest, output, asset_root):
    output, asset_root = Path(output), Path(asset_root)
    output.parent.mkdir(parents=True, exist_ok=True)
    asset_root.mkdir(parents=True, exist_ok=True)
    guard(asset_root)
    temporary_css = asset_root / 'tailwind.part'
    download(TAILWIND, temporary_css, max_bytes=8 * 2**20)
    digest = hashlib.sha256(temporary_css.read_bytes()).hexdigest()
    css = asset_root / digest
    temporary_css.replace(css)
    temporary_license = asset_root / 'tailwind-license.part'
    guard(asset_root)
    download(TAILWIND_LICENSE, temporary_license, max_bytes=65536)
    license_digest = hashlib.sha256(temporary_license.read_bytes()).hexdigest()
    temporary_license.replace(asset_root / license_digest)
    accepted, rejected = 0, Counter()
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.groups-') as scratch, \
            closing(sqlite3.connect(Path(scratch) / 'lineages.sqlite')) as groups, \
            Path(manifest).open() as source, output.with_suffix('.part').open('w') as target:
        groups.execute('CREATE TABLE lineages (lineage TEXT PRIMARY KEY, split_group TEXT NOT NULL)')
        for line in source:
            if len(line) > 2_100_000:
                rejected['row_size'] += 1
                continue
            record = json.loads(line)
            html = record.get('html')
            if not isinstance(html, str):
                rejected['no_html'] += 1
                continue
            try:
                assets = required_assets(html)
                structure = structural_identity(html)
            except ValueError as error:
                rejected[str(error)] += 1
                continue
            source_id = record['source']
            identity = case_id({'html': html, 'provenance': record['provenance']})
            lineage = record.get('template_lineage_id')
            if lineage is not None:
                if not isinstance(lineage, str) or not lineage:
                    raise ValueError('invalid template lineage')
                previous = groups.execute('SELECT split_group FROM lineages WHERE lineage=?', (lineage,)).fetchone()
                if previous and previous[0] != structure['split_group']:
                    raise ValueError('lineage group conflict')
                groups.execute('INSERT OR IGNORE INTO lineages VALUES (?,?)', (lineage, structure['split_group']))
            guard(output.parent)
            target.write(canonical({'html': html, 'source_project_id': source_id,
                'base_scene_id': identity, 'template_lineage_id': lineage,
                **structure,
                'provenance': record['provenance'],
                'assets': {url: {'sha256': digest, 'content_type': 'text/css',
                    'license': 'MIT', 'license_sha256': license_digest} for url in assets}}) + '\n')
            accepted += 1
    output.with_suffix('.part').replace(output)
    return {'renderable_candidates': accepted, 'rejections': dict(rejected), 'manifest': str(output),
            'assets': {'tailwind': digest, 'license': 'MIT', 'version': '2.2.19'}}
