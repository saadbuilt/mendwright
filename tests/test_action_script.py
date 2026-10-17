"""The GitHub Action's script (action/mendwright_action.py), step by step.

Each test builds a workspace that is a real git repository, a bare repository that stands
for GitHub's copy of it (the script's server URL is a file:// URL, so the push goes there),
a runner temp folder with the artifact, an event payload, and the step environment that
action.yml would give. The GitHub REST API is an in-memory fake behind the script's
injectable transport, which records every call. The steps run in-process, in the order
action.yml runs them. No network, no GitHub, no model.
"""

import base64
import collections
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'action' / 'mendwright_action.py'
if not SCRIPT.exists():
    pytest.skip('action/ is not in this copy of the project', allow_module_level=True)
if shutil.which('git') is None:
    pytest.skip('git is not installed, and the action script runs it', allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location('mendwright_action', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ma = _load()

REPO = 'saadbuilt/shop'
API = 'https://api.github.test'
TOKEN = 'ghs_' + 'T0kenValue' * 4
AUTH = base64.b64encode(('x-access-token:' + TOKEN).encode()).decode()
BOT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'
T0 = '2026-10-01T08:00:00+00:00'
T1 = '2026-10-02T09:30:00+00:00'
TERMS = "//input[@name='legal-consent']"
SEARCH = "//input[@name='q']"
TEST_FILE = ('def test_signup(heal):\n'
             '    heal.locator("accept_terms", "#accept-terms").check()\n'
             '    heal.locator("search", "#search-box").fill("x")\n')

INPUTS = {
    'artifact': 'mendwright-store', 'store': 'overrides.json', 'working-directory': '.',
    'paths': '.', 'globs': '', 'destructive-keys': '', 'only-default-branch': 'true',
    'branch-prefix': 'mendwright/fix/', 'dry-run': 'false', 'draft': 'false', 'labels': '',
    'close-stale': 'true', 'tests-result': 'success', 'commit-author-name': 'github-actions[bot]',
    'commit-author-email': BOT_EMAIL, 'python-version': '3.12',
}


def git(cwd, *args, check=True):
    done = subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                           '-c', 'init.defaultBranch=main', '-c', 'commit.gpgsign=false', *args],
                          cwd=str(cwd), capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


def heal(old, new, last=T1, role='textbox'):
    return {'expectedRole': role, 'candidates': [
        {'xpath': old, 'source': 'code', 'status': 'active', 'addedAt': T0, 'lastSucceededAt': T0},
        {'xpath': new, 'source': 'heal', 'status': 'active', 'addedAt': T1, 'lastSucceededAt': last}]}


def confirmed(key, xpath, test='tests/test_checkout.py::test_signup'):
    return [{'ts': T1, 'event': 'heal.applied', 'key': key, 'xpath': xpath,
             'provider': 'fingerprint', 'source': 'fingerprint'},
            {'ts': T1, 'event': 'heal.canary', 'key': key, 'xpath': xpath, 'success': True,
             'by': 'test', 'test': test}]


# --- the fake GitHub ------------------------------------------------------------------------

class FakeGitHub:
    """GitHub's REST API for one repository, in memory. Pull requests and comments are kept;
    compare is answered from the bare repository. queue holds answers for the next matching
    calls: (method, path prefix, status, data, headers)."""

    def __init__(self, world):
        self.world = world
        self.calls = []
        self.pulls = {}
        self.comments = []
        self.labels = {}
        self.queue = []
        self.next_number = 12
        self.clock = 0
        self.repository = {'default_branch': 'main', 'visibility': 'private'}
        self.artifacts = collections.defaultdict(lambda: ['mendwright-store'])
        self.down = False

    def __call__(self, method, url, headers, body, timeout):
        if self.down:
            raise OSError('timed out')
        assert url.startswith(API + '/'), url
        assert timeout == 30
        headers = {k.lower(): v for k, v in headers.items()}
        assert headers['authorization'] == 'Bearer ' + TOKEN
        assert headers['accept'] == 'application/vnd.github+json'
        assert headers['x-github-api-version'] == '2022-11-28'
        assert headers['user-agent'].startswith('mendwright-action/')
        path = url[len(API):]
        data = json.loads(body.decode('utf-8')) if body else None
        self.calls.append((method, path, data))
        for i, (m, prefix, status, answer, extra) in enumerate(self.queue):
            if m == method and path.startswith(prefix):
                del self.queue[i]
                return status, extra or {}, (json.dumps(answer).encode() if answer is not None else b'')
        status, answer = self.route(method, path, data)
        return status, {}, json.dumps(answer).encode()

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != 'GET']

    def open_pull(self, base='main', body='', number=None, **extra):
        number = number or self.next_number
        self.next_number = max(self.next_number, number + 1)
        self.clock += 1
        self.pulls[number] = dict({'number': number, 'state': 'open', 'title': 't', 'body': body,
                                   'head': {'ref': 'mendwright/fix/' + base},
                                   'base': {'ref': base}, 'merged_at': None, 'mergeable': True,
                                   'draft': False, 'updated': self.clock,
                                   'html_url': 'https://github.test/' + REPO + '/pull/' + str(number)},
                                  **extra)
        return self.pulls[number]

    def route(self, method, path, data):
        parts = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segments = [unquote(s) for s in parts.path.split('/')[1:]]
        if segments[:3] != ['repos'] + REPO.split('/'):
            return 404, {'message': 'Not Found'}
        rest = segments[3:]
        if not rest and method == 'GET':
            return 200, dict(self.repository, full_name=REPO)
        if rest[:2] == ['actions', 'runs'] and rest[3:] == ['artifacts']:
            names = (self.artifacts[rest[2]] if isinstance(self.artifacts, collections.defaultdict)
                     else self.artifacts.get(rest[2], []))
            return 200, {'total_count': len(names), 'artifacts': [
                {'name': n, 'expired': False} for n in names if n == query.get('name')]}
        if rest == ['pulls'] and method == 'GET':
            owner, _, branch = query['head'].partition(':')
            assert owner == REPO.split('/')[0]
            found = [p for p in self.pulls.values() if p['head']['ref'] == branch
                     and p['base']['ref'] == query['base'] and p['state'] == query['state']]
            if query['state'] == 'closed':
                found.sort(key=lambda p: -p['updated'])
                found = found[:int(query.get('per_page', 30))]
            return 200, [dict(p) for p in found]
        if rest == ['pulls'] and method == 'POST':
            if any(p['state'] == 'open' and p['head']['ref'] == data['head'] for p in self.pulls.values()):
                return 422, {'message': 'Validation Failed', 'errors': [
                    {'message': 'A pull request already exists for saadbuilt:' + data['head'] + '.'}]}
            assert self.world.remote_sha(data['head']), 'the branch was not pushed first'
            pull = self.open_pull(data['base'], data['body'], title=data['title'],
                                  draft=data['draft'])
            return 201, dict(pull)
        if rest[0] == 'pulls' and len(rest) == 2:
            pull = self.pulls.get(int(rest[1]))
            if pull is None:
                return 404, {'message': 'Not Found'}
            if method == 'PATCH':
                pull.update(data)
                self.clock += 1
                pull['updated'] = self.clock
            return 200, dict(pull)
        if rest[0] == 'issues' and rest[2:] == ['labels']:
            self.labels.setdefault(int(rest[1]), []).extend(data['labels'])
            return 200, [{'name': n} for n in data['labels']]
        if rest[0] == 'issues' and rest[2:] == ['comments']:
            self.comments.append((int(rest[1]), data['body']))
            return 201, {'id': len(self.comments)}
        if rest[0] == 'compare':
            base, _, branch = '/'.join(rest[1:]).partition('...')
            return 200, self.world.compare(base, branch)
        return 404, {'message': 'Not Found'}


# --- the world: a workspace, GitHub's copy of it, a runner -----------------------------------

