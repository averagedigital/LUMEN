"""GitHub reports remain separate from verified training cases."""

import fcntl
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .contracts import canonical, case_id

REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')
SHA = re.compile(r'[a-f0-9]{40}\Z')
VISUAL = re.compile(r'clip|overflow|overlap|z-index|layout|position|distort|wrap|responsive|'
                    r'visual regression|font rendering|flex|grid', re.I)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_json(path):
    if not path.startswith(('/repos/', '/search/issues?')) or '#' in path:
        raise ValueError('invalid GitHub API path')
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'lumen-data',
               'X-GitHub-Api-Version': '2022-11-28'}
    token = os.environ.get('GITHUB_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = Request('https://api.github.com' + path, headers=headers)
    try:
        with build_opener(NoRedirect).open(request, timeout=30) as response:
            body = response.read(8 * 2**20 + 1)
            if len(body) > 8 * 2**20:
                raise ValueError('GitHub response size limit')
            return json.loads(body)
    except HTTPError as error:
        raise RuntimeError(f'GitHub HTTP {error.code}; no automatic retry') from None
    except URLError:
        raise RuntimeError('GitHub transport failed') from None


def repository_name(value):
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value) or '..' in value:
        raise ValueError('invalid repository')
    return value


def closes_issue(message, repository, number):
    references = re.findall(r'\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+'
                            r'(https://github\.com/[\w.-]+/[\w.-]+/issues/\d+|'
                            r'(?:[\w.-]+/[\w.-]+)?#\d+)\b', message, re.I)
    expected = {f'#{number}', f'{repository}#{number}',
                f'https://github.com/{repository}/issues/{number}'}
    return bool(expected.intersection(references))


def evidence_links(body, errors=None):
    urls = re.findall(r'https://[^\s<>"\)]+', body)
    screenshots, reproductions = [], []
    for url in dict.fromkeys(urls):
        try:
            parsed = urlsplit(url)
        except ValueError:
            if errors is None:
                raise
            errors.append({'reason': 'invalid_url'})
            continue
        safe_parameters = {'file', 'view', 'embed', 'initialpath', 'theme', 'hidedevtools', 'hidenavigation'}
        if (parsed.username or parsed.password or
                any(key.lower() not in safe_parameters for key, _ in parse_qsl(parsed.query))):
            continue
        if (parsed.hostname == 'user-images.githubusercontent.com' or
                (parsed.hostname == 'github.com' and parsed.path.startswith('/user-attachments/assets/'))):
            screenshots.append({'source_url': url, 'availability': 'not_checked',
                                'asset_rights': 'unknown', 'sensitive_data_review': 'pending',
                                'ui_review': 'pending', 'annotation_review': 'pending'})
        elif parsed.hostname in ('codesandbox.io', 'stackblitz.com'):
            reproductions.append(url)
    return screenshots, reproductions


def validate_report(record):
    if record.get('schema_version') != 'lumen.wild.v1' or record.get('corpus') != 'lumen-wild':
        raise ValueError('invalid wild schema')
    if record.get('verification') == 'reproduced_verified':
        raise ValueError('reproduction requires a separately verified paired capture')
    if record.get('verification') not in ('reported_unverified', 'fix_linked'):
        raise ValueError('unknown report verification')
    if record.get('issues') is not None or record.get('has_single_frame_defect') is not None:
        raise ValueError('report annotations must remain unknown')
    if record.get('publication_status') != 'pending':
        raise ValueError('report publication requires rights review')
    repository_name(record['source_project'])
    if record['split_group'] != 'github-issue:' + record['issue_url']:
        raise ValueError('issue lineage mismatch')
    if (record['verification'] == 'fix_linked') != bool(record['fixes']):
        raise ValueError('fix evidence mismatch')
    for fix in record['fixes']:
        if not SHA.fullmatch(fix['buggy_revision']) or not SHA.fullmatch(fix['fixed_revision']):
            raise ValueError('invalid fix revision')


