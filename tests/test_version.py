"""The exported ``mendwright.__version__`` must track pyproject's single source of truth.

This pins the two together so the version can never silently drift again (it once read
'0.1.0' while the distribution was 0.2.0). We parse pyproject with a small regex rather
than ``tomllib`` because CI runs on Python 3.10, where ``tomllib`` does not exist.
"""

import re
from pathlib import Path

import mendwright

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject_version():
    text = PYPROJECT.read_text(encoding="utf-8")
    # Match the [project] version line: version = "x.y.z"
    match = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', text)
    assert match, "could not find a version in pyproject.toml"
    return match.group(1)


def test_exported_version_matches_pyproject():
    assert mendwright.__version__ == _pyproject_version()


def test_version_is_a_nonempty_string():
    assert isinstance(mendwright.__version__, str) and mendwright.__version__