class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.server = tmp_path / 'server'
        self.bare = self.server / 'saadbuilt' / 'shop.git'
        self.bare.mkdir(parents=True)
        git(self.bare, 'init', '-q', '--bare')
        self.ws = tmp_path / 'ws'
        self.ws.mkdir()
        git(self.ws, 'init', '-q')
        self.write('tests/test_checkout.py', TEST_FILE)
        self.commit('first')
        git(self.ws, 'remote', 'add', 'origin', str(self.bare))
        git(self.ws, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.runner = tmp_path / 'runner'
        self.runner.mkdir()
        self.temp = self.runner / 'mendwright-action'
        self.uploaded = tmp_path / 'uploaded'       # what the test job uploaded
        self.api = FakeGitHub(self)
        self.sleeps = []
        self.counter = 0
        self.event = {'repository': {'default_branch': 'main', 'visibility': 'private',
                                     'full_name': REPO}}
        self.env = {
            'PATH': os.environ.get('PATH', ''), 'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),
            'HOME': str(tmp_path / 'home'), 'GIT_CONFIG_NOSYSTEM': '1',
            'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'push', 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_REF_NAME': 'main', 'GITHUB_SHA': self.head(), 'GITHUB_REPOSITORY': REPO,
            'GITHUB_RUN_ID': '100', 'GITHUB_WORKSPACE': str(self.ws), 'GITHUB_API_URL': API,
            'GITHUB_SERVER_URL': 'file:///' + str(self.server).replace('\\', '/').lstrip('/'),
            'GITHUB_EVENT_PATH': str(tmp_path / 'event.json'), 'RUNNER_TEMP': str(self.runner),
            'GITHUB_OUTPUT': str(tmp_path / 'output.txt'),
            'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary.md'),
            'GITHUB_ACTION_PATH': str(ROOT), 'RUNNER_OS': 'Linux',
        }
        (tmp_path / 'home').mkdir()
        self.inputs = dict(INPUTS)
        self.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox')},
                    confirmed('accept_terms', TERMS))
        self.steps = {}
        self.actions = {}
        self.after_download = None
        self.after_fix = None

    # the workspace and the server

    def write(self, name, text, root=None):
        path = (root or self.ws) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf-8'))
        return path

    def commit(self, message, root=None):
        root = root or self.ws
        git(root, 'add', '-A')
        git(root, 'commit', '-q', '--allow-empty', '-m', message)
        return git(root, 'rev-parse', 'HEAD')

    def head(self):
        return git(self.ws, 'rev-parse', 'HEAD')

    def checkout(self, sha=None):
        """What actions/checkout does for the next run: a clean tree at the tested commit."""
        sha = sha or self.env['GITHUB_SHA']
        git(self.ws, 'checkout', '-q', '-f', '--detach', sha)
        git(self.ws, 'clean', '-q', '-fd')

    def new_commit(self, name, text, message='more'):
        """A commit on main, pushed, that the next run tests."""
        self.checkout(self.remote_sha('main'))
        self.write(name, text)
        sha = self.commit(message)
        git(self.ws, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
        self.env['GITHUB_SHA'] = sha
        return sha

    def remote_sha(self, branch):
        out = git(self.bare, 'rev-parse', '--verify', '-q', 'refs/heads/' + branch, check=False)
        return out or None

    def compare(self, base, branch):
        shas = git(self.bare, 'rev-list', '--reverse', base + '..' + branch).split()
        commits = []
        for sha in shas:
            email, name, message = git(self.bare, 'log', '-1', '--format=%ae%x00%an%x00%B',
                                       sha).split('\0', 2)
            commits.append({'sha': sha, 'commit': {'author': {'email': email, 'name': name},
                                                   'message': message}})
        return {'total_commits': len(commits), 'commits': commits}

    def branch_log(self, branch='mendwright/fix/main'):
        return git(self.bare, 'log', '--format=%ae|%an|%B%x00', 'main..' + branch).strip('\0\n')

    def upload(self, selectors, events, folder='.mendwright', journal=True):
        if self.uploaded.exists():
            shutil.rmtree(self.uploaded)
        target = self.uploaded / folder if folder else self.uploaded
        target.mkdir(parents=True)
        (target / 'overrides.json').write_text(json.dumps({'schema': 2, 'selectors': selectors}),
                                               encoding='utf-8')
        if journal:
            (target / 'journal.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events),
                                                  encoding='utf-8')

    # running the action

    def action(self, step, env):
        self.counter += 1
        out = io.StringIO()
        act = ma.Action(env, stdout=out, transport=self.api, sleep=self.sleeps.append,
                        token_hex=lambda n, c=self.counter: format(c, '0' + str(2 * n) + 'x'),
                        stat=getattr(self, 'stat', os.stat))
        self.actions[step] = act
        return act, out

    def step_env(self, step, extra=()):
        env = dict(self.env)
        for name, value in self.inputs.items():
            env['MW_IN_' + name.upper().replace('-', '_')] = value
        if step in ('guard', 'find-artifact', 'publish'):
            env['MW_TOKEN'] = TOKEN
        env.update(extra)
        return env

    def run(self, until=None):
        """Run the steps as action.yml does. Returns {step: (exit code, stdout, outputs)}."""
        (self.tmp / 'event.json').write_text(json.dumps(self.event), encoding='utf-8')
        if self.temp.exists():
            shutil.rmtree(self.temp)
        for name in ('output.txt', 'summary.md'):
            if (self.tmp / name).exists():
                (self.tmp / name).unlink()
        self.steps = {}
        later = {}

        def step(name, **extra):
            act, out = self.action(name, self.step_env(name, extra))
            code = ma.main([name], act)
            self.steps[name] = (code, out.getvalue(), dict(act.outputs))
            return code, act.outputs

        code, guard = step('guard')
        if code or guard.get('mode') == 'skip' or until == 'guard':
            return self.steps
        later = {'MW_MODE': guard['mode'], 'MW_BASE': guard['base'], 'MW_SHA': guard['sha'],
                 'MW_ARTIFACT_RUN_ID': guard['artifact-run-id'], 'MW_SAME_RUN': guard['same-run']}
        if self.inputs['artifact']:
            code, found = step('find-artifact', **later)
            if code:
                return self.steps
            later['MW_FOUND'] = found['found']
            if found['found'] == 'true':
                # symlinks=True: a link in the artifact arrives as a link, as it can from a
                # downloaded archive, so the action's own check is what refuses it.
                shutil.copytree(str(self.uploaded), str(self.temp / 'artifact'), symlinks=True)
                if self.after_download:
                    self.after_download(self.temp / 'artifact')
        code, fix = step('fix', **later)
        if code or fix.get('ok') != 'true' or until == 'fix':
            return self.steps
        if self.after_fix:
            self.after_fix()
        step('publish', **later)
        return self.steps

    def summary(self):
        path = self.tmp / 'summary.md'
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def outputs(self):
        merged = {}
        for name in ('guard', 'find-artifact', 'fix', 'publish'):
            if name in self.steps:
                for k, v in self.steps[name][2].items():
                    merged.setdefault(k, v) if name != 'publish' else merged.__setitem__(k, v)
        return merged

    def result(self):
        for name in ('publish', 'fix', 'guard'):
            if name in self.steps and 'result' in self.steps[name][2]:
                return self.steps[name][2]['result']
        return ''

    def log(self):
        return ''.join(out for _, out, _ in self.steps.values())


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The action runs on the Python that runs the tests; CI also tries one it refuses.
    monkeypatch.setattr(ma, '_python', lambda: ((3, 12), '3.12.0'))
    return World(tmp_path)


def pull_marker(pull):
    return ma.read_marker(pull['body'])


# --- the whole run ------------------------------------------------------------------------------

def test_a_confirmed_heal_becomes_one_commit_and_a_pull_request(world):
    world.inputs.update(labels='mendwright, selectors', draft='true')
    steps = world.run()
    assert [steps[s][0] for s in ('guard', 'find-artifact', 'fix', 'publish')] == [0, 0, 0, 0]
    assert world.result() == 'created'
    out = world.outputs()
    assert (out['pull-request-number'], out['written'], out['left-alone']) == ('12', '1', '0')
    assert out['pull-request-url'].endswith('/pull/12')
    [pull] = world.api.pulls.values()
    assert pull['title'] == 'mendwright fix: write back 1 healed selector'
    assert pull['draft'] is True
    assert world.api.labels == {12: ['mendwright', 'selectors']}
    body = pull['body']
    assert body.startswith(ma.INTRO[0] + '\n\n' + ma.INTRO[1] + '\n\n### mendwright fix\n')
    assert '| `accept_terms` (checkbox) | `#accept-terms` | `' + TERMS + '` | fingerprint (no model) ' \
           '| passing test `tests/test_checkout.py::test_signup` | `tests/test_checkout.py:2` |' in body
    assert 'From run [#100](' + world.env['GITHUB_SERVER_URL'] + '/' + REPO + '/actions/runs/100) ' \
           'on `main` at `' + world.env['GITHUB_SHA'][:7] + '`, with mendwright ' in body
    marker = pull_marker(pull)
    assert marker['keys'] == ['accept_terms'] and marker['more'] is False
    assert (marker['run'], marker['sha'], marker['state']) == (100, world.env['GITHUB_SHA'], 'open')
    assert body.rstrip('\n').splitlines()[-1].startswith('<!-- mendwright-fix {')

    # One commit, the action's, with only the test file in it.
    log = world.branch_log()
    email, name, message = log.split('|', 2)
    assert (email, name) == (BOT_EMAIL, 'github-actions[bot]')
    assert message.strip() == ('mendwright fix: write back 1 healed selector\n\nFrom '
                               + world.env['GITHUB_SERVER_URL'] + '/' + REPO + '/actions/runs/100, '
                               'tests on ' + world.env['GITHUB_SHA'] + '.')
    assert 'accept_terms' not in message and '#' not in message.split('runs/')[0]
    files = git(world.bare, 'diff', '--name-only', 'main', 'mendwright/fix/main').split()
    assert files == ['tests/test_checkout.py']
    changed = git(world.bare, 'show', 'mendwright/fix/main:tests/test_checkout.py')
    assert 'heal.locator("accept_terms", "' + TERMS + '")' in changed
    summary = world.summary()
    assert summary.startswith('## mendwright\n\nOpened #12: mendwright fix: write back 1 healed '
                              'selector.\n')
    assert steps['publish'][1].rstrip().endswith('mendwright: opened #12 with 1 healed selector '
                                                 '(https://github.test/' + REPO + '/pull/12).')


# --- guard: which runs the action works for ------------------------------------------------------

def notices(world, step='guard', kind='notice'):
    out = world.steps[step][1]
    return [line[len('::' + kind + '::'):] for line in out.splitlines()
            if line.startswith('::' + kind + '::')]


def test_pull_request_target_is_refused(world):
    world.env['GITHUB_EVENT_NAME'] = 'pull_request_target'
    steps = world.run()
    assert list(steps) == ['guard'] and steps['guard'][0] == 1
    assert notices(world, kind='error') == [
        "mendwright: this workflow runs on pull_request_target, where a job has a write token and "
        "your secrets while it can see a fork's changes. The mendwright action does not run there. "
        "Run it in a job that needs your test job on push, schedule or workflow_dispatch, as the "
        "guide shows."]
    assert 'mode' not in steps['guard'][2] and 'result' not in steps['guard'][2]
    assert world.summary().startswith('## mendwright\n\nStopped: mendwright: this workflow runs on '
                                      'pull_request_target')
    assert world.api.calls == []


def test_pull_request_is_skipped_unless_it_is_a_dry_run(world):
    world.env.update(GITHUB_EVENT_NAME='pull_request', GITHUB_REF='refs/pull/7/merge',
                     GITHUB_BASE_REF='main')
    steps = world.run()
    assert (steps['guard'][0], steps['guard'][2]['mode'], world.result()) == (0, 'skip', 'skipped')
    assert notices(world) == [
        'mendwright: skipped on pull_request. The action opens pull requests from runs on push, '
        'schedule or workflow_dispatch. Set dry-run: true to see what it would write.']
    assert world.summary() == ('## mendwright\n\nSkipped: on pull_request. The action opens pull '
                               'requests from runs on push, schedule or workflow_dispatch. Set '
                               'dry-run: true to see what it would write.\n')
    world.inputs['dry-run'] = 'true'
    steps = world.run()
    assert steps['guard'][2]['mode'] == 'dry-run' and steps['guard'][2]['base'] == 'main'
    assert world.result() == 'dry-run'
    assert world.api.writes() == []
    assert world.remote_sha('mendwright/fix/main') is None
    assert git(world.ws, 'status', '--porcelain') == ''
    assert world.summary().startswith('## mendwright\n\nDry run: this is what mendwright would '
                                      'write back.\n\nThe tests of this run: success.\n\n### '
                                      'mendwright fix\n\n#### Selectors to write back (1, dry run)')


def test_a_tag_is_skipped(world):
    world.env['GITHUB_REF'] = 'refs/tags/v1.2.0'
    world.run()
    assert world.result() == 'skipped'
    assert notices(world) == ['mendwright: skipped: this run is for the tag v1.2.0, not a branch.']


def test_another_event_is_skipped(world):
    world.env['GITHUB_EVENT_NAME'] = 'release'
    world.run()
    assert world.result() == 'skipped'
    assert notices(world) == [
        'mendwright: skipped on release. The action runs on push, schedule, workflow_dispatch and '
        'workflow_run, and does a dry run on pull_request.']


def test_the_action_s_own_branch_is_skipped(world):
    world.env['GITHUB_REF'] = 'refs/heads/mendwright/fix/main'
    world.run()
    assert world.result() == 'skipped'
    assert notices(world) == ['mendwright: skipped: mendwright/fix/main is a branch this action '
                              'writes.']


def test_only_the_default_branch_unless_asked(world):
    world.env['GITHUB_REF'] = 'refs/heads/feature/x'
    world.run()
    assert world.result() == 'skipped'
    assert notices(world) == [
        'mendwright: skipped: the tests ran on feature/x, and only-default-branch is true, so '
        'pull requests go into main only. Set only-default-branch: false to open them for other '
        'branches too.']
    world.inputs['only-default-branch'] = 'false'
    git(world.ws, 'push', '-q', 'origin', 'HEAD:refs/heads/feature/x')
    world.run()
    assert world.result() == 'created'
    [pull] = world.api.pulls.values()
    assert pull['base']['ref'] == 'feature/x' and pull['head']['ref'] == 'mendwright/fix/feature/x'


def _workflow_run(world, event='push', head_repo=REPO, branch='main', run_id=555):
    world.env.update(GITHUB_EVENT_NAME='workflow_run', GITHUB_RUN_ID='900')
    world.event['workflow_run'] = {'id': run_id, 'event': event, 'head_branch': branch,
                                   'head_sha': world.head(),
                                   'head_repository': {'full_name': head_repo},
                                   'repository': {'full_name': REPO}}
    world.api.artifacts[str(run_id)] = ['mendwright-store']


def test_a_workflow_run_from_a_fork_or_a_pull_request_is_skipped_and_nothing_is_read(world):
    _workflow_run(world, event='pull_request', head_repo='someone/fork')
    world.run()
    assert world.result() == 'skipped'
    assert notices(world) == [
        "mendwright: skipped: the run that made this store was triggered by pull_request from "
        "someone/fork, so code from outside this repository's branches could have written it. The "
        "action only uses stores from runs on push, schedule or workflow_dispatch in saadbuilt/shop."]
    assert world.api.calls == []
    _workflow_run(world, event='pull_request')
    world.run()
    assert world.result() == 'skipped'
    assert notices(world) == [
        'mendwright: skipped: the run that made this store was triggered by pull_request in '
        'saadbuilt/shop, so code from a pull request could have written it. The action only uses '
        'stores from runs on push, schedule or workflow_dispatch in saadbuilt/shop.']
    assert world.api.calls == []


def test_a_workflow_run_from_push_takes_the_branch_commit_and_run_from_the_payload(world):
    _workflow_run(world)
    tested = world.head()
    steps = world.run()
    guard = steps['guard'][2]
    assert (guard['base'], guard['sha'], guard['artifact-run-id'], guard['same-run']) == (
        'main', tested, '555', 'false')
    assert world.api.paths('GET')[0].startswith('/repos/saadbuilt/shop/actions/runs/555/artifacts?')
    assert world.result() == 'created'
    assert pull_marker(world.api.pulls[12])['run'] == 555


def test_a_branch_name_with_a_command_in_it_never_reaches_a_shell(world):
    _workflow_run(world, branch='a$(touch pwned)`b`')
    world.run()
    assert world.result() == 'skipped'
    assert not (world.tmp / 'pwned').exists() and not (world.ws / 'pwned').exists()
    world.inputs['only-default-branch'] = 'false'
    world.run()
    assert world.steps['guard'][0] == 1
    assert notices(world, kind='error') == [
        "mendwright: branch-prefix 'mendwright/fix/' must end with '/' and make a valid branch name "
        "with the base branch, and git check-ref-format refused 'mendwright/fix/a$(touch pwned)`b`'."]
    assert not (world.tmp / 'pwned').exists() and not (world.ws / 'pwned').exists()


def test_a_schedule_payload_without_a_repository_asks_the_api(world):
    world.env['GITHUB_EVENT_NAME'] = 'schedule'
    world.event = {'schedule': '0 5 * * *'}
    world.run()
    assert world.api.paths('GET')[0] == '/repos/saadbuilt/shop'
    assert world.result() == 'created'


def test_a_public_repository_gets_a_notice(world):
    world.event['repository']['visibility'] = 'public'
    world.run()
    assert 'mendwright: this repository is public, so the branch and pull request are public '\
           'too. A healed selector can quote text from your pages.' in notices(world)
    assert world.result() == 'created'


def test_a_checkout_that_is_not_the_tested_commit_is_refused(world):
    tested = world.head()
    world.write('README.md', 'later\n')
    later = world.commit('later')
    world.env['GITHUB_SHA'] = tested
    world.run()
    assert world.steps['guard'][0] == 1 and 'fix' not in world.steps
    assert notices(world, kind='error') == [
        'mendwright: the checkout is at ' + later[:7] + ', but the tests ran on ' + tested[:7]
        + '. mendwright fix must change the code the tests ran on, so nothing was changed. Check '
        'out that commit: under workflow_run, give actions/checkout ref: '
        'github.event.workflow_run.head_sha.']
    world.inputs['dry-run'] = 'true'
    world.run()
    assert world.result() == 'dry-run'
    assert any(n.startswith('mendwright: the checkout is at ') for n in notices(world))


def test_a_dirty_checkout_is_refused_and_an_untracked_file_is_fine(world):
    world.write('notes.txt', 'untracked\n')
    world.write('tests/test_checkout.py', TEST_FILE + '# edited\n')
    world.run()
    assert world.steps['guard'][0] == 1
    assert notices(world, kind='error') == [
        'mendwright: the checkout has changes to tracked files before mendwright fix ran '
        '(tests/test_checkout.py). Run the action right after actions/checkout, so the pull '
        "request holds only mendwright's changes."]
    assert '(`tests/test_checkout.py`)' in world.summary()
    git(world.ws, 'checkout', '--', 'tests/test_checkout.py')
    world.run()
    assert world.result() == 'created'
    assert git(world.bare, 'diff', '--name-only', 'main', 'mendwright/fix/main') == \
        'tests/test_checkout.py'


@pytest.mark.parametrize('name, value, message', [
    ('dry-run', 'yes', "mendwright: the input dry-run takes true or false, got 'yes'."),
    ('close-stale', 'TRUE', "mendwright: the input close-stale takes true or false, got 'TRUE'."),
    ('tests-result', 'ok', "mendwright: the input tests-result takes a job result such as success "
                           "or failure (pass needs.test.result), got 'ok'."),
    ('store', '../x.json', "mendwright: the input store takes a relative path without '..', such "
                           "as overrides.json, got '../x.json'."),
    ('store', '/etc/x.json', "mendwright: the input store takes a relative path without '..', "
                             "such as overrides.json, got '/etc/x.json'."),
    ('labels', ','.join('l' + str(i) for i in range(12)),
     'mendwright: the input labels takes at most 10 labels, got 12.'),
    ('branch-prefix', 'mendwright fix/', "mendwright: branch-prefix 'mendwright fix/' must end with "
                                         "'/' and make a valid branch name with the base branch, "
                                         "and git check-ref-format refused 'mendwright fix/main'."),
    ('branch-prefix', 'mw-', "mendwright: branch-prefix 'mw-' must end with '/' and make a valid "
                             "branch name with the base branch, such as mendwright/fix/."),
    ('branch-prefix', '', "mendwright: branch-prefix '' must end with '/' and make a valid branch "
                          "name with the base branch, such as mendwright/fix/."),
])
def test_bad_inputs_are_refused(world, name, value, message):
    world.inputs[name] = value
    world.run()
    assert world.steps['guard'][0] == 1
    assert notices(world, kind='error') == [message]


def test_a_working_directory_outside_the_workspace_is_refused(world):
    world.inputs['working-directory'] = '../other'
    world.run()
    assert notices(world, kind='error') == [
        'mendwright: working-directory ../other is outside the workspace ('
        + os.path.realpath(str(world.ws)) + '). Pass a folder inside your checkout.']


def test_a_working_directory_that_is_not_a_checkout_is_refused(world, tmp_path):
    world.env['GITHUB_WORKSPACE'] = str(tmp_path / 'plain')
    (tmp_path / 'plain' / 'services').mkdir(parents=True)
    world.inputs['working-directory'] = 'services'
    world.run()
    assert notices(world, kind='error') == ['mendwright: services is not inside a git checkout. '
                                            'Run actions/checkout first.']


def test_an_old_python_is_refused(world, monkeypatch):
    monkeypatch.setattr(ma, '_python', lambda: ((3, 9), '3.9.18'))
    world.run()
    assert notices(world, kind='error') == [
        'mendwright: the action needs Python 3.10 to 3.14, for its lxml wheel, and got 3.9.18. Set '
        'python-version to 3.12, or leave it at its default.']


def test_the_script_parses_on_python_3_8_so_the_guard_can_say_so():
    import ast
    ast.parse(SCRIPT.read_text(encoding='utf-8'), feature_version=(3, 8))


# --- the fix step: finding the store ----------------------------------------------------------

def test_the_store_at_the_artifact_s_top_or_in_mendwright(world):
    for folder in ('', '.mendwright'):
        world.checkout()
        world.api.pulls.clear()
        world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox')},
                     confirmed('accept_terms', TERMS), folder=folder)
        git(world.bare, 'update-ref', '-d', 'refs/heads/mendwright/fix/main', check=False)
        world.run()
        assert world.result() == 'created', (folder, world.log())


