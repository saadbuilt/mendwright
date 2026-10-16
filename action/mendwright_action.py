"""The steps of mendwright's GitHub Action (action.yml, at the root of the repository).

    mendwright_action.py guard          which run this is, and whether the action goes on
    mendwright_action.py find-artifact  whether the run that tested the code uploaded a store
    mendwright_action.py fix            mendwright fix --format json on the tested commit
    mendwright_action.py publish        commit, push, and open or update the pull request

Standard library only. guard, find-artifact and publish never import mendwright or lxml.
fix imports mendwright from the action's own folder ($GITHUB_ACTION_PATH, appended last to
sys.path), so the action runs the mendwright of its own tag. Each step reads its inputs
from its own environment (MW_IN_*, the outputs of earlier steps as MW_*), which action.yml
fills, and writes its outputs to $GITHUB_OUTPUT. Only guard, find-artifact and publish get
the token, as MW_TOKEN.

Nothing runs a shell: git gets argument lists, with hooks and the credential prompts off.
The token reaches git only through GIT_CONFIG_* variables, and is masked first. Text that
comes from the store, the journal, the artifact or your source files is printed between
::stop-commands:: markers, so it cannot act as a workflow command, and the workflow
commands this script issues have their text escaped.

The script also runs on an older Python, so that guard can say which version it needs.
"""

import base64
import hashlib
import io
import json
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

PYTHONS = ((3, 10), (3, 14))          # the versions lxml publishes wheels for
TRUSTED_EVENTS = ('push', 'schedule', 'workflow_dispatch')
TEST_RESULTS = ('success', 'failure', 'cancelled', 'skipped', 'neutral', 'timed_out',
                'action_required', 'stale', 'startup_failure')
BOOLEAN_INPUTS = ('dry-run', 'only-default-branch', 'draft', 'close-stale')
MAX_STORE = 50 * 1024 * 1024
MAX_JOURNAL = 512 * 1024 * 1024
MAX_LABELS = 10
MAX_COMMITS = 250
MAX_MARKER = 8000
RENDER_LIMIT = 54000
MAX_UNTRACKED = 20
TIMEOUT = 30
MAX_RETRY_AFTER = 60
COMMIT_PREFIX = 'mendwright fix:'
STORE_FOLDER = '.mendwright'
JOURNAL = 'journal.jsonl'
# Variables that make git print what it sends, or ask someone for a password, and those that
# would put another author on the action's commit (they win over -c user.email), so that the
# next run would take its own commit for someone else's.
_GIT_UNSET = ('GIT_TRACE', 'GIT_TRACE_PACKET', 'GIT_TRACE_CURL', 'GIT_CURL_VERBOSE',
              'GIT_ASKPASS', 'SSH_ASKPASS', 'MW_TOKEN', 'GIT_CONFIG_COUNT', 'GIT_DIR',
              'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL',
              'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL')

INTRO = ('mendwright healed these selectors while your tests ran, and each heal was confirmed. '
         'This pull request writes them into your code, so the code says what the store says.',
         'Check each new selector before you merge. A passing test confirms a heal, but a test '
         'can pass for the wrong reason, and anything that can write your test job\'s files can '
         'write the store.')
MORE_LEFT = 'Run mendwright fix with this store to see them all.'


class Stop(Exception):
    """A refusal: the message is printed as an error, written to the job summary as
    Stopped: <message>, and the step exits 1. summary, when given, is the message as the job
    summary shows it, with the text from the artifact or the store in code spans."""

    def __init__(self, message, summary=None):
        super().__init__(message)
        self.summary = summary


class Done(Exception):
    """A result reached before the end of a step: (result, summary line, annotation)."""

    def __init__(self, result, line, annotation=None):
        super().__init__(line)
        self.result, self.line, self.annotation = result, line, annotation


def plan_hash(replacements):
    """mendwright.fixback.plan_hash, copied so publish needs no mendwright: the SHA-256 of
    each replacement's path, old selector, new selector and keys, sorted."""
    rows = sorted([r['path'], r['selector'], r['new'], sorted(r['keys'])] for r in replacements)
    text = json.dumps(rows, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def encodable(text):
    """text with any lone surrogate written as \\uXXXX, so it can be written as UTF-8. A key
    or a file name can hold one, and the log, the outputs and the job summary are UTF-8."""
    return re.sub('[\ud800-\udfff]', lambda m: '\\u%04X' % ord(m.group()), str(text))


def escape_data(text):
    """Text for the data part of a workflow command (::notice::<data>)."""
    return str(text).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def md_code(text):
    """text as one Markdown code span (see mendwright._md.code, which publish cannot
    import): control and bidirectional characters written out, the span's backticks one
    more than the longest run inside it."""
    out = []
    for ch in str(text):
        n = ord(ch)
        if (n < 0x20 or 0x7F <= n <= 0x9F or n in (0x2028, 0x2029, 0x061C, 0x200E, 0x200F)
                or 0x202A <= n <= 0x202E or 0x2066 <= n <= 0x2069 or 0xD800 <= n <= 0xDFFF):
            out.append('\\u%04X' % n)
        else:
            out.append(ch)
    text = re.sub(r'\s+', ' ', ''.join(out))
    if not text:
        return '(empty)'
    ticks = '`' * (max((len(m) for m in re.findall('`+', text)), default=0) + 1)
    if text[0] in '` ' or text[-1] in '` ':
        text = ' ' + text + ' '
    return ticks + text + ticks


def plural(n, word):
    return str(n) + ' ' + word + ('' if n == 1 else 's')


def title_for(n):
    return 'mendwright fix: write back ' + plural(n, 'healed selector')


def _norm(path):
    path = os.path.normpath(str(path)).replace('\\', '/')
    return '' if path == '.' else path


def _inside(path, folder):
    """True when the real path of path is folder or inside it (both real paths)."""
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(folder)]) == \
            os.path.normcase(folder)
    except ValueError:              # another drive
        return False


def _version(action_path):
    """mendwright's version, read from the fallback literal in mendwright/__init__.py so
    nothing is imported."""
    try:
        with open(os.path.join(action_path, 'mendwright', '__init__.py'), encoding='utf-8') as f:
            found = re.findall(r"__version__ = '([^']+)'", f.read())
        return found[-1] if found else 'unknown'
    except OSError:
        return 'unknown'


