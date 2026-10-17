"""What the Python sdist and wheel hold: never js/, the npm package, which has a release of its
own. Text checks of MANIFEST.in and pyproject.toml, both of which the sdist carries, plus the
sdist's manifest rules run on a file list that has js/ in it, as a file finder such as
setuptools-scm gives one."""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / 'MANIFEST.in'
PYPROJECT = ROOT / 'pyproject.toml'


def _directives():
    return [line.strip() for line in MANIFEST.read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def _section(text, name):
    start = text.index('[' + name + ']\n')
    end = text.find('\n[', start + 1)
    return text[start:end if end > 0 else len(text)]


def test_the_sdist_prunes_js_after_every_other_line():
    lines = _directives()
    assert lines[-1] == 'prune js'
    # No other line adds the whole tree or anything under js/.
    for line in lines[:-1]:
        words = line.split()
        assert words[0] not in ('graft', 'global-include'), line
        assert not any(w == '.' or w == '*' or w.startswith('js') for w in words[1:]), line


def test_the_wheel_holds_only_the_mendwright_package():
    text = PYPROJECT.read_text(encoding='utf-8')
    find = _section(text, 'tool.setuptools.packages.find')
    assert re.search(r'^include = \["mendwright\*"\]$', find, re.M), find
    data = _section(text, 'tool.setuptools.package-data')
    keys = re.findall(r'^([A-Za-z_][\w.-]*) = ', data, re.M)
    assert keys == ['mendwright'], data


def test_the_manifest_rules_drop_js_from_every_file_git_tracks():
    filelist = pytest.importorskip('setuptools._distutils.filelist')
    tracked = []
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '__pycache__')]
        for name in files:
            tracked.append(os.path.relpath(os.path.join(folder, name), ROOT))
    # js/ as a checkout has it, also when this copy of the project (an sdist) does not.
    tracked += [os.path.join('js', *p.split('/')) for p in (
        'package.json', 'src/index.ts', 'test/unit/store.test.ts', 'README.md',
        'node_modules/@playwright/test/index.js')]
    files = filelist.FileList()
    files.set_allfiles(list(tracked))
    files.files = list(tracked)             # what a file finder would have added
    previous = os.getcwd()
    os.chdir(ROOT)
    try:
        for line in _directives():
            files.process_template_line(line)
    finally:
        os.chdir(previous)
    kept = [f.replace(os.sep, '/') for f in files.files]
    assert not [f for f in kept if f == 'js' or f.startswith('js/')]
    assert 'mendwright/__init__.py' in kept and 'action.yml' in kept