def test_no_artifact_is_nothing_to_write_back(world):
    world.api.artifacts = {}
    steps = world.run()
    assert world.result() == 'nothing' and steps['fix'][2]['ok'] == 'false'
    assert 'publish' not in steps
    [notice] = notices(world, 'fix')
    assert notice == ('mendwright: run 100 has no artifact named mendwright-store, so there is '
                      'nothing to write back. If your tests use mendwright, upload the store with '
                      'actions/upload-artifact and include-hidden-files: true (.mendwright is a '
                      'hidden folder).')
    assert world.summary() == '## mendwright\n\nNothing to write back.\n'


def test_an_artifact_without_a_store_lists_what_it_has(world):
    shutil.rmtree(str(world.uploaded))
    (world.uploaded / 'b').mkdir(parents=True)
    (world.uploaded / 'a.txt').write_text('x', encoding='utf-8')
    (world.uploaded / "it's.txt").write_text('x', encoding='utf-8')
    steps = world.run()
    assert steps['fix'][0] == 1 and 'ok' not in steps['fix'][2]
    assert notices(world, 'fix', 'error') == [
        "mendwright: the artifact mendwright-store has no overrides.json at its top or in "
        ".mendwright/ (it has: 'a.txt', 'b/', \"it's.txt\"). Upload the folder that holds the "
        "store, such as .mendwright/."]
    assert world.summary() == (
        "## mendwright\n\nStopped: mendwright: the artifact mendwright-store has no overrides.json "
        "at its top or in .mendwright/ (it has: `a.txt`, `b/`, `it's.txt`). Upload the folder that "
        "holds the store, such as .mendwright/.\n")


