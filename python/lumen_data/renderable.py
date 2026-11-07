from collections import Counter
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from .contracts import canonical, case_id
from .resource_guard import guard
from .sources import download

TAILWIND = 'https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css'


class StaticHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()
        self.structure = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.structure.append(tag)
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
    return parser.assets


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
    accepted, rejected = 0, Counter()
    with Path(manifest).open() as source, output.with_suffix('.part').open('w') as target:
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
            except ValueError as error:
                rejected[str(error)] += 1
                continue
            parser = StaticHTML()
            parser.feed(html)
            source_id = record['source']
            identity = case_id({'html': html, 'provenance': record['provenance']})
            structure = case_id(parser.structure)
            guard(output.parent)
            target.write(canonical({'html': html, 'source_project_id': source_id,
                'base_scene_id': identity, 'template_lineage_id': source_id,
                'structural_family_id': structure, 'provenance': record['provenance'],
                'assets': {url: {'sha256': digest, 'content_type': 'text/css'} for url in assets}}) + '\n')
            accepted += 1
    output.with_suffix('.part').replace(output)
    return {'renderable_candidates': accepted, 'rejections': dict(rejected), 'manifest': str(output),
            'assets': {'tailwind': digest, 'license': 'MIT', 'version': '2.2.19'}}