# --- the GitHub API ----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_transport(method, url, headers, body, timeout):
    """(status, headers, body bytes) for one request. Follows no redirect: a 3xx comes back
    as it is. Raises OSError when the server cannot be reached."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b''


class GitHub:
    """The REST calls the action makes, through an injectable transport."""

    def __init__(self, action, transport=None):
        self.action = action
        self.transport = transport or urllib_transport
        self.base = (action.env.get('GITHUB_API_URL') or 'https://api.github.com').rstrip('/')

    def call(self, method, path, body=None, *, retry=True):
        """(status, data). A redirect, an unreachable server and a rate limit stop."""
        headers = {'Authorization': 'Bearer ' + self.action.token,
                   'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'mendwright-action/' + self.action.version}
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        try:
            status, got, raw = self.transport(method, self.base + path, headers, data, TIMEOUT)
        except (OSError, ValueError) as exc:
            reason = 'timed out after ' + str(TIMEOUT) + ' s' if 'timed out' in str(exc) \
                else type(exc).__name__ + ': ' + str(exc)
            raise Stop('mendwright: could not reach the GitHub API at ' + self.base + ' ('
                       + reason + ').') from None
        if 300 <= status < 400:
            raise Stop('mendwright: GitHub answered HTTP ' + str(status) + ' for '
                       + path.split('?')[0] + ', a redirect, which the action does not follow. '
                       'Was the repository renamed?')
        try:
            parsed = json.loads(raw.decode('utf-8')) if raw else None
        except ValueError:
            parsed = None
        wait = _retry_after(got) if status in (403, 429) else None
        if wait is not None:
            if retry and wait <= MAX_RETRY_AFTER:
                self.action.sleep(wait)
                return self.call(method, path, body, retry=False)
            raise Stop("mendwright: GitHub's rate limit stopped the action (HTTP " + str(status)
                       + ', retry after ' + str(wait) + ' s). '
                       + ('The branch ' + self.action.branch + ' is pushed; the pull request '
                          'was not opened.' if self.action.pushed else 'Nothing was pushed.'))
        return status, parsed


def _retry_after(headers):
    value = (headers or {}).get('retry-after')
    try:
        return int(str(value).strip()) if value is not None else None
    except ValueError:
        return None


def _message(data):
    if isinstance(data, dict):
        text = str(data.get('message') or '')
        errors = data.get('errors')
        if isinstance(errors, list):
            for e in errors:
                if isinstance(e, dict) and e.get('message'):
                    text += ' ' + str(e['message'])
        return text.strip() or 'no message'
    return 'no message'


# --- the steps' shared plumbing ------------------------------------------------------------

class Action:
    """One step of the action: its environment, output and job summary, git and the API.
    Every side effect goes through here, so the tests can give fakes."""

    def __init__(self, env=None, *, stdout=None, transport=None, sleep=time.sleep,
                 token_hex=secrets.token_hex, stat=os.stat, git='git'):
        self.env = dict(os.environ if env is None else env)
        self.stdout = stdout or sys.stdout
        self.sleep = sleep
        self.token_hex = token_hex
        self.stat = stat
        self.git_exe = git
        self.outputs = {}
        self.git_calls = []       # every git argv, for the tests
        self.pushed = False
        self.branch = ''
        self.action_path = self.env.get('GITHUB_ACTION_PATH') or os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        self.version = _version(self.action_path)
        self.token = self.env.get('MW_TOKEN', '')
        self.github = GitHub(self, transport)
        self.temp = os.path.join(self.env.get('RUNNER_TEMP') or tempfile.gettempdir(),
                                 'mendwright-action')
        self._hooks = None

    # inputs and outputs

    def input(self, name):
        return self.env.get('MW_IN_' + name.upper().replace('-', '_'), '')

    def lines(self, name):
        return [line.strip() for line in self.input(name).splitlines() if line.strip()]

    def flag(self, name):
        return self.input(name).strip() == 'true'

    def print(self, text=''):
        self.stdout.write(encodable(text) + '\n')
        self.stdout.flush()

    def annotate(self, kind, text):
        self.print('::' + kind + '::' + escape_data(text))

    def untrusted(self, text):
        """Print text that came from the store, the journal, the artifact or the source
        files, where no workflow command in it can run."""
        token = self.token_hex(16)
        self.print('::stop-commands::' + token)
        for line in str(text).splitlines():
            self.print(line)
        self.print('::' + token + '::')

    def set_output(self, name, value):
        value = encodable(value)
        self.outputs[name] = value
        path = self.env.get('GITHUB_OUTPUT')
        if not path:
            return
        delimiter = 'mw_' + self.token_hex(16)
        with open(path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(name + '<<' + delimiter + '\n' + value + '\n' + delimiter + '\n')

    def summary(self, text):
        path = self.env.get('GITHUB_STEP_SUMMARY')
        if not path:
            return
        with open(path, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(encodable(text).rstrip('\n') + '\n')

    # git

    def hooks_off(self):
        if self._hooks is None:
            os.makedirs(self.temp, exist_ok=True)
            self._hooks = tempfile.mkdtemp(prefix='no-hooks-', dir=self.temp)
        return self._hooks

    def git_env(self, extra=None):
        env = {k: v for k, v in self.env.items() if k not in _GIT_UNSET
               and not k.startswith('GIT_CONFIG_')}
        env['GIT_TERMINAL_PROMPT'] = '0'
        env.update(extra or {})
        return env

    def git(self, *args, cwd=None, extra_env=None, check=True):
        """git with an argument list, hooks off. (exit code, stdout, stderr) as text."""
        argv = [self.git_exe, '-c', 'core.hooksPath=' + self.hooks_off(),
                '-c', 'core.fsmonitor=false'] + list(args)
        self.git_calls.append({'argv': list(argv), 'env': dict(extra_env or {})})
        try:
            done = subprocess.run(argv, cwd=cwd, env=self.git_env(extra_env),
                                  stdin=subprocess.DEVNULL, capture_output=True)
        except OSError as exc:
            raise Stop('mendwright: could not run git (' + str(exc) + '). The action needs git '
                       'on the runner.') from None
        out = done.stdout.decode('utf-8', 'replace')
        err = done.stderr.decode('utf-8', 'replace')
        if check and done.returncode != 0:
            raise Stop('mendwright: git ' + ' '.join(args[:2]) + ' failed: '
                       + _first_error(err, self.secrets()))
        return done.returncode, out, err

    def secrets(self):
        values = [self.token, self.auth_value()] if self.token else []
        return [v for v in values if v]

    def auth_value(self):
        if not self.token:
            return ''
        return base64.b64encode(('x-access-token:' + self.token).encode('utf-8')).decode('ascii')

    def auth_env(self):
        """The GIT_CONFIG_* pairs that give git the token for the server, and clear a header
        actions/checkout may have saved. Nothing is written to .git/config."""
        server = (self.env.get('GITHUB_SERVER_URL') or 'https://github.com').rstrip('/')
        key = 'http.' + server + '/.extraheader'
        return {'GIT_CONFIG_COUNT': '2', 'GIT_CONFIG_KEY_0': key, 'GIT_CONFIG_VALUE_0': '',
                'GIT_CONFIG_KEY_1': key,
                'GIT_CONFIG_VALUE_1': 'AUTHORIZATION: basic ' + self.auth_value()}

    def remote_url(self):
        server = (self.env.get('GITHUB_SERVER_URL') or 'https://github.com').rstrip('/')
        return server + '/' + self.env.get('GITHUB_REPOSITORY', '') + '.git'

    # paths

    def workspace(self):
        return os.path.realpath(self.env.get('GITHUB_WORKSPACE') or os.getcwd())

    def working_directory(self):
        wd = self.input('working-directory') or '.'
        return os.path.realpath(os.path.join(self.workspace(), wd))


def _first_error(text, secrets_=()):
    """git's first line that says what went wrong, with the secrets masked."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    pick = next((line for line in lines if line.startswith(('error:', 'fatal:', '!', 'remote:'))
                 and 'unable to access' not in line), None) or (lines[-1] if lines else 'no message')
    for value in secrets_:
        pick = pick.replace(value, '***')
    return pick