def test_the_summary_shows_the_artifact_s_entries_in_code_spans(world):
    shutil.rmtree(str(world.uploaded))
    world.uploaded.mkdir()
    (world.uploaded / '[x](https%3A__evil.example) @octocat.txt').write_text('x', encoding='utf-8')
    world.run()
    assert world.summary() == (
        '## mendwright\n\nStopped: mendwright: the artifact mendwright-store has no overrides.json '
        'at its top or in .mendwright/ (it has: `[x](https%3A__evil.example) @octocat.txt`). '
        'Upload the folder that holds the store, such as .mendwright/.\n')


def _can_link(tmp_path):
    try:
        (tmp_path / 'link-probe').symlink_to(tmp_path / 'nothing')
        return True
    except (OSError, NotImplementedError):
        return False


def test_a_store_that_is_a_link_is_not_read(world, tmp_path):
    if not _can_link(tmp_path):
        pytest.skip('this system cannot make symbolic links here')
    outside = tmp_path / 'outside.json'
    outside.write_text(json.dumps({'schema': 2, 'selectors': {}}), encoding='utf-8')
    store = world.uploaded / '.mendwright' / 'overrides.json'
    store.unlink()
    store.symlink_to(outside)
    world.run()
    assert notices(world, 'fix', 'error') == [
        'mendwright: the store or journal in the artifact is a link or points outside it, so it '
        'was not read.']


def test_a_store_whose_real_path_is_outside_the_artifact_is_not_read(world, tmp_path):
    winapi = pytest.importorskip('_winapi')
    if not hasattr(winapi, 'CreateJunction'):
        pytest.skip('directory junctions are Windows only')
    outside = tmp_path / 'outside'
    shutil.copytree(str(world.uploaded / '.mendwright'), str(outside))

    def junction(artifact):
        shutil.rmtree(str(artifact / '.mendwright'))
        winapi.CreateJunction(str(outside), str(artifact / '.mendwright'))

    world.after_download = junction
    try:
        world.run()
    finally:
        link = world.temp / 'artifact' / '.mendwright'
        if os.path.lexists(str(link)):
            os.rmdir(str(link))                 # removes the junction, not its target
    assert notices(world, 'fix', 'error') == [
        'mendwright: the store or journal in the artifact is a link or points outside it, so it '
        'was not read.']
    assert world.remote_sha('mendwright/fix/main') is None


def test_store_and_journal_over_the_size_limits_are_not_read(world):
    real = os.stat

    def big(path, size):
        def fake(p, *a, **k):
            result = real(p, *a, **k)
            if os.path.basename(str(p)) == path:
                return os.stat_result((result.st_mode, 0, 0, 0, 0, 0, size, 0, 0, 0))
            return result
        return fake

    world.stat = big('journal.jsonl', 600 * 1024 * 1024)
    world.run()
    assert notices(world, 'fix', 'error') == [
        "mendwright: journal.jsonl is 600 MiB, over the action's limit of 512 MiB, so it was not "
        "read. Move the old journal aside, or stop carrying it from run to run."]
    world.stat = big('overrides.json', 73 * 1024 * 1024)
    world.run()
    assert notices(world, 'fix', 'error') == [
        "mendwright: overrides.json is 73 MiB, over the action's limit of 50 MiB, so it was not "
        "read."]


def test_the_store_from_the_workspace(world):
    world.inputs.update(artifact='', store='store/overrides.json')
    world.run()
    assert world.result() == 'nothing'
    assert notices(world, 'fix') == ['mendwright: no store at .mendwright/store/overrides.json, so '
                                     'there is nothing to write back.']
    assert 'find-artifact' not in world.steps
    # A store in the workspace, outside git's view (.mendwright is ignored), works.
    world.new_commit('.gitignore', '.mendwright/\nstore/\n')
    folder = world.ws / 'store'
    folder.mkdir()
    shutil.copy(str(world.uploaded / '.mendwright' / 'overrides.json'), str(folder))
    shutil.copy(str(world.uploaded / '.mendwright' / 'journal.jsonl'), str(folder))
    world.run()
    assert world.result() == 'created'
    files = git(world.bare, 'diff', '--name-only', 'main', 'mendwright/fix/main').split()
    assert files == ['tests/test_checkout.py']


def test_without_a_journal_nothing_is_confirmed(world):
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox')}, [], journal=False)
    world.run()
    assert notices(world, 'fix', 'warning') == [
        'mendwright: there is no journal.jsonl next to the store, so no heal counts as confirmed '
        'and nothing is written back. Upload the journal too, not only overrides.json.']
    assert world.result() == 'nothing'
    assert world.outputs()['left-alone'] == '1'
    assert world.remote_sha('mendwright/fix/main') is None


# --- the fix step: what it runs ---------------------------------------------------------------

def test_apply_only_in_mode_fix_and_never_include_unconfirmed(world):
    world.inputs.update(**{'destructive-keys': 'pay\nrefund\n', 'globs': '*.ts\n*.robot',
                           'paths': 'tests\n.'})
    world.run()
    argv = world.actions['fix'].fix_argv
    assert '--apply' in argv
    assert not any('unconfirmed' in a or a.startswith('--lists') for a in argv)
    assert [a for a in argv if a.startswith(('--destructive-key', '--glob', '--path'))] == [
        '--path=tests', '--path=.', '--glob=*.ts', '--glob=*.robot', '--destructive-key=pay',
        '--destructive-key=refund']
    world.checkout()
    world.inputs['dry-run'] = 'true'
    world.run()
    assert '--apply' not in world.actions['fix'].fix_argv


def test_destructive_keys_are_never_written(world):
    world.inputs['destructive-keys'] = 'accept_terms'
    world.run()
    assert world.result() == 'nothing'
    assert world.remote_sha('mendwright/fix/main') is None


def test_a_path_outside_the_working_directory_is_refused(world):
    world.inputs['paths'] = '.\n../other'
    world.run()
    assert notices(world, 'fix', 'error') == [
        'mendwright: the path ../other is outside the working directory, and mendwright fix would '
        'write there. Pass paths inside your checkout.']


def test_errors_in_fix_put_the_files_back_and_stop(world, monkeypatch):
    world.new_commit('tests/test_other.py', 'X = "#search-box"\n')
    world.new_commit('tests/test_more.py', 'Y = "#accept-terms"\n')
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'search': heal('#search-box', SEARCH)},
                 confirmed('accept_terms', TERMS) + confirmed('search', SEARCH))
    from mendwright import fixback
    real = fixback._write_atomic

    def flaky(change):
        if change.shown.endswith('test_other.py'):
            raise OSError(13, 'Permission denied')
        real(change)

    monkeypatch.setattr(fixback, '_write_atomic', flaky)
    world.run()
    assert world.steps['fix'][0] == 1
    [error] = notices(world, 'fix', 'error')
    assert error == ('mendwright: mendwright fix could not finish (tests/test_other.py was not '
                     'changed: Permission denied. Close the file in other programs, or edit it by '
                     'hand, then run again.). The files it wrote were put back and nothing was '
                     'pushed. The other errors are above.')
    assert 'could not finish (`tests/test_other.py was not changed: ' in world.summary()
    assert git(world.ws, 'status', '--porcelain') == ''
    assert 'publish' not in world.steps


def _fix_fails_on_one_file(world, monkeypatch):
    world.new_commit('.gitignore', 'generated/\n')
    world.new_commit('tests/test_other.py', 'X = "#search-box"\n')
    world.write('generated/test_gen.py', 'G = "#accept-terms"\n')    # written, not tracked
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'search': heal('#search-box', SEARCH)},
                 confirmed('accept_terms', TERMS) + confirmed('search', SEARCH))
    from mendwright import fixback
    real = fixback._write_atomic

    def flaky(change):
        if change.shown.endswith('test_other.py'):
            raise OSError(13, 'Permission denied')
        real(change)

    monkeypatch.setattr(fixback, '_write_atomic', flaky)


FAILED_WRITE = ('mendwright: mendwright fix could not finish (tests/test_other.py was not changed: '
                'Permission denied. Close the file in other programs, or edit it by hand, then run '
                'again.). ')


def test_errors_in_fix_put_back_the_tracked_files_and_name_the_untracked_ones(world, monkeypatch):
    _fix_fails_on_one_file(world, monkeypatch)
    world.run()
    assert world.steps['fix'][0] == 1 and 'publish' not in world.steps
    assert notices(world, 'fix', 'error') == [
        FAILED_WRITE + "The tracked files it wrote were put back and nothing was pushed, but git "
        "does not track generated/test_gen.py, so it keeps mendwright fix's changes. The other "
        "errors are above."]
    assert git(world.ws, 'status', '--porcelain', '--untracked-files=no') == ''
    assert (world.ws / 'tests' / 'test_checkout.py').read_text(encoding='utf-8') == TEST_FILE
    assert world.summary() == (
        '## mendwright\n\nStopped: mendwright: mendwright fix could not finish (`tests/test_other.py '
        'was not changed: Permission denied. Close the file in other programs, or edit it by hand, '
        'then run again.`). The tracked files it wrote were put back and nothing was pushed, but '
        "git does not track `generated/test_gen.py`, so it keeps mendwright fix's changes. The "
        'other errors are above.\n')


