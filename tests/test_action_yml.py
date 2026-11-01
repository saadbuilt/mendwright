"""Static checks of the GitHub Action: action.yml, its pinned actions, the hash-pinned lxml
requirement, what the sdist carries, and the guide's tables. Text checks, so no YAML library
is needed (one test parses action.yml with PyYAML when it is installed)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / 'action.yml'
REQUIREMENTS = ROOT / 'action' / 'requirements.txt'
GUIDE = ROOT / 'docs' / 'guide.md'
WORKFLOWS = ROOT / '.github' / 'workflows'

if not ACTION.exists():
    pytest.skip('action.yml is not in this copy of the project', allow_module_level=True)
TEXT = ACTION.read_text(encoding='utf-8')
_SHA = re.compile(r'^\s*(?:-\s+)?uses:\s+([\w.-]+/[\w.-]+)@(\S+)(?:\s+#\s*(v\S+))?\s*$', re.M)


def _pyproject_version():
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    return re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1)


def steps():
    """{step id or name: the step's lines} for the steps of runs.steps."""
    body = TEXT[TEXT.index('\n  steps:\n'):]
    chunks = re.split(r'\n(?=    - )', body)[1:]
    out = {}
    for chunk in chunks:
        m = re.search(r'^\s+id: (\S+)', chunk, re.M) or re.search(r'name: (.+)', chunk)
        out[m.group(1).strip()] = chunk
    return out


def block(step, key):
    """The lines of one mapping or block scalar of a step (env:, with:, run: |)."""
    lines = step.split('\n')
    for i, line in enumerate(lines):
        if re.match(r'^\s+' + key + r':', line):
            indent = len(line) - len(line.lstrip())
            got = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                got.append(nxt)
            return got
    return []


def test_no_expression_inside_a_run_script():
    found = 0
    for name, step in steps().items():
        for line in block(step, 'run'):
            found += 1
            assert '${{' not in line, (name, line)
    assert found > 5


def test_the_token_goes_only_to_the_steps_that_need_it():
    allowed_env = {'guard', 'find-artifact', 'publish'}
    for name, step in steps().items():
        env = '\n'.join(block(step, 'env'))
        with_ = '\n'.join(block(step, 'with'))
        if name in allowed_env:
            assert 'MW_TOKEN: ${{ inputs.token }}' in env, name
        else:
            assert 'MW_TOKEN' not in step and 'inputs.token' not in env, name
        if 'inputs.token' in with_:
            assert name == 'Download the artifact from the run that triggered this one', name
            assert 'github-token: ${{ inputs.token }}' in with_
    assert 'toJSON' not in TEXT and 'GITHUB_ENV' not in TEXT and 'GITHUB_PATH' not in TEXT
    assert TEXT.count('inputs.token') == 4


def test_the_webhook_url_goes_only_to_the_notify_step():
    for name, step in steps().items():
        env = '\n'.join(block(step, 'env'))
        if name == 'notify':
            assert 'MENDWRIGHT_WEBHOOK_URL: ${{ inputs.webhook-url }}' in env
            assert 'MW_TOKEN' not in step and 'inputs.token' not in step
        else:
            assert 'webhook-url' not in step and 'MENDWRIGHT_WEBHOOK_URL' not in step, name
    assert TEXT.count('inputs.webhook-url') == 2          # its env and its if:
    assert 'webhook-url' not in TEXT[TEXT.index('\noutputs:'):TEXT.index('\nruns:')]


def test_the_notify_step_runs_only_for_a_run_the_guard_let_through_and_never_fails():
    step = steps()['notify']
    condition = ' '.join(step[step.index('if: >-') + len('if: >-'):step.index('shell:')].split())
    assert condition == ("!cancelled() && (steps.guard.outputs.mode == 'fix' || "
                         "steps.guard.outputs.mode == 'dry-run') && github.event_name != "
                         "'pull_request' && inputs.webhook-url != ''")
    run = '\n'.join(block(step, 'run'))
    assert 'notify || echo "::warning title=mendwright notify::' in run
    assert 'if [ -n "$MW_VENV_PY" ]; then' in run


def test_each_step_runs_the_script_with_python_isolated():
    for name in ('guard', 'find-artifact', 'fix', 'publish', 'notify'):
        run = '\n'.join(block(steps()[name], 'run'))
        assert '-I "$GITHUB_ACTION_PATH/action/mendwright_action.py" ' + name in run, name
    for step in steps().values():
        if block(step, 'run'):
            assert 'shell: bash' in step


def test_the_install_line_takes_only_the_hash_pinned_wheel():
    run = '\n'.join(block(steps()['install'], 'run'))
    install = next(line for line in run.splitlines() if 'pip install' in line)
    for flag in ('--only-binary=:all:', '--require-hashes', '--no-deps', '--no-input',
                 '-r "$GITHUB_ACTION_PATH/action/requirements.txt"'):
        assert flag in install, flag
    assert '-I -m pip' in install and '-I -m venv' in run


def test_the_requirements_file_pins_one_lxml_wheel_with_its_hashes():
    lines = [line for line in REQUIREMENTS.read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    requirements = [line for line in lines if not line.startswith(' ')]
    assert len(requirements) == 1
    assert re.fullmatch(r'lxml==\d+\.\d+\.\d+ \\', requirements[0]), requirements[0]
    hashes = lines[1:]
    assert len(hashes) >= 20
    for line in hashes:
        assert re.fullmatch(r'    --hash=sha256:[0-9a-f]{64}( \\)?', line), line
    assert not hashes[-1].endswith('\\')
    assert (ROOT / 'action' / 'requirements.in').read_text(encoding='utf-8').split() == ['lxml']


def test_every_action_is_pinned_to_the_sha_the_workflows_use():
    pins = _SHA.findall(TEXT)
    assert {name for name, _, _ in pins} == {'actions/setup-python', 'actions/download-artifact'}
    for name, ref, comment in pins:
        assert re.fullmatch(r'[0-9a-f]{40}', ref), (name, ref)
        assert comment, name
    if not WORKFLOWS.is_dir():
        pytest.skip('.github/ is not in this copy of the project')
    used = {}
    for path in WORKFLOWS.glob('*.yml'):
        for name, ref, comment in _SHA.findall(path.read_text(encoding='utf-8')):
            used.setdefault(name, set()).add((ref, comment))
    for name, ref, comment in pins:
        assert used.get(name) == {(ref, comment)}, (name, ref, used.get(name))


def test_both_fallback_versions_match_pyproject():
    text = (ROOT / 'mendwright' / '__init__.py').read_text(encoding='utf-8')
    literals = re.findall(r"__version__ = '([^']+)'", text)
    assert literals == [_pyproject_version()] * 2


def test_the_sdist_carries_the_action_and_its_fixtures():
    manifest = (ROOT / 'MANIFEST.in').read_text(encoding='utf-8')
    for line in ('include action.yml', 'recursive-include action *.py *.txt *.in',
                 'recursive-include tests/fixtures/action *.py *.json *.jsonl'):
        assert line in manifest.splitlines(), line


def test_dependabot_keeps_the_lxml_pin_current():
    path = ROOT / '.github' / 'dependabot.yml'
    if not path.exists():
        pytest.skip('.github/ is not in this copy of the project')
    text = path.read_text(encoding='utf-8')
    assert re.search(r'package-ecosystem: pip\n\s+directory: /action\n', text)


def test_the_ci_job_runs_the_action_as_a_dry_run():
    path = WORKFLOWS / 'ci.yml'
    if not path.exists():
        pytest.skip('.github/ is not in this copy of the project')
    text = path.read_text(encoding='utf-8')
    job = text[text.index('\n  action:\n'):]
    for line in ('uses: ./', "artifact: ''", 'working-directory: tests/fixtures/action/project',
                 'store: store/overrides.json', 'dry-run: true', 'windows-latest', 'ubuntu-latest'):
        assert line in job, line


def _guide():
    if not GUIDE.exists():
        pytest.skip('docs/guide.md is not in this copy of the project (the sdist leaves it out)')
    return GUIDE.read_text(encoding='utf-8')


def _section(text, title):
    start = text.index(title)
    end = text.find('\n### ', start + len(title))
    return text[start:end if end > 0 else len(text)]


def test_the_guide_lists_every_input_and_output():
    guide = _section(_guide(), '#### Inputs and outputs')
    inputs = re.findall(r'^  ([\w-]+):\n    description:', TEXT[:TEXT.index('\noutputs:')], re.M)
    outputs = re.findall(r'^  ([\w-]+):\n    description:', TEXT[TEXT.index('\noutputs:'):
                                                               TEXT.index('\nruns:')], re.M)
    in_table = re.findall(r'^\| `([\w-]+)` \|', guide, re.M)
    assert sorted(set(in_table)) == sorted(set(inputs) | set(outputs))
    assert len(inputs) == 22 and len(outputs) == 6


def test_the_guide_uses_the_action_of_this_version():
    """The guide's workflows name the release the next tag makes: pyproject's version, or
    while that version is out already and the CHANGELOG has an Unreleased section, a later
    one, which the release then sets in pyproject.toml."""
    uses = set(re.findall(r'uses: saadbuilt/mendwright@v(\S+)', _guide()))
    assert len(uses) == 1, uses
    [shown] = uses
    version = _pyproject_version()
    if shown != version:
        changelog = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        released = re.search(r'^## \[' + re.escape(version) + r'\] - \d{4}-\d{2}-\d{2}$', changelog,
                             re.M)
        assert released and '## [Unreleased]' in changelog, (shown, version)

        def key(v):
            return tuple(int(p) for p in v.split('.'))
        assert key(shown) > key(version), (shown, version)


def test_action_yml_is_yaml_with_the_steps_in_order():
    yaml = pytest.importorskip('yaml')
    data = yaml.safe_load(TEXT)
    assert data['runs']['using'] == 'composite'
    ids = [s.get('id') for s in data['runs']['steps']]
    assert ids == ['python', 'guard', 'install', 'find-artifact', None, None, 'fix', 'publish',
                   'notify']
    for step in data['runs']['steps']:
        if 'run' in step:
            assert step['shell'] == 'bash'
    assert set(data['inputs']['dry-run']) == {'description', 'default'}