# --- guard -------------------------------------------------------------------------------

def _event(action):
    path = action.env.get('GITHUB_EVENT_PATH')
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _check_inputs(action):
    """G11, G12, G14, G17 and G15, before anything else."""
    for name in BOOLEAN_INPUTS:
        value = action.input(name)
        if value not in ('true', 'false'):
            raise Stop('mendwright: the input ' + name + ' takes true or false, got '
                       + repr(value) + '.')
    tests = action.input('tests-result').strip()
    if tests and tests not in TEST_RESULTS:
        raise Stop('mendwright: the input tests-result takes a job result such as success or '
                   'failure (pass needs.test.result), got ' + repr(tests) + '.')
    store = action.input('store').strip()
    parts = store.replace('\\', '/').split('/')
    if not store or os.path.isabs(store) or store.startswith(('/', '\\')) or '..' in parts \
            or re.match(r'^[A-Za-z]:', store):
        raise Stop("mendwright: the input store takes a relative path without '..', such as "
                   'overrides.json, got ' + repr(store) + '.')
    labels = [label.strip() for label in action.input('labels').split(',') if label.strip()]
    if len(labels) > MAX_LABELS:
        raise Stop('mendwright: the input labels takes at most ' + str(MAX_LABELS)
                   + ' labels, got ' + str(len(labels)) + '.')
    wd = action.input('working-directory') or '.'
    if not _inside(action.working_directory(), action.workspace()):
        raise Stop('mendwright: working-directory ' + wd + ' is outside the workspace ('
                   + action.workspace() + '). Pass a folder inside your checkout.')
    version = sys.version_info[:2]
    if not PYTHONS[0] <= version <= PYTHONS[1]:
        raise Stop('mendwright: the action needs Python 3.10 to 3.14, for its lxml wheel, and '
                   'got ' + platform.python_version() + '. Set python-version to 3.12, or leave '
                   'it at its default.')


def _skip(text):
    """Done for a skip: text is the notice, and the summary says Skipped: <reason>."""
    reason = re.sub(r'^mendwright: skipped:? ?', '', text)
    return Done('skipped', 'Skipped: ' + reason + ('' if reason.endswith('.') else '.'),
                ('notice', text))


def _repo_info(action, event):
    """(default branch, public) from the payload's repository, else GET /repos/{repo}."""
    repo = event.get('repository') if isinstance(event.get('repository'), dict) else {}
    default, visibility = repo.get('default_branch'), repo.get('visibility')
    if not visibility and isinstance(repo.get('private'), bool):
        visibility = 'private' if repo['private'] else 'public'
    if not default or not visibility:
        status, data = action.github.call('GET', '/repos/' + action.env.get('GITHUB_REPOSITORY', ''))
        if status != 200 or not isinstance(data, dict):
            raise Stop('mendwright: could not read the repository from the GitHub API (HTTP '
                       + str(status) + ': ' + _message(data) + '). Pass the token input, or '
                       'run the action in the repository it changes.')
        default = default or data.get('default_branch')
        visibility = visibility or data.get('visibility') or (
            'private' if data.get('private') else 'public')
    return str(default or ''), visibility == 'public'


def decide(action):
    """The guard's decision: a dict of its outputs, or raises Stop or Done."""
    _check_inputs(action)
    env, event = action.env, _event(action)
    name = env.get('GITHUB_EVENT_NAME', '')
    repo = env.get('GITHUB_REPOSITORY', '')
    dry = action.flag('dry-run')
    run_id = env.get('GITHUB_RUN_ID', '')
    if name == 'pull_request_target':
        raise Stop('mendwright: this workflow runs on pull_request_target, where a job has a '
                   'write token and your secrets while it can see a fork\'s changes. The '
                   'mendwright action does not run there. Run it in a job that needs your test '
                   'job on push, schedule or workflow_dispatch, as the guide shows.')
    if name == 'pull_request':
        if not dry:
            raise _skip('mendwright: skipped on pull_request. The action opens pull requests '
                        'from runs on push, schedule or workflow_dispatch. Set dry-run: true to '
                        'see what it would write.')
        return {'mode': 'dry-run', 'base': env.get('GITHUB_BASE_REF', ''),
                'sha': env.get('GITHUB_SHA', ''), 'artifact-run-id': run_id, 'same-run': 'true'}
    if name in TRUSTED_EVENTS:
        ref = env.get('GITHUB_REF', '')
        if not ref.startswith('refs/heads/'):
            what = ('the tag ' + ref[len('refs/tags/'):] if ref.startswith('refs/tags/')
                    else repr(ref))
            raise _skip('mendwright: skipped: this run is for ' + what + ', not a branch.')
        return {'mode': 'dry-run' if dry else 'fix', 'base': ref[len('refs/heads/'):],
                'sha': env.get('GITHUB_SHA', ''), 'artifact-run-id': run_id, 'same-run': 'true'}
    if name == 'workflow_run':
        run = event.get('workflow_run') if isinstance(event.get('workflow_run'), dict) else {}
        upstream = str(run.get('event') or 'an unknown event')
        head = run.get('head_repository') if isinstance(run.get('head_repository'), dict) else {}
        own = run.get('repository') if isinstance(run.get('repository'), dict) else {}
        head_repo, own_repo = head.get('full_name'), own.get('full_name')
        if head_repo != repo or own_repo != repo:
            raise _skip('mendwright: skipped: the run that made this store was triggered by '
                        + upstream + ' from ' + str(head_repo or 'an unknown repository')
                        + ", so code from outside this repository's branches could have written "
                        'it. The action only uses stores from runs on push, schedule or '
                        'workflow_dispatch in ' + repo + '.')
        if upstream not in TRUSTED_EVENTS:
            why = ('code from a pull request could have written it' if 'pull_request' in upstream
                   else 'the action does not trust that event')
            raise _skip('mendwright: skipped: the run that made this store was triggered by '
                        + upstream + ' in ' + repo + ', so ' + why + '. The action only uses '
                        'stores from runs on push, schedule or workflow_dispatch in ' + repo + '.')
        base, sha, upstream_id = run.get('head_branch'), run.get('head_sha'), run.get('id')
        if not (isinstance(base, str) and base and isinstance(sha, str) and sha
                and str(upstream_id or '').isdigit()):
            raise _skip('mendwright: skipped: the workflow_run payload names no branch, commit '
                        'or run.')
        return {'mode': 'dry-run' if dry else 'fix', 'base': base, 'sha': sha,
                'artifact-run-id': str(upstream_id), 'same-run': 'false'}
    raise _skip('mendwright: skipped on ' + (name or 'an unknown event') + '. The action runs on '
                'push, schedule, workflow_dispatch and workflow_run, and does a dry run on '
                'pull_request.')