def test_errors_in_fix_put_back_a_file_paths_names_in_another_case(world, monkeypatch):
    if not (world.ws / 'TESTS').exists():
        pytest.skip('this file system tells Tests from tests')
    _fix_fails_on_one_file(world, monkeypatch)
    world.inputs['paths'] = 'Tests'             # fix says Tests/..., git tests/...
    world.run()
    assert world.steps['fix'][0] == 1 and 'publish' not in world.steps
    assert notices(world, 'fix', 'error') == [
        FAILED_WRITE.replace('tests/test_other.py', 'Tests/test_other.py') + 'The files it wrote '
        'were put back and nothing was pushed. The other errors are above.']
    assert git(world.ws, 'status', '--porcelain', '--untracked-files=no') == ''


@pytest.mark.parametrize('folder', ['.github', '.github/scripts'])
def test_a_working_directory_inside_github_is_refused(world, folder):
    # mendwright fix counts only the part of a path below the folder it runs in, so run there
    # it would scan .github.
    world.new_commit('.github/scripts/test_x.py', 'X = "#accept-terms"\n')
    world.inputs['working-directory'] = folder
    world.run()
    assert world.steps['fix'][0] == 1 and 'publish' not in world.steps
    assert notices(world, 'fix', 'error') == [
        'mendwright: working-directory ' + folder + ' is inside .github/, which mendwright fix '
        'never changes. Pass the folder that holds your tests.']
    assert git(world.ws, 'status', '--porcelain') == ''


def test_a_put_back_that_git_refuses_is_not_called_done(world, monkeypatch):
    _fix_fails_on_one_file(world, monkeypatch)
    real = ma.Action.git

    def no_checkout(self, *args, **kwargs):
        if 'checkout' in args:
            return 1, '', 'error: unable to unlink old \'tests/test_checkout.py\': Permission denied\n'
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ma.Action, 'git', no_checkout)
    world.run()
    assert notices(world, 'fix', 'error') == [
        FAILED_WRITE + "Nothing was pushed, but git could not put back the files it wrote (error: "
        "unable to unlink old 'tests/test_checkout.py': Permission denied). The other errors are "
        "above."]
    assert "(`error: unable to unlink old 'tests/test_checkout.py': Permission denied`)" \
        in world.summary()


def test_the_fix_step_works_without_the_token(world):
    world.run(until='fix')
    act = world.actions['fix']
    assert 'MW_TOKEN' not in act.env
    assert act.token == '' and world.steps['fix'][0] == 0
    assert not world.steps['fix'][1].startswith('::add-mask::')


# --- publish: what goes into the commit ---------------------------------------------------------

def test_an_untracked_written_file_is_listed_and_left_out(world):
    world.new_commit('.gitignore', 'generated/\n')
    world.write('generated/test_gen.py', 'G = "#accept-terms"\n')
    world.run()
    assert world.result() == 'created'
    files = git(world.bare, 'diff', '--name-only', 'main', 'mendwright/fix/main').split()
    assert files == ['tests/test_checkout.py']
    body = world.api.pulls[12]['body']
    assert ('Written but not tracked by git, so not in the pull request: '
            '`generated/test_gen.py`.') in body
    assert 'Written but not tracked by git' in world.summary()


def test_only_an_untracked_file_written_is_nothing_to_push(world):
    world.new_commit('.gitignore', 'generated/\n')
    world.new_commit('tests/test_checkout.py', 'def test_x():\n    pass\n')
    world.write('generated/test_gen.py', 'G = "#accept-terms"\n')
    world.run()
    assert world.result() == 'nothing'
    assert world.remote_sha('mendwright/fix/main') is None
    assert 'Written but not tracked by git, so not in the pull request: `generated/test_gen.py`.' \
        in world.summary()


def test_paths_in_another_case_on_a_file_system_that_ignores_case(world):
    if not (world.ws / 'TESTS').exists():
        pytest.skip('this file system tells Tests from tests')
    world.new_commit('services/web/tests/test_a.py', TEST_FILE)
    world.inputs.update({'working-directory': 'Services/Web', 'paths': 'Tests'})
    world.run()
    assert world.result() == 'created', world.log()
    files = git(world.bare, 'diff', '--name-only', 'main', 'mendwright/fix/main').split()
    assert files == ['services/web/tests/test_a.py']
    assert 'Written but not tracked' not in world.api.pulls[12]['body']


def test_a_monorepo_working_directory(world):
    world.new_commit('services/web/tests/test_a.py', TEST_FILE)
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox')},
                 confirmed('accept_terms', TERMS))
    world.inputs['working-directory'] = 'services/web'
    world.run()
    assert world.result() == 'created', world.log()
    files = git(world.bare, 'diff', '--name-only', 'main', 'mendwright/fix/main').split()
    assert files == ['services/web/tests/test_a.py']
    body = world.api.pulls[12]['body']
    assert '`services/web/tests/test_a.py:2`' in body
    assert '`tests/test_checkout.py:2`' not in body


HOOKS = ('pre-commit', 'prepare-commit-msg', 'commit-msg', 'post-commit', 'post-checkout',
         'reference-transaction', 'pre-push', 'post-index-change')


@pytest.mark.parametrize('where', ['.git/hooks', 'core.hooksPath'])
def test_no_git_hook_runs(world, where):
    # --no-verify only skips pre-commit and commit-msg: switch, commit and push run the others
    # unless core.hooksPath points at an empty folder.
    folder = world.ws / '.git' / 'hooks' if where == '.git/hooks' else world.tmp / 'custom-hooks'
    folder.mkdir(exist_ok=True)
    for name in HOOKS:
        (folder / name).write_text('#!/bin/sh\necho ran > "' + str(world.tmp).replace('\\', '/')
                                   + '/hook-' + name + '"\n', encoding='utf-8')
        os.chmod(str(folder / name), 0o755)
    if where == 'core.hooksPath':
        git(world.ws, 'config', 'core.hooksPath', str(folder))
    world.run()
    assert world.result() == 'created'
    assert sorted(p.name for p in world.tmp.iterdir() if p.name.startswith('hook-')) == []
    # The hooks are real: a plain git commit runs them.
    git(world.ws, 'commit', '-q', '--allow-empty', '-m', 'by hand')
    assert (world.tmp / 'hook-pre-commit').exists() and (world.tmp / 'hook-post-commit').exists()


# --- publish: changes mendwright fix did not make -------------------------------------------------

def _changed_after_fix(world, name, text):
    def change():
        world.write(name, text)
    world.after_fix = change


def _nothing_pushed_and_all_put_back(world):
    assert world.steps['publish'][0] == 1
    assert world.remote_sha('mendwright/fix/main') is None
    assert world.api.writes() == []
    assert git(world.ws, 'status', '--porcelain', '--untracked-files=no') == ''
    assert (world.ws / 'tests' / 'test_checkout.py').read_text(encoding='utf-8') == TEST_FILE


def test_a_change_under_github_is_refused_and_put_back(world):
    world.new_commit('.github/workflows/ci.yml', 'run: echo "#accept-terms"\n')
    _changed_after_fix(world, '.github/workflows/ci.yml', "run: echo \"//input[@name='x']\"\n")
    world.run()
    assert notices(world, 'publish', 'error') == [
        'mendwright: mendwright fix changed .github/workflows/ci.yml, under .github/, which it '
        'never should. The files were put back and nothing was pushed; please report this.']
    assert 'changed `.github/workflows/ci.yml`, under .github/' in world.summary()
    _nothing_pushed_and_all_put_back(world)


def test_a_changed_file_fix_did_not_report_is_refused_and_put_back(world):
    world.new_commit('README.md', 'readme\n')
    _changed_after_fix(world, 'README.md', 'changed\n')
    world.run()
    assert notices(world, 'publish', 'error') == [
        'mendwright: git shows changes mendwright fix did not report (README.md). The files were '
        'put back and nothing was pushed; please report this.']
    assert 'did not report (`README.md`)' in world.summary()
    _nothing_pushed_and_all_put_back(world)


def test_a_put_back_that_git_refuses_in_publish_is_not_called_done(world, monkeypatch):
    world.new_commit('README.md', 'readme\n')
    _changed_after_fix(world, 'README.md', 'changed\n')
    real = ma.Action.git

    def no_checkout(self, *args, **kwargs):
        if 'checkout' in args:
            return 1, '', "error: unable to unlink old 'README.md': Permission denied\n"
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ma.Action, 'git', no_checkout)
    world.run()
    assert notices(world, 'publish', 'error') == [
        "mendwright: git shows changes mendwright fix did not report (README.md). Nothing was "
        "pushed, but git could not put the files back (error: unable to unlink old 'README.md': "
        "Permission denied); please report this."]
    assert world.remote_sha('mendwright/fix/main') is None and world.api.writes() == []
    assert ("did not report (`README.md`). Nothing was pushed, but git could not put the files back "
            "(`error: unable to unlink old 'README.md': Permission denied`)") in world.summary()


def test_a_staged_file_fix_did_not_write_is_refused_and_put_back(world, monkeypatch):
    real = ma.Action.git

    def one_more_staged(self, *args, **kwargs):
        code, out, err = real(self, *args, **kwargs)
        if 'diff' in args and '--cached' in args:
            out += 'tests/extra.py\0'
        return code, out, err

    monkeypatch.setattr(ma.Action, 'git', one_more_staged)
    world.run()
    assert notices(world, 'publish', 'error') == [
        'mendwright: git shows changes mendwright fix did not report (tests/extra.py). The files '
        'were put back and nothing was pushed; please report this.']
    assert 'did not report (`tests/extra.py`)' in world.summary()
    _nothing_pushed_and_all_put_back(world)


@pytest.mark.parametrize('text', [
    'a‮b', '⁦x⁩', '‏', '؜', 'c\x1bd', 'e\x85f', ' ', 'k\ud800',
    'a`b``c', '`', ' x ', '', 'a|b', 'tab\there',
])
def test_the_script_s_code_spans_are_mendwright_s(text):
    from mendwright import _md
    assert ma.md_code(text) == _md.code(text, cut=None)


