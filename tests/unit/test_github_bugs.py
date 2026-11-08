import pytest
import fcntl
import json

from lumen_data.github_bugs import GitHubBugMiner, validate_report, evidence_links, mine_reports
from lumen_data.contracts import validate_case


REPO = 'example/ui'
ISSUE = {'number': 12, 'html_url': 'https://github.com/example/ui/issues/12',
         'title': 'Dropdown clipped by container', 'body': 'Steps: open the menu',
         'state': 'closed', 'state_reason': 'completed', 'updated_at': '2026-01-01T00:00:00Z'}


class API:
    def __init__(self, responses):
        self.responses = responses

    def __call__(self, path):
        return self.responses[path]


def miner(events=(), commit=None):
    responses = {f'/repos/{REPO}/issues/12': ISSUE,
                 f'/repos/{REPO}/issues/12/timeline?per_page=100&page=1': list(events)}
    if commit:
        responses[f'/repos/{REPO}/commits/' + 'a' * 40] = commit
    return GitHubBugMiner(API(responses))


def test_closed_issue_is_unknown_not_a_negative_or_verified_fix():
    record = miner([{'event': 'closed', 'commit_id': None}]).inspect(REPO, 12)
    assert record['verification'] == 'reported_unverified'
    assert record['issues'] is None
    assert record['has_single_frame_defect'] is None
    assert record['publication_status'] == 'pending'
    assert record['corpus'] == 'lumen-wild'
    validate_report(record)
    with pytest.raises(ValueError):
        validate_case(record)


def test_cross_reference_does_not_prove_a_fix():
    event = {'event': 'cross-referenced', 'source': {'issue': {
        'html_url': 'https://github.com/example/ui/pull/15', 'pull_request': {}}}}
    record = miner([event]).inspect(REPO, 12)
    assert record['verification'] == 'reported_unverified'
    assert record['candidate_pull_requests'] == ['https://github.com/example/ui/pull/15']


def test_closing_commit_requires_explicit_issue_reference_and_preserves_parents():
    commit = {'sha': 'a' * 40, 'html_url': 'https://github.com/example/ui/commit/' + 'a' * 40,
              'commit': {'message': 'Fix dropdown clipping\n\nFixes #12'},
              'parents': [{'sha': 'b' * 40}],
              'files': [{'filename': 'menu.css', 'patch': '-overflow: hidden;\n+overflow: visible;'}]}
    event = {'event': 'closed', 'commit_id': 'a' * 40}
    record = miner([event], commit).inspect(REPO, 12)
    assert record['verification'] == 'fix_linked'
    assert record['fixes'][0]['buggy_revision'] == 'b' * 40
    assert record['fixes'][0]['fixed_revision'] == 'a' * 40
    assert record['fixes'][0]['files'][0]['filename'] == 'menu.css'
    assert record['issues'] is None
    validate_report(record)
    commit['commit']['message'] = 'References #12'
    assert miner([event], commit).inspect(REPO, 12)['verification'] == 'reported_unverified'


@pytest.mark.parametrize('message', ['Fixes #123', 'Fixes other/ui#12',
                                     'Fixes https://github.com/other/ui/issues/12'])
def test_unrelated_closing_reference_does_not_link(message):
    commit = {'sha': 'a' * 40, 'commit': {'message': message},
              'parents': [{'sha': 'b' * 40}], 'files': []}
    assert miner([{'event': 'closed', 'commit_id': 'a' * 40}], commit).inspect(
        REPO, 12)['verification'] == 'reported_unverified'


def test_report_cannot_promote_itself_without_reproduction_evidence():
    record = miner().inspect(REPO, 12)
    record['verification'] = 'reproduced_verified'
    with pytest.raises(ValueError, match='reproduction'):
        validate_report(record)


@pytest.mark.parametrize('repository', ['../secret', 'https://github.com/example/ui',
                                       'example/ui?token=x', 'example/ui/extra'])
def test_repository_input_is_validated_before_api_access(repository):
    with pytest.raises(ValueError, match='repository'):
        miner().inspect(repository, 12)


def test_reproduction_query_and_fragment_survive_without_credentials():
    safe = 'https://codesandbox.io/s/menu?file=/src/App.tsx&view=preview#demo'
    _, links = evidence_links(safe + ' https://stackblitz.com/edit/menu?access_token=secret')
    assert links == [safe]


def test_malformed_link_is_diagnosed_without_losing_valid_evidence():
    instance = miner()
    instance.api.responses[f'/repos/{REPO}/issues/12'] = dict(
        ISSUE, body='https://[example.invalid/demo https://stackblitz.com/edit/menu')
    record = instance.inspect(REPO, 12)
    assert record['reproduction_links'] == ['https://stackblitz.com/edit/menu']
    assert record['evidence_errors'] == [{'reason': 'invalid_url'}]


def test_commit_file_list_is_explicitly_incomplete_at_page_boundary():
    commit = {'sha': 'a' * 40, 'commit': {'message': 'Fixes #12'},
              'parents': [{'sha': 'b' * 40}],
              'files': [{'filename': f'file-{index}.css'} for index in range(300)]}
    record = miner([{'event': 'closed', 'commit_id': 'a' * 40}], commit).inspect(REPO, 12)
    assert record['fixes'][0]['files_status'] == 'possibly_truncated'
    commit['files'] = commit['files'][:1]
    record = miner([{'event': 'closed', 'commit_id': 'a' * 40}], commit).inspect(REPO, 12)
    assert record['fixes'][0]['files_status'] == 'complete'


def test_cached_issue_is_refreshed_when_reopened(monkeypatch, tmp_path):
    current = miner([{'event': 'closed', 'commit_id': None}]).inspect(REPO, 12)
    monkeypatch.setattr(GitHubBugMiner, 'search', lambda *args: [dict(ISSUE, updated_at=current['source_revision'])])
    monkeypatch.setattr(GitHubBugMiner, 'inspect', lambda *args: dict(current))
    result = mine_reports(REPO, 'clipped', tmp_path, 1)
    path = tmp_path / result['reports'][0]
    current['source_revision'] = '2026-02-01T00:00:00Z'
    current['title'] = 'Reopened: dropdown still clipped'
    mine_reports(REPO, 'clipped', tmp_path, 1)
    saved = json.loads(path.read_text())
    assert saved['source_revision'] == current['source_revision']
    assert saved['title'] == current['title']


def test_concurrent_miner_cannot_write_same_output(monkeypatch, tmp_path):
    monkeypatch.setattr(GitHubBugMiner, 'search', lambda *args: [])
    with (tmp_path / '.github.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            mine_reports(REPO, 'clipped', tmp_path, 1)