def _check_checkout(action, decision, notices):
    """G6 to G10, G13 and G16 in mode fix; in mode dry-run a HEAD that differs and a dirty
    checkout are notices."""
    mode, base, sha = decision['mode'], decision['base'], decision['sha']
    prefix = action.input('branch-prefix')
    wd = action.working_directory()
    if mode == 'fix':
        if base.startswith(prefix):
            raise _skip('mendwright: skipped: ' + base + ' is a branch this action writes.')
        default, public = _repo_info(action, _event(action))
        decision['default-branch'] = default
        decision['public'] = 'true' if public else 'false'
        if action.flag('only-default-branch') and base != default:
            raise _skip('mendwright: skipped: the tests ran on ' + base + ', and '
                        'only-default-branch is true, so pull requests go into ' + default
                        + ' only. Set only-default-branch: false to open them for other branches '
                        'too.')
        code = action.git('check-ref-format', '--branch', prefix + base, check=False)[0]
        if not prefix.endswith('/') or code != 0:
            raise Stop('mendwright: branch-prefix ' + repr(prefix) + " must end with '/' and make "
                       'a valid branch name with the base branch, and git check-ref-format '
                       'refused ' + repr(prefix + base) + '.')
    code, out, _ = action.git('-C', wd, 'rev-parse', '--is-inside-work-tree', check=False)
    if code != 0 or out.strip() != 'true':
        message = ('mendwright: ' + (action.input('working-directory') or '.') + ' is not inside '
                   'a git checkout. Run actions/checkout first.')
        if mode == 'fix':
            raise Stop(message)
        notices.append(message)
        return
    head = action.git('-C', wd, 'rev-parse', 'HEAD', check=False)[1].strip()
    if head != sha:
        message = ('mendwright: the checkout is at ' + head[:7] + ', but the tests ran on '
                   + sha[:7] + '. mendwright fix must change the code the tests ran on, so '
                   'nothing was changed. Check out that commit: under workflow_run, give '
                   'actions/checkout ref: github.event.workflow_run.head_sha.')
        if mode == 'fix':
            raise Stop(message)
        notices.append(message)
    dirty = action.git('-C', wd, 'status', '--porcelain=v1', '-z', '--untracked-files=no')[1]
    changed = _porcelain_paths(dirty)
    if changed:
        message = ('mendwright: the checkout has changes to tracked files before mendwright fix '
                   'ran (' + ', '.join(changed[:10]) + (', ...' if len(changed) > 10 else '')
                   + '). Run the action right after actions/checkout, so the pull request holds '
                   "only mendwright's changes.")
        if mode == 'fix':
            raise Stop(message)
        notices.append(message)
    if mode == 'fix' and decision.get('public') == 'true':
        notices.append('mendwright: this repository is public, so the branch and pull request '
                       'are public too. A healed selector can quote text from your pages.')


def cmd_guard(action):
    notices = []
    decision = decide(action)
    _check_checkout(action, decision, notices)
    for text in notices:
        action.annotate('notice', text)
    for name in ('mode', 'base', 'sha', 'artifact-run-id', 'same-run', 'default-branch', 'public'):
        action.set_output(name, decision.get(name, ''))
    action.print('mendwright: ' + decision['mode'] + ' for ' + decision['base'] + ' at '
                 + decision['sha'][:7] + '.')
    return 0


# --- find-artifact -------------------------------------------------------------------------

def cmd_find_artifact(action):
    name = action.input('artifact')
    run_id = action.env.get('MW_ARTIFACT_RUN_ID', '')
    repo = action.env.get('GITHUB_REPOSITORY', '')
    if not run_id.isdigit():
        raise Stop('mendwright: no run to take the artifact from (the guard set none).')
    status, data = action.github.call('GET', '/repos/' + repo + '/actions/runs/' + run_id
                                      + '/artifacts?name=' + urllib.parse.quote(name, safe='')
                                      + '&per_page=100')
    if status != 200 or not isinstance(data, dict):
        raise Stop('mendwright: could not list the artifacts of run ' + run_id + ' (HTTP '
                   + str(status) + ': ' + _message(data) + '). Give the job permissions: '
                   'actions: read.')
    found = any(isinstance(a, dict) and a.get('name') == name and not a.get('expired')
                for a in data.get('artifacts') or ())
    action.set_output('found', 'true' if found else 'false')
    action.print('mendwright: run ' + run_id + (' has' if found else ' has no') + ' artifact '
                 'named ' + name + '.')
    return 0


# --- fix -----------------------------------------------------------------------------------

def _regular(path):
    """True for a regular file that is not a link."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def find_store(action):
    """(store path, journal path or None). Raises Stop or Done (F1 to F8)."""
    store_input = action.input('store').strip()
    artifact = action.input('artifact').strip()
    if artifact:
        if action.env.get('MW_FOUND') != 'true':
            raise Done('nothing', 'Nothing to write back.', ('notice', (
                'mendwright: run ' + action.env.get('MW_ARTIFACT_RUN_ID', '') + ' has no '
                'artifact named ' + artifact + ', so there is nothing to write back. If your '
                'tests use mendwright, upload the store with actions/upload-artifact and '
                'include-hidden-files: true (.mendwright is a hidden folder).')))
        root = os.path.join(action.temp, 'artifact')
        tries = [os.path.join(root, store_input), os.path.join(root, STORE_FOLDER, store_input)]
        found = next((p for p in tries if os.path.lexists(p)), None)
        if found is None:
            try:
                entries = sorted(os.listdir(root))
            except OSError:
                entries = []
            shown = [e + ('/' if os.path.isdir(os.path.join(root, e)) else '') for e in entries]
            more = ', ...' if len(shown) > 20 else ''

            def refusal(listed):
                return ('mendwright: the artifact ' + artifact + ' has no ' + store_input + ' at '
                        'its top or in .mendwright/ (it has: ' + (listed or 'nothing') + '). '
                        'Upload the folder that holds the store, such as .mendwright/.')
            raise Stop(refusal(', '.join(repr(e) for e in shown[:20]) + more),
                       summary=refusal(', '.join(md_code(e) for e in shown[:20]) + more))
        inside = os.path.realpath(root)
        refused = ('mendwright: the store or journal in the artifact is a link or points outside '
                   'it, so it was not read.')
    else:
        wd = action.working_directory()
        tries = [os.path.join(wd, store_input), os.path.join(wd, STORE_FOLDER, store_input)]
        found = next((p for p in tries if os.path.lexists(p)), None)
        if found is None:
            raise Done('nothing', 'Nothing to write back.', ('notice', (
                'mendwright: no store at ' + _norm(os.path.join(STORE_FOLDER, store_input))
                + ', so there is nothing to write back.')))
        inside = action.workspace()
        refused = ('mendwright: the store ' + found + ' is a link or is outside the workspace, '
                   'so it was not read.')
    journal = os.path.join(os.path.dirname(found), JOURNAL)
    for path in [found] + ([journal] if os.path.lexists(journal) else []):
        if not _regular(path) or not _inside(os.path.realpath(path), inside):
            raise Stop(refused)
    for path, limit in ((found, MAX_STORE), (journal, MAX_JOURNAL)):
        if not os.path.lexists(path):
            continue
        size = action.stat(path).st_size
        if size > limit:
            raise Stop('mendwright: ' + os.path.basename(path) + ' is '
                       + str(size // (1024 * 1024)) + " MiB, over the action's limit of "
                       + str(limit // (1024 * 1024)) + ' MiB, so it was not read.'
                       + (' Move the old journal aside, or stop carrying it from run to run.'
                          if path == journal else ''))
    if not os.path.lexists(journal):
        action.annotate('warning', 'mendwright: there is no journal.jsonl next to the store, so '
                        'no heal counts as confirmed and nothing is written back. Upload the '
                        'journal too, not only overrides.json.')
        return found, None
    return found, journal


def _best_sources(store):
    """{key: source of its best active candidate}, as mendwright fix picks it."""
    out = {}
    for key, entry in (store.get('selectors') or {}).items():
        if not isinstance(entry, dict):
            continue
        active = [c for c in entry.get('candidates') or () if isinstance(c, dict)
                  and isinstance(c.get('xpath'), str) and c['xpath']
                  and (c.get('status') or 'active') == 'active']
        if active:
            best = sorted(active, key=lambda c: (str(c.get('lastSucceededAt') or ''),
                                                 c.get('source') == 'heal'), reverse=True)[0]
            out[key] = best.get('source')
    return out


def _repo_prefix(action, wd):
    """The working directory relative to the repository root, with a trailing /, or ''."""
    code, out, _ = action.git('-C', wd, 'rev-parse', '--show-toplevel', check=False)
    if code != 0 or not out.strip():
        return ''
    rel = _norm(os.path.relpath(wd, os.path.realpath(out.strip())))
    return '' if not rel or rel.startswith('..') else rel + '/'


def _same_file(top, a, b):
    try:
        return os.path.samefile(os.path.join(top, a), os.path.join(top, b))
    except (OSError, ValueError):
        return False


def _fold(path):
    return unicodedata.normalize('NFC', path).casefold()


def git_name(top, path, changed):
    """The name git gives the file path (repository-relative, with /) among changed (git's
    names), or None: the same text, or, where the file system ignores case, the same file
    written in another case, such as Tests/a.py for git's tests/a.py."""
    if path in changed:
        return path
    for name in changed:
        if _fold(name) == _fold(path) and _same_file(top, name, path):
            return name
    return None