def test_the_closing_comment_writes_out_a_bidirectional_control_in_a_key(world):
    key = 'accept‮terms'
    world.write('tests/test_checkout.py', 'def test_signup(heal):\n    heal.locator('
                + ascii(key) + ', "#accept-terms").check()\n')
    world.env['GITHUB_SHA'] = world.commit('a key with a bidirectional control')
    git(world.ws, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    world.upload({key: heal('#accept-terms', TERMS, role='checkbox')}, confirmed(key, TERMS))
    world.run()
    assert world.result() == 'created', world.log()
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    entry = heal('#accept-terms', TERMS, role='checkbox')
    entry['candidates'][0]['lastSucceededAt'] = '2026-10-05T00:00:00+00:00'
    world.upload({key: entry}, confirmed(key, TERMS))
    world.run()
    assert world.result() == 'closed'
    [(_, comment)] = world.api.comments
    assert comment.splitlines()[2] == '- `accept\\u202Eterms`: the selector in your code works again.'
    assert '‮' not in comment


# --- publish: the same changes twice ------------------------------------------------------------

def test_a_second_run_with_the_same_store_pushes_nothing_and_patches_nothing(world):
    world.run()
    first = world.remote_sha('mendwright/fix/main')
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.api.calls.clear()
    world.run()
    assert world.result() == 'unchanged'
    assert world.remote_sha('mendwright/fix/main') == first
    assert world.api.writes() == []
    assert world.summary().startswith('## mendwright\n\n#12 is up to date.\n')
    assert world.steps['publish'][1].rstrip().endswith('mendwright: #12 is up to date.')


def test_a_changed_left_alone_patches_the_body_only(world):
    world.run()
    first = world.remote_sha('mendwright/fix/main')
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'email': heal('#email', "//input[@name='e-mail']")},
                 confirmed('accept_terms', TERMS))
    world.api.calls.clear()
    world.run()
    assert world.result() == 'updated'
    assert world.remote_sha('mendwright/fix/main') == first
    assert world.api.writes() == [('PATCH', '/repos/saadbuilt/shop/pulls/12')]
    assert '- `email`: its heal was never confirmed.' in world.api.pulls[12]['body']
    assert pull_marker(world.api.pulls[12])['run'] == 101


def test_a_changed_plan_pushes_with_the_lease_and_patches(world):
    world.run()
    first = world.remote_sha('mendwright/fix/main')
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'search': heal('#search-box', SEARCH)},
                 confirmed('accept_terms', TERMS) + confirmed('search', SEARCH))
    world.api.calls.clear()
    world.run()
    assert world.result() == 'updated'
    second = world.remote_sha('mendwright/fix/main')
    assert second != first
    assert git(world.bare, 'rev-list', '--count', 'main..mendwright/fix/main') == '1'
    push = [c for c in world.actions['publish'].git_calls if 'push' in c['argv']]
    assert push and '--force-with-lease=refs/heads/mendwright/fix/main:' + first in push[0]['argv']
    assert world.api.writes() == [('PATCH', '/repos/saadbuilt/shop/pulls/12')]
    assert world.api.pulls[12]['title'] == 'mendwright fix: write back 2 healed selectors'
    assert pull_marker(world.api.pulls[12])['keys'] == ['accept_terms', 'search']


def test_a_conflict_with_the_same_plan_is_rebuilt_only_on_a_newer_commit(world):
    world.run()
    first = world.remote_sha('mendwright/fix/main')
    world.api.pulls[12]['mergeable'] = False
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.run()
    assert world.result() == 'unchanged'
    assert world.remote_sha('mendwright/fix/main') == first
    world.new_commit('README.md', 'later\n')
    world.env['GITHUB_RUN_ID'] = '102'
    world.run()
    assert world.result() == 'updated'
    rebuilt = world.remote_sha('mendwright/fix/main')
    assert rebuilt != first
    assert git(world.bare, 'rev-parse', rebuilt + '^') == world.env['GITHUB_SHA']
    assert pull_marker(world.api.pulls[12])['sha'] == world.env['GITHUB_SHA']


def test_an_older_run_never_overwrites_a_newer_one(world):
    world.env['GITHUB_RUN_ID'] = '200'
    world.run()
    first = world.remote_sha('mendwright/fix/main')
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '150'
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'search': heal('#search-box', SEARCH)},
                 confirmed('accept_terms', TERMS) + confirmed('search', SEARCH))
    world.api.calls.clear()
    world.run()
    assert world.result() == 'superseded'
    assert world.api.writes() == []
    assert world.remote_sha('mendwright/fix/main') == first
    assert notices(world, 'publish') == [
        'mendwright: #12 was last updated by run 200, which is newer than this run (150), so it '
        'was left as it is.']
    assert world.summary().startswith('## mendwright\n\n#12 was last updated by a newer run, so it '
                                      'was left as it is.\n')


# --- publish: a branch with someone else's commits ----------------------------------------------

def _push_on_branch(world, author='Ada Lovelace <ada@example.com>', message='tweak'):
    other = world.tmp / 'other'
    if other.exists():
        shutil.rmtree(str(other))
    git(world.tmp, 'clone', '-q', str(world.bare), str(other))
    git(other, 'checkout', '-q', 'mendwright/fix/main')
    world.write('tests/extra.py', 'x = 1\n', root=other)
    git(other, 'add', '-A')
    name, email = author[:-1].split(' <')
    git(other, '-c', 'user.name=' + name, '-c', 'user.email=' + email, 'commit', '-q', '-m', message)
    git(other, 'push', '-q', 'origin', 'HEAD:refs/heads/mendwright/fix/main')
    return git(other, 'rev-parse', 'HEAD')


def _changed_plan(world, run_id='101'):
    world.checkout()
    world.env['GITHUB_RUN_ID'] = run_id
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'search': heal('#search-box', SEARCH)},
                 confirmed('accept_terms', TERMS) + confirmed('search', SEARCH))


def test_a_commit_by_someone_else_blocks_the_branch(world):
    world.run()
    theirs = _push_on_branch(world)
    _changed_plan(world)
    world.run()
    assert world.result() == 'blocked'
    assert world.remote_sha('mendwright/fix/main') == theirs
    assert notices(world, 'publish', 'warning') == [
        'mendwright: mendwright/fix/main has commits that this action did not make (' + theirs[:7]
        + ' by Ada Lovelace), so it was not updated. Merge or close #12, or delete the branch, and '
        'the next run starts it again.']
    assert world.summary().startswith('## mendwright\n\nDid not update mendwright/fix/main: it has '
                                      'commits this action did not make.\n')


def test_an_author_from_the_job_s_environment_or_git_config_does_not_block_the_action(world):
    # A workflow that sets GIT_AUTHOR_* for its own commits, or author.email in git's config,
    # must not put that author on the action's commit: the next run would block itself.
    world.env.update(GIT_AUTHOR_NAME='CI', GIT_AUTHOR_EMAIL='ci@example.com',
                     GIT_COMMITTER_NAME='CI', GIT_COMMITTER_EMAIL='ci@example.com')
    for role in ('author', 'committer'):
        git(world.ws, 'config', role + '.name', 'Config')
        git(world.ws, 'config', role + '.email', 'config@example.com')
    world.run()
    assert world.result() == 'created'
    who = git(world.bare, 'log', '-1', '--format=%an|%ae|%cn|%ce', 'mendwright/fix/main')
    assert who == '|'.join(['github-actions[bot]', BOT_EMAIL] * 2)
    _changed_plan(world)
    world.run()
    assert world.result() == 'updated', world.log()
    assert notices(world, 'publish', 'warning') == []


def test_an_update_branch_merge_commit_blocks_the_branch(world):
    world.run()
    # GitHub's "Update branch" merges the base into the branch, as the person who clicked.
    world.new_commit('README.md', 'base moved\n')
    other = world.tmp / 'other'
    git(world.tmp, 'clone', '-q', str(world.bare), str(other))
    git(other, 'checkout', '-q', 'mendwright/fix/main')
    git(other, '-c', 'user.name=Grace', '-c', 'user.email=grace@example.com', 'merge', '-q',
        '--no-edit', 'origin/main')
    git(other, 'push', '-q', 'origin', 'HEAD:refs/heads/mendwright/fix/main')
    _changed_plan(world, '102')
    world.run()
    assert world.result() == 'blocked'


def test_more_than_250_commits_block_the_branch(world):
    world.run()
    _changed_plan(world)
    world.api.queue.append(('GET', '/repos/saadbuilt/shop/compare/', 200,
                            {'total_commits': 251, 'commits': []}, None))
    world.run()
    assert world.result() == 'blocked'


# --- publish: closed pull requests --------------------------------------------------------------

def test_a_pull_request_a_person_closed_is_not_opened_again(world):
    world.run()
    world.api.pulls[12]['state'] = 'closed'
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.api.calls.clear()
    world.run()
    assert world.result() == 'kept-closed'
    assert world.api.writes() == []
    assert notices(world, 'publish') == [
        'mendwright: #12 had exactly these changes and was closed without merging, so the action '
        'did not open it again. Reopen #12 to bring it back. A change in what would be written '
        'opens a new pull request.']
    _changed_plan(world, '102')
    world.run()
    assert world.result() == 'created'
    assert sorted(world.api.pulls) == [12, 13]


def test_a_pull_request_the_action_closed_is_opened_again_when_the_heal_comes_back(world):
    world.run()
    pull = world.api.pulls[12]
    pull['state'] = 'closed'
    pull['body'] = ma._MARKER.sub(lambda m: ma.write_marker(dict(pull_marker(pull),
                                                                state='closed-stale')), pull['body'])
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.run()
    assert world.result() == 'created'
    assert sorted(world.api.pulls) == [12, 13]


# --- publish: closing a pull request that is no longer needed -----------------------------------

def _store_without_the_heal(world, run_id='101', source='code'):
    world.checkout()
    world.env['GITHUB_RUN_ID'] = run_id
    entry = heal('#accept-terms', TERMS, role='checkbox')
    entry['candidates'][0]['lastSucceededAt'] = '2026-10-05T00:00:00+00:00'
    entry['candidates'][0]['source'] = source
    world.upload({'accept_terms': entry}, confirmed('accept_terms', TERMS))


def test_a_pull_request_whose_changes_are_no_longer_needed_is_closed(world):
    world.run()
    _store_without_the_heal(world)
    world.api.calls.clear()
    world.run()
    assert world.result() == 'closed'
    assert world.api.writes() == [('PATCH', '/repos/saadbuilt/shop/pulls/12'),
                                  ('POST', '/repos/saadbuilt/shop/issues/12/comments'),
                                  ('PATCH', '/repos/saadbuilt/shop/pulls/12')]
    pull = world.api.pulls[12]
    assert pull['state'] == 'closed' and pull_marker(pull)['state'] == 'closed-stale'
    [(number, comment)] = world.api.comments
    assert number == 12
    assert comment.splitlines() == [
        'mendwright closed this pull request: run [#101](' + world.env['GITHUB_SERVER_URL'] + '/'
        + REPO + '/actions/runs/101) shows that none of its selectors needs writing back.', '',
        '- `accept_terms`: the selector in your code works again.']
    assert world.summary().startswith('## mendwright\n\nClosed #12: none of its selectors needs '
                                      'writing back.\n')


