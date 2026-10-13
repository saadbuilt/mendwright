"""mendwright model install: download, checksum, Modelfile and ollama create, all offline."""

import hashlib
import io

import pytest

from mendwright import model_install as mi
from mendwright.candidates import SYSTEM_PROMPT
from mendwright.cli import main

PAYLOAD = b'GGUF' + b'\0' * 4096


def _model(payload=PAYLOAD, **kw):
    fields = dict(tag='v9.9.9', file='mw-test.gguf', size=len(payload),
                  sha256=hashlib.sha256(payload).hexdigest())
    fields.update(kw)
    return mi.ModelFile(**fields)


class _Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Opener:
    def __init__(self, data=PAYLOAD, fail=None):
        self.data, self.fail, self.urls = data, fail, []

    def __call__(self, url, timeout=None):
        self.urls.append(url)
        if self.fail:
            raise self.fail
        return _Response(self.data)


class _Run:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return type('R', (), {'returncode': self.code})()


def test_install_downloads_checks_writes_modelfile_and_creates(tmp_path):
    opener, run, lines = _Opener(), _Run(), []
    modelfile = mi.install(folder=tmp_path, urlopen=opener, run=run, ollama='ollama',
                           say=lines.append, model=_model())
    gguf = tmp_path / 'mw-test.gguf'
    assert gguf.read_bytes() == PAYLOAD
    assert opener.urls == ['https://github.com/saadbuilt/mendwright/releases/download/v9.9.9/mw-test.gguf']
    text = modelfile.read_text(encoding='utf-8')
    assert 'FROM ' + str(gguf.resolve()) in text and SYSTEM_PROMPT in text
    assert 'PARAMETER temperature 0' in text
    assert run.calls == [['ollama', 'create', 'mendwright-selector', '-f', str(modelfile)]]
    assert any('MENDWRIGHT_LOCAL_MODEL=mendwright-selector' in line for line in lines)
    assert not list(tmp_path.glob('*.part'))


def test_a_file_already_there_with_the_right_hash_is_not_downloaded_again(tmp_path):
    (tmp_path / 'mw-test.gguf').write_bytes(PAYLOAD)
    opener = _Opener()
    mi.install(folder=tmp_path, urlopen=opener, run=_Run(), ollama='ollama', say=lambda s: None,
               model=_model())
    assert opener.urls == []


def test_a_wrong_checksum_deletes_the_download_and_says_so(tmp_path):
    with pytest.raises(mi.InstallError, match='does not match the checksum'):
        mi.install(folder=tmp_path, urlopen=_Opener(b'tampered'), run=_Run(), ollama='ollama',
                   say=lambda s: None, model=_model())
    assert not any(tmp_path.iterdir())


def test_a_network_error_leaves_no_partial_file(tmp_path):
    with pytest.raises(mi.InstallError, match='download failed'):
        mi.install(folder=tmp_path, urlopen=_Opener(fail=OSError('connection reset')),
                   run=_Run(), ollama='ollama', say=lambda s: None, model=_model())
    assert not any(tmp_path.iterdir())


def test_missing_ollama_is_reported_before_any_download(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, 'find_ollama', lambda: None)
    opener = _Opener()
    with pytest.raises(mi.InstallError, match='ollama.com/download'):
        mi.install(folder=tmp_path, urlopen=opener, run=_Run(), say=lambda s: None, model=_model())
    assert opener.urls == []


def test_skip_create_downloads_only_and_prints_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, 'find_ollama', lambda: None)
    run, lines = _Run(), []
    mi.install(folder=tmp_path, urlopen=_Opener(), run=run, skip_create=True, say=lines.append,
               model=_model())
    assert run.calls == [] and (tmp_path / 'mw-test.gguf').is_file()
    assert any('ollama create mendwright-selector -f' in line for line in lines)


def test_a_failing_ollama_create_is_an_install_error(tmp_path):
    with pytest.raises(mi.InstallError, match='ollama create exited with code 1'):
        mi.install(folder=tmp_path, urlopen=_Opener(), run=_Run(1), ollama='ollama',
                   say=lambda s: None, model=_model())


def test_the_pinned_model_points_at_a_release_asset():
    assert mi.MODEL.url.startswith('https://github.com/saadbuilt/mendwright/releases/download/v')
    assert len(mi.MODEL.sha256) == 64 and mi.MODEL.size > 500_000_000


def test_cli_model_install_reports_errors_with_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(mi, 'find_ollama', lambda: None)
    assert main(['model', 'install']) == 1
    assert 'mendwright model install: Ollama is not installed' in capsys.readouterr().err


def test_cli_model_without_an_action_prints_help(capsys):
    assert main(['model']) == 0
    assert 'install' in capsys.readouterr().out