def _stop_after_errors(action, wd, written, first):
    """F11: put back the tracked files mendwright fix wrote, and stop. git cannot put back a
    file it does not track, so the message names those."""
    start = 'mendwright: mendwright fix could not finish (' + first + '). '
    end = ' The other errors are above.'
    tracked, kept, failed = [], [], None
    if written:
        top = os.path.realpath(action.git('-C', wd, 'rev-parse', '--show-toplevel')[1].strip())
        changed = _porcelain_paths(action.git('-C', top, 'status', '--porcelain=v1', '-z',
                                              '--untracked-files=no')[1])
        names = [(path, git_name(top, path, changed)) for path in written]
        tracked = [name for _, name in names if name]
        kept = [path for path, name in names if not name]
    if tracked:
        code, _, err = action.git('-C', top, 'checkout', '--', *tracked, check=False)
        if code != 0:
            failed = _first_error(err, action.secrets())
    if failed is not None:
        raise Stop(start + 'Nothing was pushed, but git could not put back the files it wrote ('
                   + failed + ').' + end)
    if not kept:
        raise Stop(start + 'The files it wrote were put back and nothing was pushed.' + end)

    def refusal(shown):
        return (start + ('The tracked files it wrote were put back and n' if tracked else 'N')
                + 'othing was pushed, but git does not track ' + ', '.join(shown) + ', so '
                + ('it keeps' if len(kept) == 1 else 'they keep') + " mendwright fix's changes."
                + end)
    raise Stop(refusal(kept), summary=refusal([md_code(p) for p in kept]))


def run_fix(action, argv):
    """mendwright.cli.main(argv), with its stdout and stderr captured: (code, text)."""
    if action.action_path not in sys.path:
        sys.path.append(action.action_path)
    import mendwright.cli
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        code = mendwright.cli.main(argv)
    finally:
        sys.stdout, sys.stderr = saved
    return code, out.getvalue() + err.getvalue()


def cmd_fix(action):
    mode = action.env.get('MW_MODE', '')
    wd = action.working_directory()
    store, journal = find_store(action)
    for entry in action.lines('paths') or ['.']:
        target = os.path.realpath(os.path.join(wd, entry))
        if not _inside(target, wd):
            raise Stop('mendwright: the path ' + entry + ' is outside the working directory, and '
                       'mendwright fix would write there. Pass paths inside your checkout.')
    os.makedirs(action.temp, exist_ok=True)
    fix_json = os.path.join(action.temp, 'fix.json')
    if os.path.exists(fix_json):
        os.unlink(fix_json)
    argv = ['fix', '--store=' + store, '--format=json', '--out=' + fix_json]
    argv += ['--path=' + p for p in action.lines('paths') or ['.']]
    argv += ['--glob=' + g for g in action.lines('globs')]
    argv += ['--destructive-key=' + k for k in action.lines('destructive-keys')]
    if mode == 'fix':
        argv.append('--apply')
    action.fix_argv = argv
    here = os.getcwd()
    try:
        os.chdir(wd)
        code, said = run_fix(action, argv)
    finally:
        os.chdir(here)
    if said.strip():
        action.untrusted(said)
    if not os.path.exists(fix_json):
        raise Stop('mendwright: mendwright fix could not read the store (see its message above), '
                   'so nothing was changed.')
    with open(fix_json, encoding='utf-8') as handle:
        data = json.load(handle)
    prefix = _repo_prefix(action, wd)
    written = [prefix + f['path'] for f in data.get('files') or () if f.get('written')]
    if data.get('errors'):
        _stop_after_errors(action, wd, written, str(data['errors'][0]))
    import mendwright.cli
    import mendwright.fixback
    store_data = mendwright.cli._read_store(store)
    render = mendwright.fixback.render_markdown(data, heading='###', limit=RENDER_LIMIT,
                                                path_prefix=prefix)
    keys = [w['key'] for w in data.get('written_back') or ()]
    left = data.get('left_alone') or []
    plan = {'plan': data['plan'], 'written': keys,
            'left_alone': {i['key']: i['code'] for i in left if i.get('key') is not None},
            'best_source': _best_sources(store_data), 'files': written,
            'counts': {'written': len(keys), 'left_alone': len(left)},
            'mendwright': data.get('mendwright'),
            'phrases': dict(mendwright.fixback.CODES)}
    with open(os.path.join(action.temp, 'render.md'), 'w', encoding='utf-8', newline='\n') as h:
        h.write(render)
    with open(os.path.join(action.temp, 'body.md'), 'w', encoding='utf-8', newline='\n') as h:
        h.write(INTRO[0] + '\n\n' + INTRO[1] + '\n\n' + render)
    with open(os.path.join(action.temp, 'plan.json'), 'w', encoding='utf-8', newline='\n') as h:
        json.dump(plan, h, ensure_ascii=True, indent=2)
    action.untrusted(render)
    action.set_output('ok', 'true')
    action.set_output('written', len(keys))
    action.set_output('left-alone', len(left))
    return 0


# --- publish -------------------------------------------------------------------------------

_MARKER = re.compile(r'<!-- mendwright-fix (\{.*?\}) -->')