def test_the_closing_comment_names_a_seed_selector_and_a_left_alone_code(world):
    world.upload({'accept_terms': heal('#accept-terms', TERMS, role='checkbox'),
                  'search': heal('#search-box', SEARCH)},
                 confirmed('accept_terms', TERMS) + confirmed('search', SEARCH))
    world.run()
    _store_without_the_heal(world, source='seed')
    with_search = json.loads((world.uploaded / '.mendwright' / 'overrides.json').read_text())
    with_search['selectors']['search'] = dict(heal('#search-box', SEARCH), destructive=True)
    (world.uploaded / '.mendwright' / 'overrides.json').write_text(json.dumps(with_search))
    world.run()
    assert world.result() == 'closed'
    assert world.api.comments[0][1].splitlines()[2:] == [
        '- `accept_terms`: a selector from your seed file works again.',
        '- `search`: is marked destructive, so its heals are never written.']


@pytest.mark.parametrize('change, line', [
    ('tests-failure', '#12 is still open: the tests did not pass in this run, and close-stale '
                      'closes a pull request only after a run whose tests passed.'),
    ('tests-empty', '#12 is still open: the tests did not pass in this run, and close-stale '
                    'closes a pull request only after a run whose tests passed.'),
    ('key-missing', '#12 is still open: the store from this run does not account for '
                    '`accept_terms`, so mendwright cannot tell whether it is still needed.'),
    ('more', '#12 is still open: its description does not list all of its keys, so mendwright '
             'cannot tell whether it is still needed.'),
    ('close-stale-false', '#12 is still open: close-stale is false.'),
])
def test_a_pull_request_is_left_open_when_mendwright_cannot_tell(world, change, line):
    world.run()
    _store_without_the_heal(world)
    if change == 'tests-failure':
        world.inputs['tests-result'] = 'failure'
    elif change == 'tests-empty':
        world.inputs['tests-result'] = ''
    elif change == 'key-missing':
        world.upload({}, [])
    elif change == 'more':
        pull = world.api.pulls[12]
        pull['body'] = ma._MARKER.sub(lambda m: ma.write_marker(dict(pull_marker(pull), more=True)),
                                      pull['body'])
    else:
        world.inputs['close-stale'] = 'false'
    world.api.calls.clear()
    world.run()
    assert world.result() == 'unchanged'
    assert world.api.writes() == [] and world.api.pulls[12]['state'] == 'open'
    assert world.summary().startswith('## mendwright\n\n#12 is up to date.\n\n' + line + '\n')


EVIL_KEY = '[click me](https://evil.example/x) <img src=https://evil.example/i.png> @octocat'


@pytest.mark.parametrize('key, shown', [
    (EVIL_KEY, '`' + EVIL_KEY + '`'),
    ('k\ud800', '`k\\uD800`'),
    ('a‮b', '`a\\u202Eb`'),
], ids=['markdown', 'lone-surrogate', 'bidi'])
def test_a_key_the_store_no_longer_has_is_in_a_code_span_in_the_summary(world, key, shown):
    world.write('tests/test_checkout.py', 'def test_signup(heal):\n    heal.locator('
                + ascii(key) + ', "#accept-terms").check()\n')
    world.env['GITHUB_SHA'] = world.commit('a key with markup in it')
    git(world.ws, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    world.upload({key: heal('#accept-terms', TERMS, role='checkbox')}, confirmed(key, TERMS))
    world.run()
    assert world.result() == 'created', world.log()
    world.checkout()
    world.env['GITHUB_RUN_ID'] = '101'
    world.upload({}, [])
    world.run()
    assert (world.result(), world.steps['publish'][0]) == ('unchanged', 0)
    assert ('\n#12 is still open: the store from this run does not account for ' + shown
            + ', so mendwright cannot tell whether it is still needed.\n') in world.summary()


def test_text_with_a_lone_surrogate_is_written_to_the_log_outputs_and_summary(world):
    out = io.StringIO()
    act = ma.Action(world.step_env('publish'), stdout=out, transport=world.api)
    act.print('a\udc80b')
    act.set_output('x', 'c\ud800d')
    act.summary('e\udfffef')
    assert out.getvalue() == 'a\\uDC80b\n'
    assert act.outputs['x'] == 'c\\uD800d'
    assert (world.tmp / 'summary.md').read_text(encoding='utf-8') == 'e\\uDFFFef\n'
    assert 'c\\uD800d' in (world.tmp / 'output.txt').read_text(encoding='utf-8')


# --- publish: races and refusals ----------------------------------------------------------------

def test_a_branch_that_moved_after_ls_remote_is_not_pushed_over(world, monkeypatch):
    world.run()
    _changed_plan(world)
    real = ma.Publish.remote_sha

    def then_someone_pushes(self):
        sha = real(self)
        other = world.tmp / 'racer'
        git(world.tmp, 'clone', '-q', str(world.bare), str(other))
        git(other, 'checkout', '-q', 'mendwright/fix/main')
        git(other, '-c', 'user.email=' + BOT_EMAIL, 'commit', '-q', '--allow-empty', '-m',
            'mendwright fix: write back 1 healed selector')
        git(other, 'push', '-q', 'origin', 'HEAD:refs/heads/mendwright/fix/main')
        return sha

    monkeypatch.setattr(ma.Publish, 'remote_sha', then_someone_pushes)
    world.run()
    assert world.steps['publish'][0] == 1
    assert notices(world, 'publish', 'error') == [
        'mendwright: mendwright/fix/main changed while this run worked on it, so nothing was '
        'pushed. The next run picks it up.']


def test_two_runs_at_once_update_the_pull_request_the_other_opened(world):
    real = world.api.route

    def first_post_finds_one(method, path, data):
        if method == 'POST' and path.endswith('/pulls') and not world.api.pulls:
            world.api.open_pull(body='<!-- mendwright-fix {"v":1} -->')
        return real(method, path, data)

    world.api.route = first_post_finds_one
    world.run()
    assert world.result() == 'updated'
    assert list(world.api.pulls) == [12]
    assert pull_marker(world.api.pulls[12])['keys'] == ['accept_terms']


def test_actions_not_allowed_to_open_pull_requests(world):
    world.api.queue.append(('POST', '/repos/saadbuilt/shop/pulls', 403, {
        'message': 'GitHub Actions is not permitted to create or approve pull requests.'}, None))
    world.run()
    assert world.steps['publish'][0] == 1
    assert notices(world, 'publish', 'error') == [
        'mendwright: GitHub refused to open the pull request: Actions may not create pull requests '
        'in this repository. Turn on Settings, Actions, General, Workflow permissions, "Allow '
        'GitHub Actions to create and approve pull requests", or pass a GitHub App token as token. '
        'The branch mendwright/fix/main is pushed, so you can also open the pull request by hand.']
    assert world.remote_sha('mendwright/fix/main')


@pytest.mark.parametrize('status, message', [
    (403, 'mendwright: GitHub refused to open the pull request (HTTP 403: Resource not accessible '
          'by integration). Give the job permissions: pull-requests: write.'),
    (422, 'mendwright: GitHub refused the pull request (HTTP 422: Validation Failed).'),
])
def test_other_refusals_of_the_pull_request(world, status, message):
    answer = {'message': 'Resource not accessible by integration' if status == 403
              else 'Validation Failed'}
    world.api.queue.append(('POST', '/repos/saadbuilt/shop/pulls', status, answer, None))
    world.run()
    assert notices(world, 'publish', 'error') == [message]


def test_a_refused_push_quotes_git_with_the_token_masked(world, monkeypatch):
    real = ma.Action.git

    def refuse_push(self, *args, **kwargs):
        if 'push' in args:
            self.git_calls.append({'argv': list(args), 'env': dict(kwargs.get('extra_env') or {})})
            return 1, '', ('warning: unable to access a config file\n'
                           'remote: Permission to saadbuilt/shop.git denied to github-actions[bot] '
                           '(' + AUTH + ', ' + TOKEN + ').\n'
                           'fatal: unable to access the repository: The requested URL returned '
                           'error: 403\n')
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ma.Action, 'git', refuse_push)
    world.run()
    assert world.steps['publish'][0] == 1
    assert notices(world, 'publish', 'error') == [
        'mendwright: git push to mendwright/fix/main was refused (remote: Permission to '
        'saadbuilt/shop.git denied to github-actions[bot] (***, ***).). Give the job permissions: '
        'contents: write, or pass a token that can push. A ruleset that protects mendwright/** or '
        'asks for signed commits also refuses it.']
    assert TOKEN not in world.log() and AUTH not in world.summary()


def test_a_secondary_rate_limit_is_waited_out_once(world):
    world.api.queue.append(('POST', '/repos/saadbuilt/shop/pulls', 403,
                            {'message': 'You have exceeded a secondary rate limit.'},
                            {'retry-after': '7'}))
    world.run()
    assert world.result() == 'created' and world.sleeps == [7]
    world.checkout()
    _changed_plan(world, '102')
    for _ in range(2):
        world.api.queue.append(('PATCH', '/repos/saadbuilt/shop/pulls/12', 429, {'message': 'slow'},
                                {'retry-after': '3'}))
    world.run()
    assert notices(world, 'publish', 'error') == [
        "mendwright: GitHub's rate limit stopped the action (HTTP 429, retry after 3 s). The branch "
        'mendwright/fix/main is pushed; the pull request was not opened.']


def test_a_redirect_is_not_followed(world):
    world.api.queue.append(('GET', '/repos/saadbuilt/shop/pulls', 301, None,
                            {'location': 'https://api.github.test/repositories/1/pulls'}))
    world.run()
    assert notices(world, 'publish', 'error') == [
        'mendwright: GitHub answered HTTP 301 for /repos/saadbuilt/shop/pulls, a redirect, which '
        'the action does not follow. Was the repository renamed?']


def test_an_unreachable_api(world):
    world.api.down = True
    world.run()
    assert notices(world, 'find-artifact', 'error') == [
        'mendwright: could not reach the GitHub API at https://api.github.test (timed out after '
        '30 s).']


# --- secrets ------------------------------------------------------------------------------------