class GitHubBugMiner:
    def __init__(self, api=github_json):
        self.api = api

    def inspect(self, repository, number):
        repository_name(repository)
        if type(number) is not int or number <= 0:
            raise ValueError('invalid issue number')
        prefix = f'/repos/{repository}'
        issue = self.api(f'{prefix}/issues/{number}')
        expected_url = f'https://github.com/{repository}/issues/{number}'
        if issue.get('html_url') != expected_url or 'pull_request' in issue:
            raise ValueError('issue identity mismatch or pull request')
        events = []
        for page in range(1, 11):
            batch = self.api(f'{prefix}/issues/{number}/timeline?per_page=100&page={page}')
            if not isinstance(batch, list):
                raise ValueError('invalid timeline response')
            events.extend(batch)
            if len(batch) < 100:
                break
        else:
            raise ValueError('timeline page limit; fix evidence incomplete')
        fixes, candidates = [], []
        for event in events:
            linked = event.get('source', {}).get('issue', {})
            if 'pull_request' in linked and linked.get('html_url'):
                candidates.append(linked['html_url'])
        # The latest closure must identify a commit, not merely a PR cross-reference.
        closures = [event for event in events if event.get('event') in ('closed', 'reopened')]
        closure = closures[-1] if closures else {}
        sha = closure.get('commit_id')
        if (issue['state'] == 'closed' and issue.get('state_reason') != 'not_planned' and
                closure.get('event') == 'closed' and isinstance(sha, str) and SHA.fullmatch(sha)):
            commit = self.api(f'{prefix}/commits/{sha}')
            if commit.get('sha') != sha:
                raise ValueError('commit identity mismatch')
            if closes_issue(commit['commit']['message'], repository, number):
                parents = commit.get('parents', [])
                if not parents or not SHA.fullmatch(parents[0]['sha']):
                    raise ValueError('fix has no valid parent revision')
                fixes.append({'fixed_revision': sha, 'buggy_revision': parents[0]['sha'],
                              'commit_url': f'https://github.com/{repository}/commit/{sha}',
                              'link_evidence': 'closing_event_and_commit_reference',
                              'files_status': 'possibly_truncated' if len(commit.get('files', [])) >= 300 else 'complete',
                              'files': [{'filename': f['filename'], 'patch': f.get('patch'),
                                         'patch_status': 'available' if f.get('patch') else 'unavailable'}
                                        for f in commit.get('files', [])]})
        body = issue.get('body') or ''
        evidence_errors = []
        screenshots, reproductions = evidence_links(body, evidence_errors)
        record = {'schema_version': 'lumen.wild.v1', 'corpus': 'lumen-wild',
                  'origin': 'github_reported', 'source_id': 'github', 'source_project': repository,
                  'issue_url': expected_url, 'title': issue['title'], 'body': body,
                  'source_revision': issue['updated_at'], 'license': 'unknown',
                  'asset_rights': 'unknown', 'publication_status': 'pending',
                  'split_group': 'github-issue:' + expected_url, 'intended_use': 'review_pool',
                  'verification': 'fix_linked' if fixes else 'reported_unverified',
                  'issues': None, 'has_single_frame_defect': None,
                  'visual_candidate': bool(VISUAL.search(issue['title'] + '\n' + body)),
                  'candidate_pull_requests': sorted(set(candidates)), 'fixes': fixes,
                  'screenshots': screenshots, 'reproduction_links': reproductions,
                  'evidence_errors': evidence_errors,
                  'reproduction_status': 'not_attempted'}
        validate_report(record)
        return record

    def search(self, repository, query, limit):
        repository_name(repository)
        if type(limit) is not int or not 1 <= limit <= 300 or not query.strip() or len(query) > 200:
            raise ValueError('invalid search budget or query')
        found = {}
        for page in range(1, 4):
            params = urlencode({'q': f'repo:{repository} is:issue {query}',
                                'per_page': min(limit, 100), 'page': page})
            result = self.api('/search/issues?' + params)
            if result.get('incomplete_results'):
                raise ValueError('GitHub search incomplete')
            for issue in result['items']:
                if issue.get('repository_url') != f'https://api.github.com/repos/{repository}':
                    raise ValueError('search repository mismatch')
                found[issue['number']] = issue
                if len(found) == limit:
                    return list(found.values())
            if len(result['items']) < min(limit, 100):
                break
        return list(found.values())


def mine_reports(repository, query, output, limit):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.github.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _mine_reports(repository, query, root, limit)


def _mine_reports(repository, query, root, limit):
    miner = GitHubBugMiner()
    candidates = miner.search(repository, query, limit)
    paths = []
    for issue in candidates:
        path = root / (case_id({'issue_url': issue['html_url']}) + '.json')
        record = None
        if path.exists():
            record = json.loads(path.read_text())
            validate_report(record)
        if record is None or record['source_revision'] != issue['updated_at']:
            record = miner.inspect(repository, issue['number'])
            temporary = path.with_suffix('.part')
            temporary.write_text(canonical(record) + '\n')
            temporary.replace(path)
        paths.append(path.name)
    return {'github_issues_found': len(paths), 'reports': paths,
            'reproductions_attempted': 0, 'reproductions_verified': 0}