def read_marker(body):
    found = _MARKER.findall(body or '')
    if not found:
        return None
    try:
        data = json.loads(found[-1])
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get('v') == 1 else None


def write_marker(fields):
    text = json.dumps(fields, separators=(',', ':'), ensure_ascii=True)
    text = text.replace('-', '\\u002d').replace('<', '\\u003c').replace('>', '\\u003e')
    return '<!-- mendwright-fix ' + text + ' -->'


def _porcelain_paths(text):
    """The paths in `git status --porcelain=v1 -z` output (both of a rename)."""
    out, items, i = [], text.split('\0'), 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        out.append(_norm(item[3:]))
        if item[0] in 'RC' or item[1] in 'RC':
            if i < len(items) and items[i]:
                out.append(_norm(items[i]))
            i += 1
    return out


class Publish:
    """The publish step: everything after mendwright fix wrote the files."""

    def __init__(self, action):
        self.action = action
        env = action.env
        self.repo = env.get('GITHUB_REPOSITORY', '')
        self.owner = self.repo.split('/')[0]
        self.base = env.get('MW_BASE', '')
        self.sha = env.get('MW_SHA', '')
        self.run = env.get('MW_ARTIFACT_RUN_ID', '') or env.get('GITHUB_RUN_ID', '')
        self.branch = action.input('branch-prefix') + self.base
        action.branch = self.branch
        self.wd = action.working_directory()
        self.email = action.input('commit-author-email')
        server = (env.get('GITHUB_SERVER_URL') or 'https://github.com').rstrip('/')
        self.run_url = server + '/' + self.repo + '/actions/runs/' + self.run
        with open(os.path.join(action.temp, 'plan.json'), encoding='utf-8') as handle:
            self.plan = json.load(handle)
        with open(os.path.join(action.temp, 'body.md'), encoding='utf-8') as handle:
            self.body_md = handle.read()
        try:
            with open(os.path.join(action.temp, 'render.md'), encoding='utf-8') as handle:
                self.render_md = handle.read()
        except OSError:
            self.render_md = self.body_md
        self.untracked = []
        self.notes = []
        self.number = None
        self.url = None
        self._top = None

    # the body

    def content(self):
        """Parts 1 to 4 of the body: the intro, the Markdown and the untracked files."""
        text = self.body_md.rstrip('\n') + '\n'
        if self.untracked:
            shown = self.untracked[:MAX_UNTRACKED]
            text += ('\nWritten but not tracked by git, so not in the pull request: '
                     + ', '.join(md_code(p) for p in shown)
                     + (' and ' + str(len(self.untracked) - len(shown)) + ' more'
                        if len(self.untracked) > len(shown) else '') + '.\n')
        return text

    def marker(self, state='open', keys=None):
        keys = list(self.plan['written'] if keys is None else keys)
        fields = {'v': 1, 'plan': self.plan['plan'],
                  'body': hashlib.sha256(self.content().encode('utf-8')).hexdigest(),
                  'run': int(self.run) if self.run.isdigit() else 0, 'sha': self.sha,
                  'state': state, 'keys': [], 'more': False}
        for key in keys:
            fields['keys'].append(key)
            if len(write_marker(fields)) >= MAX_MARKER:
                fields['keys'].pop()
                fields['more'] = True
                break
        return write_marker(fields)

    def body(self, state='open', keys=None):
        footer = ('From run [#' + self.run + '](' + self.run_url + ') on ' + md_code(self.base)
                  + ' at ' + md_code(self.sha[:7]) + ', with mendwright '
                  + str(self.plan.get('mendwright') or self.action.version) + '. Each run '
                  'rewrites the branch ' + md_code(self.branch) + ". Don't push to it: the action "
                  'stops updating a branch that has commits it did not make. Comment here instead '
                  'of editing this description, which the action rewrites.')
        return self.content() + '\n' + footer + '\n\n' + self.marker(state, keys) + '\n'

    def title(self):
        return title_for(len(self.plan['written']))

    # git

    def tracked_changes(self):
        out = self.action.git('-C', self.wd, 'status', '--porcelain=v1', '-z',
                              '--untracked-files=no')[1]
        return _porcelain_paths(out)

    def put_back(self, files):
        if files:
            top = self.top()
            self.action.git('-C', top, 'checkout', '--', *files, check=False)

    def top(self):
        if self._top is None:
            self._top = os.path.realpath(self.action.git('-C', self.wd, 'rev-parse',
                                                         '--show-toplevel')[1].strip())
        return self._top

    def check_tree(self):
        """The changed tracked files, which must be exactly those mendwright fix wrote."""
        changed = self.tracked_changes()
        planned = [_norm(p) for p in self.plan.get('files') or ()]
        github = [p for p in set(changed) | set(planned) if p == '.github' or p.startswith('.github/')]
        if github:
            self.put_back(changed)
            raise Stop('mendwright: mendwright fix changed ' + ', '.join(sorted(github))
                       + ', under .github/, which it never should. The files were put back and '
                       'nothing was pushed; please report this.')
        extra = sorted(set(changed) - set(planned))
        if extra:
            self.put_back(changed)
            raise Stop('mendwright: git shows changes mendwright fix did not report ('
                       + ', '.join(extra) + '). The files were put back and nothing was pushed; '
                       'please report this.')
        top = self.top()
        for path in planned:
            if path not in changed:
                listed = self.action.git('-C', top, 'ls-files', '-z', '--', path)[1]
                if not listed.strip('\0'):
                    self.untracked.append(path)
        return changed

    # the API

    def pulls(self, state, extra=''):
        query = ('head=' + urllib.parse.quote(self.owner + ':' + self.branch, safe='')
                 + '&base=' + urllib.parse.quote(self.base, safe='') + '&state=' + state + extra)
        status, data = self.action.github.call('GET', '/repos/' + self.repo + '/pulls?' + query)
        if status != 200 or not isinstance(data, list):
            raise Stop('mendwright: could not list the pull requests (HTTP ' + str(status) + ': '
                       + _message(data) + '). Give the job permissions: pull-requests: write.')
        return [p for p in data if isinstance(p, dict) and isinstance(p.get('number'), int)]

    def find(self):
        """(the open pull request or None, the newest closed one or None)."""
        open_ = sorted(self.pulls('open'), key=lambda p: p['number'])
        if len(open_) > 1:
            self.action.annotate('warning', 'mendwright: ' + str(len(open_)) + ' pull requests '
                                 'are open from ' + self.branch + '; the action updates #'
                                 + str(open_[0]['number']) + '. Close the others.')
        if open_:
            return open_[0], None
        closed = self.pulls('closed', '&sort=updated&direction=desc&per_page=1')
        return None, (closed[0] if closed else None)

    def patch(self, number, fields, verb='update'):
        status, data = self.action.github.call('PATCH', '/repos/' + self.repo + '/pulls/'
                                               + str(number), fields)
        if status != 200:
            self.refused(status, data, verb)
        return data

    def refused(self, status, data, verb):
        message = _message(data)
        if status == 403 and 'not permitted to create or approve pull requests' in message:
            raise Stop('mendwright: GitHub refused to open the pull request: Actions may not '
                       'create pull requests in this repository. Turn on Settings, Actions, '
                       'General, Workflow permissions, "Allow GitHub Actions to create and '
                       'approve pull requests", or pass a GitHub App token as token. The branch '
                       + self.branch + ' is pushed, so you can also open the pull request by hand.')
        if status in (403, 404):
            raise Stop('mendwright: GitHub refused to ' + verb + ' the pull request (HTTP '
                       + str(status) + ': ' + message + '). Give the job permissions: '
                       'pull-requests: write.')
        raise Stop('mendwright: GitHub refused the pull request (HTTP ' + str(status) + ': '
                   + message + ').')

    def mergeable(self, number):
        status, data = self.action.github.call('GET', '/repos/' + self.repo + '/pulls/'
                                               + str(number))
        return data.get('mergeable') if status == 200 and isinstance(data, dict) else None

    # pushing

    def remote_sha(self):
        code, out, err = self.action.git('ls-remote', self.action.remote_url(),
                                         'refs/heads/' + self.branch, cwd=self.wd,
                                         extra_env=self.action.auth_env(), check=False)
        if code != 0:
            raise Stop('mendwright: git ls-remote ' + self.branch + ' failed ('
                       + _first_error(err, self.action.secrets()) + ').')
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == 'refs/heads/' + self.branch:
                return parts[0]
        return ''

    def foreign_commits(self):
        """None when every commit on the branch is the action's, else a short description
        of the first that is not (for P3)."""
        path = ('/repos/' + self.repo + '/compare/' + urllib.parse.quote(self.base, safe='/')
                + '...' + urllib.parse.quote(self.branch, safe='/'))
        status, data = self.action.github.call('GET', path)
        if status != 200 or not isinstance(data, dict):
            raise Stop('mendwright: could not compare ' + self.branch + ' with ' + self.base
                       + ' (HTTP ' + str(status) + ': ' + _message(data) + ').')
        total = data.get('total_commits')
        if not isinstance(total, int) or total > MAX_COMMITS:
            return 'more than ' + str(MAX_COMMITS) + ' commits'
        for item in data.get('commits') or ():
            commit = item.get('commit') if isinstance(item, dict) else None
            author = commit.get('author') if isinstance(commit, dict) else None
            email = author.get('email') if isinstance(author, dict) else None
            message = commit.get('message') if isinstance(commit, dict) else None
            if email != self.email or not str(message or '').startswith(COMMIT_PREFIX):
                name = author.get('name') if isinstance(author, dict) else None
                return str(item.get('sha') or '')[:7] + ' by ' + str(name or email or 'someone')
        return None

    def commit_and_push(self, files, remote):
        action, top = self.action, self.top()
        action.git('-C', top, 'switch', '-C', self.branch)
        action.git('-C', top, 'add', '--', *files)
        staged = [_norm(p) for p in action.git('-C', top, 'diff', '--cached', '--name-only',
                                               '-z')[1].split('\0') if p]
        if sorted(staged) != sorted(files):
            self.put_back(files)
            raise Stop('mendwright: git shows changes mendwright fix did not report ('
                       + ', '.join(sorted(set(staged) ^ set(files))) + '). The files were put '
                       'back and nothing was pushed; please report this.')
        os.makedirs(action.temp, exist_ok=True)
        message = os.path.join(action.temp, 'commit-message.txt')
        with open(message, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(self.title() + '\n\nFrom ' + self.run_url + ', tests on ' + self.sha
                         + '.\n')
        name = action.input('commit-author-name')
        who = []
        for role in ('user', 'author', 'committer'):       # author.* and committer.* beat user.*
            who += ['-c', role + '.name=' + name, '-c', role + '.email=' + self.email]
        action.git('-C', top, *who, '-c', 'commit.gpgsign=false',
                   'commit', '--no-verify', '--cleanup=verbatim', '-F', message)
        code, _, err = action.git('-C', top, 'push', '--force-with-lease=refs/heads/'
                                  + self.branch + ':' + remote, action.remote_url(),
                                  'HEAD:refs/heads/' + self.branch,
                                  extra_env=action.auth_env(), check=False)
        if code != 0:
            if 'stale info' in err or 'fetch first' in err:
                raise Stop('mendwright: ' + self.branch + ' changed while this run worked on it, '
                           'so nothing was pushed. The next run picks it up.')
            raise Stop('mendwright: git push to ' + self.branch + ' was refused ('
                       + _first_error(err, action.secrets()) + '). Give the job permissions: '
                       'contents: write, or pass a token that can push. A ruleset that protects '
                       'mendwright/** or asks for signed commits also refuses it.')
        action.pushed = True

    def open_or_update(self, pull):
        """PATCH the open pull request, or POST a new one: (result, number, url)."""
        if pull is not None:
            data = self.patch(pull['number'], {'title': self.title(), 'body': self.body()})
            return 'updated', pull['number'], (data or pull).get('html_url') or pull.get('html_url')
        status, data = self.action.github.call('POST', '/repos/' + self.repo + '/pulls', {
            'title': self.title(), 'head': self.branch, 'base': self.base, 'body': self.body(),
            'draft': self.action.flag('draft')})
        if status == 422 and 'already exists' in _message(data):
            open_, _ = self.find()
            if open_ is not None:
                data = self.patch(open_['number'], {'title': self.title(), 'body': self.body()})
                return 'updated', open_['number'], (data or open_).get('html_url')
        if status != 201 or not isinstance(data, dict):
            self.refused(status, data, 'open')
        labels = [label.strip() for label in self.action.input('labels').split(',') if label.strip()]
        if labels:
            got, answer = self.action.github.call('POST', '/repos/' + self.repo + '/issues/'
                                                  + str(data['number']) + '/labels',
                                                  {'labels': labels})
            if got not in (200, 201):
                self.action.annotate('warning', 'mendwright: could not add the labels to #'
                                     + str(data['number']) + ' (HTTP ' + str(got) + ': '
                                     + _message(answer) + '). The pull request is open.')
        return 'created', data['number'], data.get('html_url')

    # closing

    def why_open(self, pull, marker):
        """None when the open pull request can be closed (step 11), else the P11 line."""
        n = '#' + str(pull['number'])
        if not self.action.flag('close-stale'):
            return n + ' is still open: close-stale is false.'
        if self.action.input('tests-result').strip() != 'success':
            return (n + ' is still open: the tests did not pass in this run, and close-stale '
                    'closes a pull request only after a run whose tests passed.')
        if marker is None or marker.get('more') or not isinstance(marker.get('keys'), list):
            return (n + ' is still open: its description does not list all of its keys, so '
                    'mendwright cannot tell whether it is still needed.')
        missing = [k for k in marker['keys'] if k not in self.plan['left_alone']
                   and self.plan['best_source'].get(k) not in ('code', 'seed')]
        if missing:
            return (n + ' is still open: the store from this run does not account for '
                    + ', '.join(md_code(k) for k in missing) + ', so mendwright cannot tell whether '
                    'it is still needed.')
        return None

    def close(self, pull, marker):
        lines = ['mendwright closed this pull request: run [#' + self.run + '](' + self.run_url
                 + ') shows that none of its selectors needs writing back.', '']
        phrases = self.plan.get('phrases') or {}
        for key in marker['keys']:
            code = self.plan['left_alone'].get(key)
            if code is not None:
                lines.append('- ' + md_code(key) + ': ' + str(phrases.get(code, 'was left alone'))
                             + '.')
            elif self.plan['best_source'].get(key) == 'seed':
                lines.append('- ' + md_code(key) + ': a selector from your seed file works again.')
            else:
                lines.append('- ' + md_code(key) + ': the selector in your code works again.')
        old_body = pull.get('body') or ''
        new_marker = write_marker(dict(marker, state='closed-stale'))
        body = (_MARKER.sub(lambda m: new_marker, old_body) if read_marker(old_body)
                else old_body + '\n\n' + new_marker + '\n')
        self.patch(pull['number'], {'body': body})
        status, data = self.action.github.call('POST', '/repos/' + self.repo + '/issues/'
                                               + str(pull['number']) + '/comments',
                                               {'body': '\n'.join(lines) + '\n'})
        if status != 201:
            self.refused(status, data, 'update')
        self.patch(pull['number'], {'state': 'closed'})

    # the step

    def run_step(self):
        action = self.action
        if action.env.get('MW_MODE') == 'dry-run':
            return self.finish('dry-run', 'Dry run: this is what mendwright would write back.')
        changed = self.check_tree()
        pull, closed = self.find()
        marker = read_marker(pull.get('body')) if pull else None
        if pull is not None and marker and isinstance(marker.get('run'), int) \
                and self.run.isdigit() and marker['run'] > int(self.run):
            self.number, self.url = pull['number'], pull.get('html_url')
            action.annotate('notice', 'mendwright: #' + str(pull['number']) + ' was last updated '
                            'by run ' + str(marker['run']) + ', which is newer than this run ('
                            + self.run + '), so it was left as it is.')
            return self.finish('superseded', '#' + str(pull['number']) + ' was last updated by a '
                               'newer run, so it was left as it is.')
        if not changed:
            if pull is None:
                return self.finish('nothing', 'Nothing to write back.')
            self.number, self.url = pull['number'], pull.get('html_url')
            why = self.why_open(pull, marker)
            if why:
                self.notes.append(why)
                return self.finish('unchanged', '#' + str(pull['number']) + ' is up to date.')
            self.close(pull, marker)
            return self.finish('closed', 'Closed #' + str(pull['number']) + ': none of its '
                               'selectors needs writing back.')
        content_hash = hashlib.sha256(self.content().encode('utf-8')).hexdigest()
        if pull is not None and marker and marker.get('plan') == self.plan['plan']:
            self.number, self.url = pull['number'], pull.get('html_url')
            if not (self.mergeable(pull['number']) is False and marker.get('sha') != self.sha):
                if marker.get('body') != content_hash:
                    self.patch(pull['number'], {'title': self.title(), 'body': self.body()})
                    return self.finish('updated', 'Updated #' + str(pull['number']) + ': '
                                       + self.title() + '.')
                return self.finish('unchanged', '#' + str(pull['number']) + ' is up to date.')
        if pull is None and closed is not None and not closed.get('merged_at'):
            old = read_marker(closed.get('body'))
            if old and old.get('plan') == self.plan['plan'] and old.get('state') != 'closed-stale':
                self.number, self.url = closed['number'], closed.get('html_url')
                action.annotate('notice', 'mendwright: #' + str(closed['number']) + ' had exactly '
                                'these changes and was closed without merging, so the action did '
                                'not open it again. Reopen #' + str(closed['number']) + ' to bring '
                                'it back. A change in what would be written opens a new pull '
                                'request.')
                return self.finish('kept-closed', '#' + str(closed['number']) + ' was closed '
                                   'without merging and has the same changes, so it was not '
                                   'opened again.')
        remote = self.remote_sha()
        if remote:
            foreign = self.foreign_commits()
            if foreign:
                where = ('Merge or close #' + str(pull['number']) + ', or delete the branch'
                         if pull else 'Delete the branch')
                action.annotate('warning', 'mendwright: ' + self.branch + ' has commits that this '
                                'action did not make (' + foreign + '), so it was not updated. '
                                + where + ', and the next run starts it again.')
                if pull:
                    self.number, self.url = pull['number'], pull.get('html_url')
                return self.finish('blocked', 'Did not update ' + self.branch + ': it has commits '
                                   'this action did not make.')
        self.commit_and_push(changed, remote)
        result, self.number, self.url = self.open_or_update(pull)
        verb = 'Opened' if result == 'created' else 'Updated'
        return self.finish(result, verb + ' #' + str(self.number) + ': ' + self.title() + '.')

    def finish(self, result, line):
        action = self.action
        action.set_output('result', result)
        if self.number is not None:
            action.set_output('pull-request-number', self.number)
            action.set_output('pull-request-url', self.url or '')
        action.set_output('written', len(self.plan['written']))
        action.set_output('left-alone', self.plan['counts']['left_alone'])
        text = '## mendwright\n\n' + line + '\n'
        for note in self.notes:
            text += '\n' + note + '\n'
        tests = action.input('tests-result').strip()
        if tests:
            text += '\nThe tests of this run: ' + tests + '.\n'
        if self.untracked:
            text += ('\nWritten but not tracked by git, so not in the pull request: '
                     + ', '.join(md_code(p) for p in self.untracked[:MAX_UNTRACKED]) + '.\n')
        action.summary(text + '\n' + self.render_md)
        n = len(self.plan['written'])
        where = ' (' + str(self.url) + ')' if self.url else ''
        said = {'created': 'opened #' + str(self.number) + ' with ' + plural(n, 'healed selector')
                + where + '.',
                'updated': 'updated #' + str(self.number) + ' with ' + plural(n, 'healed selector')
                + where + '.',
                'unchanged': '#' + str(self.number) + ' is up to date.',
                'closed': 'closed #' + str(self.number) + where + '.'}.get(result)
        action.print('mendwright: ' + (said or line[0].lower() + line[1:]))
        return 0


def cmd_publish(action):
    return Publish(action).run_step()


# --- main ----------------------------------------------------------------------------------

COMMANDS = {'guard': cmd_guard, 'find-artifact': cmd_find_artifact, 'fix': cmd_fix,
            'publish': cmd_publish}


def main(argv=None, action=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in COMMANDS:
        sys.stderr.write('usage: mendwright_action.py {' + ','.join(COMMANDS) + '}\n')
        return 2
    action = action or Action()
    if action.token:
        action.print('::add-mask::' + action.auth_value())
    try:
        return COMMANDS[argv[0]](action)
    except Done as done:
        if done.annotation:
            action.annotate(*done.annotation)
        action.set_output('result', done.result)
        action.summary('## mendwright\n\n' + done.line + '\n')
        if argv[0] == 'guard':
            action.set_output('mode', 'skip')
        if argv[0] == 'fix':
            action.set_output('ok', 'false')
        return 0
    except Stop as stop:
        message = str(stop)
        action.annotate('error', message)
        action.summary('## mendwright\n\nStopped: ' + (stop.summary or message) + '\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