def test_the_token_reaches_git_only_through_the_environment(world):
    decoy = world.tmp / 'decoy.git'
    git(world.tmp, 'init', '-q', '--bare', str(decoy))
    git(world.ws, 'remote', 'set-url', 'origin', str(decoy))
    for name in ('GIT_TRACE', 'GIT_TRACE_PACKET', 'GIT_CURL_VERBOSE', 'GIT_ASKPASS', 'SSH_ASKPASS'):
        world.env[name] = '1'
    world.run()
    assert world.result() == 'created'
    assert world.remote_sha('mendwright/fix/main')
    assert git(decoy, 'branch', '--list') == ''               # nothing went to origin
    log = world.log()
    lines = log.splitlines()
    masks = [i for i, line in enumerate(lines) if line.startswith('::add-mask::')]
    assert masks and lines[masks[0]] == '::add-mask::' + AUTH
    assert TOKEN not in log
    assert AUTH not in '\n'.join(line for line in lines if not line.startswith('::add-mask::'))
    config = (world.ws / '.git' / 'config').read_text(encoding='utf-8')
    assert TOKEN not in config and AUTH not in config and 'extraheader' not in config
    publish = world.actions['publish']
    for call in publish.git_calls:
        assert not any(TOKEN in a or AUTH in a for a in call['argv']), call['argv']
    env = publish.git_env(publish.auth_env())
    for name in ('GIT_TRACE', 'GIT_TRACE_PACKET', 'GIT_CURL_VERBOSE', 'GIT_ASKPASS', 'SSH_ASKPASS',
                 'MW_TOKEN'):
        assert name not in env
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    key = 'http.' + world.env['GITHUB_SERVER_URL'] + '/.extraheader'
    assert (env['GIT_CONFIG_COUNT'], env['GIT_CONFIG_KEY_0'], env['GIT_CONFIG_VALUE_0'],
            env['GIT_CONFIG_KEY_1'], env['GIT_CONFIG_VALUE_1']) == (
        '2', key, '', key, 'AUTHORIZATION: basic ' + AUTH)
    remote = [c for c in publish.git_calls if 'push' in c['argv'] or 'ls-remote' in c['argv']]
    assert len(remote) == 2
    for call in remote:
        assert world.env['GITHUB_SERVER_URL'] + '/' + REPO + '.git' in call['argv']
        assert call['env']['GIT_CONFIG_VALUE_1'] == 'AUTHORIZATION: basic ' + AUTH
    others = [c for c in publish.git_calls if c not in remote]
    assert all('GIT_CONFIG_COUNT' not in c['env'] for c in others)


# --- injection ----------------------------------------------------------------------------------

def test_store_text_cannot_run_a_workflow_command_or_a_shell(world):
    key = '::set-output name=x::y'
    old = '`$(touch pwned)`'
    new = "//a[@x='$(touch pwned)']"
    world.new_commit('tests/test_hostile.py', 'heal.locator("' + key + '", "' + old + '")\n')
    world.upload({key: heal(old, new)}, confirmed(key, new))
    world.run()
    assert world.result() == 'created'
    assert world.api.pulls[12]['title'] == 'mendwright fix: write back 1 healed selector'
    assert not (world.tmp / 'pwned').exists() and not (world.ws / 'pwned').exists()
    lines = world.log().splitlines()
    inside = None
    for line in lines:
        if line.startswith('::stop-commands::'):
            inside = line[len('::stop-commands::'):]
            continue
        if inside and line == '::' + inside + '::':
            inside = None
            continue
        if inside is None:
            assert 'set-output' not in line and 'touch pwned' not in line, line
    assert inside is None


# --- the body -----------------------------------------------------------------------------------

def test_a_big_store_keeps_the_body_under_github_s_limit(world):
    keys = ['key_%04d_-->' % i for i in range(3000)]
    code = ''.join('heal.locator("%s", "#old-%04d")\n' % (k, i) for i, k in enumerate(keys))
    world.new_commit('tests/test_many.py', code)
    selectors, events = {}, []
    for i, k in enumerate(keys):
        new = "//button[@id='new-%04d']" % i
        selectors[k] = heal('#old-%04d' % i, new, role='button')
        events += confirmed(k, new)
    world.upload(selectors, events)
    world.run()
    assert world.result() == 'created', world.log()[-2000:]
    body = world.api.pulls[12]['body']
    assert len(body) <= 65536
    marker = pull_marker(world.api.pulls[12])
    assert marker['more'] is True and 0 < len(marker['keys']) < 3000
    assert marker['keys'] == keys[:len(marker['keys'])]
    comment = body[body.rindex('<!-- mendwright-fix '):]
    assert comment.count('-->') == 1 and comment.rstrip().endswith('-->')
    assert len(comment) < 8100


def test_the_marker_round_trips_and_no_key_ends_the_comment():
    fields = {'v': 1, 'plan': 'a' * 64, 'body': 'b' * 64, 'run': 5, 'sha': 'c' * 40,
              'state': 'open', 'keys': ['x-->', '<!-- y', '--><script>'], 'more': False}
    text = ma.write_marker(fields)
    assert text.count('-->') == 1 and text.endswith(' -->')
    assert '<script>' not in text
    assert ma.read_marker('before\n' + text + '\n') == fields
    assert ma.read_marker('no marker') is None
    assert ma.read_marker('<!-- mendwright-fix {not json} -->') is None


# --- what publish imports ------------------------------------------------------------------------

def test_guard_find_artifact_and_publish_import_neither_mendwright_nor_lxml(world, tmp_path):
    """Run the script by itself, with python -I, from a folder where a mendwright and an lxml
    package raise when imported; the script's own folder is first on sys.path. The API is the
    fake, served on 127.0.0.1, so the script's own urllib transport is used too."""
    if not ma.PYTHONS[0] <= tuple(sys.version_info[:2]) <= ma.PYTHONS[1]:
        pytest.skip('the action refuses this Python, and this test runs its steps as they are')
    import http.server
    import threading

    folder = tmp_path / 'isolated'
    folder.mkdir()
    shutil.copy(str(SCRIPT), str(folder / 'mendwright_action.py'))
    for name in ('mendwright', 'lxml'):
        (folder / name).mkdir()
        (folder / name / '__init__.py').write_text('raise ImportError("imported ' + name + '")\n')
    api = world.api

    class Handler(http.server.BaseHTTPRequestHandler):
        def answer(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else None
            status, extra, raw = api(self.command, API + self.path, dict(self.headers.items()),
                                     body, 30)
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PATCH = answer

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    world.run(until='guard')          # writes the event payload
    served = 'http://127.0.0.1:' + str(server.server_address[1])

    def isolated(step, **extra):
        env = dict(world.step_env(step, extra), GITHUB_API_URL=served)
        (world.tmp / 'output.txt').write_text('', encoding='utf-8')
        done = subprocess.run([sys.executable, '-I', str(folder / 'mendwright_action.py'), step],
                              env=env, cwd=str(world.ws), capture_output=True, text=True)
        assert done.returncode == 0, (step, done.stdout, done.stderr)
        assert 'imported' not in done.stderr
        text = (world.tmp / 'output.txt').read_text(encoding='utf-8')
        pairs = re.findall(r'^([\w-]+)<<(mw_\w+)\n(.*?)\n\2$', text, re.M | re.S)
        return {name: value for name, _, value in pairs}

    try:
        guard = isolated('guard')
        later = {'MW_MODE': guard['mode'], 'MW_BASE': guard['base'], 'MW_SHA': guard['sha'],
                 'MW_ARTIFACT_RUN_ID': guard['artifact-run-id']}
        found = isolated('find-artifact', **later)
        assert found == {'found': 'true'}
        shutil.copytree(str(world.uploaded), str(world.temp / 'artifact'))
        act, _ = world.action('fix', world.step_env('fix', dict(later, MW_FOUND='true')))
        assert ma.main(['fix'], act) == 0          # this step imports mendwright, in-process
        published = isolated('publish', **later)
    finally:
        server.shutdown()
    assert published['result'] == 'created' and published['pull-request-number'] == '12'
    assert world.remote_sha('mendwright/fix/main')


def test_the_plan_hash_is_the_one_mendwright_fix_computes():
    from mendwright import fixback
    rows = [{'path': 'tests/a.py', 'selector': '#a', 'new': '//a', 'keys': ['k', 'j'], 'line': 3},
            {'path': 'tests/b.py', 'selector': "#b'", 'new': '//b[@x="1"]', 'keys': ['é'], 'line': 1}]
    assert ma.plan_hash(rows) == fixback.plan_hash(rows)
    assert ma.plan_hash([]) == fixback.plan_hash([])


def test_labels_that_cannot_be_added_are_a_warning(world):
    world.inputs['labels'] = 'mendwright'
    world.api.queue.append(('POST', '/repos/saadbuilt/shop/issues/12/labels', 422,
                            {'message': 'Validation Failed'}, None))
    world.run()
    assert world.result() == 'created'
    assert notices(world, 'publish', 'warning') == [
        'mendwright: could not add the labels to #12 (HTTP 422: Validation Failed). The pull '
        'request is open.']


def test_with_two_open_pull_requests_the_lowest_is_updated(world):
    world.run()
    world.api.open_pull(number=20, body=world.api.pulls[12]['body'])
    _changed_plan(world)
    world.run()
    assert world.result() == 'updated'
    assert world.outputs()['pull-request-number'] == '12'
    assert notices(world, 'publish', 'warning') == [
        'mendwright: 2 pull requests are open from mendwright/fix/main; the action updates #12. '
        'Close the others.']


def test_the_ci_fixture_gives_the_outputs_the_ci_job_checks(world):
    """.github/workflows/ci.yml runs the action as a dry run on tests/fixtures/action/project
    and checks result, written and left-alone."""
    fixture = ROOT / 'tests' / 'fixtures' / 'action' / 'project'
    target = world.ws / 'tests' / 'fixtures' / 'action' / 'project'
    shutil.copytree(str(fixture), str(target))
    world.commit('fixture')
    git(world.ws, 'push', '-q', 'origin', 'HEAD:refs/heads/main')
    world.env['GITHUB_SHA'] = world.head()
    world.inputs.update(**{'artifact': '', 'working-directory': 'tests/fixtures/action/project',
                           'store': 'store/overrides.json', 'dry-run': 'true'})
    world.run()
    out = world.outputs()
    assert (world.result(), out['written'], out['left-alone']) == ('dry-run', '1', '2')
    assert git(world.ws, 'status', '--porcelain') == ''
    assert world.api.calls == []
