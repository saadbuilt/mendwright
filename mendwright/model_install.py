"""Download mendwright's local selector model and register it with Ollama.

``mendwright model install`` fetches the quantized model file that a GitHub release of
mendwright carries, checks it against the SHA-256 pinned below, writes an Ollama
Modelfile next to it and runs ``ollama create``. After that,
``MENDWRIGHT_LOCAL_MODEL=mendwright-selector`` selects the model (see
mendwright.providers.local). The file is about 1 GB and is only downloaded once.

The Modelfile template lives here, not in training/, so the model a user installs and the
one ``training/export.py`` builds always use the same prompt format and settings.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .candidates import SYSTEM_PROMPT

DEFAULT_NAME = 'mendwright-selector'
RELEASE_URL = 'https://github.com/saadbuilt/mendwright/releases/download/{tag}/{file}'


@dataclass(frozen=True)
class ModelFile:
    """One published model file: the release that carries it, its name, size and hash."""
    tag: str
    file: str
    size: int
    sha256: str

    @property
    def url(self) -> str:
        return RELEASE_URL.format(tag=self.tag, file=self.file)


# The model this version of mendwright was tested with (training round 3, q4_K_M).
MODEL = ModelFile(
    tag='v0.5.0',
    file='mendwright-selector-r3-q4_k_m.gguf',
    size=986_048_064,
    sha256='05967803f01b586f909c45510733307dfa22d86531fc739e6bb1da2e6b77f7dc',
)

# Qwen2.5's chat format, the one the model was fine-tuned with. {FROM} and {SYSTEM} are
# filled in by modelfile_text().
MODELFILE_TEMPLATE = '''# mendwright local selector model.
FROM {FROM}

TEMPLATE """{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}
{{- range .Messages }}
{{- if eq .Role "user" }}<|im_start|>user
{{ .Content }}<|im_end|>
{{ else if eq .Role "assistant" }}<|im_start|>assistant
{{ .Content }}<|im_end|>
{{ end }}
{{- end }}<|im_start|>assistant
"""

SYSTEM """{SYSTEM}"""

PARAMETER temperature 0
PARAMETER top_k 1
PARAMETER num_ctx 4096
PARAMETER num_predict 24
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|endoftext|>"

LICENSE """Fine-tuned from Qwen2.5-1.5B-Instruct (Apache License 2.0) by the mendwright project."""
'''


class InstallError(Exception):
    """A step of the install failed; the message says which and what to do."""


def modelfile_text(source) -> str:
    """The Modelfile for the model file or folder at ``source``."""
    return MODELFILE_TEMPLATE.replace('{FROM}', str(source)).replace('{SYSTEM}', SYSTEM_PROMPT)


def default_dir() -> Path:
    """Where downloaded models are kept: %LOCALAPPDATA%\\mendwright\\models on Windows,
    $XDG_CACHE_HOME/mendwright/models (default ~/.cache) elsewhere."""
    if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
        base = Path(os.environ['LOCALAPPDATA'])
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
    return base / 'mendwright' / 'models'


def find_ollama() -> str | None:
    """The ollama executable on PATH, or in its default Windows install folder."""
    found = shutil.which('ollama')
    if found:
        return found
    if os.name == 'nt' and os.environ.get('LOCALAPPDATA'):
        exe = Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'Ollama' / 'ollama.exe'
        if exe.is_file():
            return str(exe)
    return None


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def download(model: ModelFile, folder: Path, *, urlopen=urllib.request.urlopen,
             say=print, force=False) -> Path:
    """Download ``model`` into ``folder`` and check its SHA-256. A file that is already
    there with the right hash is kept, unless ``force``. Returns the file's path."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / model.file
    if target.is_file() and not force:
        if sha256_of(target) == model.sha256:
            say('mendwright: ' + str(target) + ' is already downloaded and its checksum matches.')
            return target
        say('mendwright: ' + str(target) + ' is there but its checksum does not match; '
            'downloading it again.')
    partial = target.with_name(target.name + '.part')
    say('mendwright: downloading ' + model.url + ' (' + _size(model.size) + ')')
    digest = hashlib.sha256()
    done = 0
    shown = -1
    try:
        with urlopen(model.url, timeout=60) as response, open(partial, 'wb') as out:
            total = int(response.headers.get('Content-Length') or model.size)
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                tenth = done * 10 // max(total, 1)
                if tenth != shown:
                    shown = tenth
                    say('  ' + str(min(tenth * 10, 100)) + '% (' + _size(done) + ')')
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise InstallError('the download failed (' + str(exc) + '). Check your connection '
                           'and run mendwright model install again.') from None
    if digest.hexdigest() != model.sha256:
        partial.unlink(missing_ok=True)
        raise InstallError('the downloaded file does not match the checksum this version of '
                           'mendwright expects, so it was deleted. Run mendwright model install '
                           'again; if it keeps failing, open an issue.')
    os.replace(partial, target)
    return target


def create(ollama: str, name: str, modelfile: Path, *, run=subprocess.run) -> None:
    """Run ``ollama create <name> -f <modelfile>``."""
    result = run([ollama, 'create', name, '-f', str(modelfile)])
    if result.returncode != 0:
        raise InstallError('ollama create exited with code ' + str(result.returncode) + '. '
                           'Check that Ollama is running (ollama serve, or the Ollama app), '
                           'then run mendwright model install again.')


def install(*, name=DEFAULT_NAME, folder=None, force=False, skip_create=False,
            urlopen=urllib.request.urlopen, run=subprocess.run, ollama=None, say=print,
            model: ModelFile = MODEL) -> Path:
    """Download the model, write its Modelfile and register it with Ollama. Returns the
    Modelfile's path. Raises InstallError with a next step when something fails."""
    if not skip_create:
        ollama = ollama or find_ollama()
        if not ollama:
            raise InstallError('Ollama is not installed (or not on PATH). Install it from '
                               'https://ollama.com/download, then run mendwright model '
                               'install again. --skip-create downloads the file without it.')
    folder = Path(folder) if folder else default_dir()
    gguf = download(model, folder, urlopen=urlopen, say=say, force=force)
    modelfile = folder / 'Modelfile'
    modelfile.write_text(modelfile_text(gguf.resolve()), encoding='utf-8')
    if skip_create:
        say('mendwright: downloaded to ' + str(gguf) + '. Register it later with: ollama create '
            + name + ' -f "' + str(modelfile) + '"')
        return modelfile
    say('mendwright: registering ' + name + ' with Ollama')
    create(ollama, name, modelfile, run=run)
    say('mendwright: ' + name + ' is ready. Select it with MENDWRIGHT_LOCAL_MODEL=' + name
        + ' (or MENDWRIGHT_PROVIDER=local), then check with mendwright doctor.')
    return modelfile


def _size(n) -> str:
    return '{:.0f} MB'.format(n / 1e6) if n < 1e9 else '{:.2f} GB'.format(n / 1e9)


def main(args, out=print, err=None) -> int:
    """The ``mendwright model install`` command."""
    err = err or (lambda text: print(text, file=sys.stderr))
    try:
        install(name=args.name, folder=args.dir, force=args.force,
                skip_create=args.skip_create, say=out)
    except InstallError as exc:
        err('mendwright model install: ' + str(exc))
        return 1
    return 0
