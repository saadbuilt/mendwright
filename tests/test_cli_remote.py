"""The command line and a remote store (mendwright.remote): report, fix, check, lists and notify
read a store file, and do not read a remote store yet. Given a store URL, with --store or
MENDWRIGHT_STORE, each prints one line that says so and how to download the store and run the
command on the copy: no traceback, no "no store at", and never a URL as it was given when it
holds a credential. doctor names a remote store and never contacts it. Offline."""

import json
import sys

import pytest

from mendwright import cli

COMMANDS = [
    (['report'], 'report', 1),
    (['fix'], 'fix', 1),
    (['check', '--base-url', 'https://shop.example/'], 'check', 4),
    (['lists'], 'lists', 1),
    (['lists', 'accept', 'cart', '1a2b3c4d'], 'lists accept', 1),
    (['lists', 'reject', 'cart'], 'lists reject', 1),
]


def _lines(text):
    return [line for line in text.splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _here(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize('argv, command, code', COMMANDS, ids=[c[1] for c in COMMANDS])
def test_a_command_given_an_s3_store_says_how_to_download_it(argv, command, code, capsys):
    assert cli.main(argv + ['--store', 's3://team-bucket/mendwright/']) == code
    out, err = capsys.readouterr()
    [line] = _lines(err)
    assert line.startswith('mendwright ' + command + ": remote stores aren't supported by this "
                           'command yet (s3://team-bucket/mendwright/).')
    assert 'aws s3 cp s3://team-bucket/mendwright/overrides.json .mendwright/overrides.json' in line
    assert 'Traceback' not in err + out and 'no store at' not in err
    if command.startswith('lists '):
        assert 'mendwright lists --store .mendwright/overrides.json' in line
    else:
        assert 'then mendwright ' + command + ' --store .mendwright/overrides.json' in line
    # Read without a backend: no SDK, no request.
    assert 'mendwright.backends.s3' not in sys.modules or 'boto3' in sys.modules


@pytest.mark.parametrize('argv, command, code', COMMANDS, ids=[c[1] for c in COMMANDS])
def test_a_command_given_an_https_store_by_the_environment_says_how_to_download_it(
        argv, command, code, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, '_on_windows', lambda: False)      # the curl form
    monkeypatch.setenv('MENDWRIGHT_STORE', 'https://store.example/team/overrides.json')
    monkeypatch.setenv('MENDWRIGHT_JOURNAL_DIR', str(tmp_path / 'journals'))
    assert cli.main(argv) == code
    [line] = _lines(capsys.readouterr().err)
    assert "remote stores aren't supported by this command yet (https://store.example/team/)" in line
    assert ('curl -fsS -o journals/overrides.json -H "Authorization: Bearer '
            '$MENDWRIGHT_STORE_TOKEN" https://store.example/team/overrides.json') in line


@pytest.mark.parametrize('windows, token_file, how', [
    (False, False, 'curl -fsS -o .mendwright/overrides.json -H "Authorization: Bearer '
                   '$MENDWRIGHT_STORE_TOKEN" https://store.example/team/overrides.json'),
    (False, True, 'curl -fsS -o .mendwright/overrides.json -H "Authorization: Bearer '
                  '$(cat "$MENDWRIGHT_STORE_TOKEN_FILE")" https://store.example/team/overrides.json'),
    (True, False, 'Invoke-WebRequest -UseBasicParsing -Headers @{Authorization = "Bearer '
                  '$env:MENDWRIGHT_STORE_TOKEN"} -OutFile .mendwright/overrides.json '
                  'https://store.example/team/overrides.json'),
    (True, True, 'Invoke-WebRequest -UseBasicParsing -Headers @{Authorization = "Bearer '
                 '$((Get-Content -Raw $env:MENDWRIGHT_STORE_TOKEN_FILE).Trim())"} -OutFile '
                 '.mendwright/overrides.json https://store.example/team/overrides.json'),
], ids=['posix', 'posix, token file', 'windows', 'windows, token file'])
def test_the_https_download_reads_the_token_as_mendwright_does_in_this_shell(
        monkeypatch, windows, token_file, how):
    # A token kept in a file (a Docker or Kubernetes secret) leaves MENDWRIGHT_STORE_TOKEN
    # empty, which sent 'Bearer ' and got 401; cmd.exe and PowerShell don't read $NAME.
    if token_file:
        monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN_FILE', '/run/secrets/SECRETPATH')
    assert cli._https_download('https://store.example/team/', '.mendwright/overrides.json',
                               windows=windows) == how
    assert 'SECRETPATH' not in how


def test_notify_says_it_too_and_fails_only_when_asked(capsys):
    assert cli.main(['notify', '--all', '--store', 's3://team-bucket/x/']) == 0
    [line] = _lines(capsys.readouterr().err)
    assert line.startswith("mendwright notify: remote stores aren't supported by this command "
                           'yet (s3://team-bucket/x/).')
    assert cli.main(['notify', '--all', '--fail-on-error', '--store', 's3://team-bucket/x/']) == 1


@pytest.mark.parametrize('url', [
    'https://user:SECRETVALUE@store.example/x/', 'https://store.example/x/?token=SECRETVALUE',
    'gs://SECRETVALUE/x/',
])
def test_a_store_url_mendwright_refuses_is_one_line_that_does_not_quote_it(url, capsys):
    assert cli.main(['report', '--store', url]) == 1
    out, err = capsys.readouterr()
    [line] = _lines(err)
    assert line.startswith('mendwright report: ') and 'SECRETVALUE' not in err + out
    assert 'Traceback' not in err


def test_memory_is_a_store_no_command_can_read(capsys):
    assert cli.main(['report', '--store', 'memory:']) == 1
    [line] = _lines(capsys.readouterr().err)
    assert 'memory: is a store in the memory of one process' in line


def test_a_local_path_is_read_as_before(capsys, tmp_path):
    assert cli.main(['report', '--store', str(tmp_path / 'nope.json')]) == 1
    [line] = _lines(capsys.readouterr().err)
    assert line.startswith('mendwright report: no store at ')


def test_doctor_names_a_remote_store_and_does_not_contact_it(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv('MENDWRIGHT_STORE', 's3://team-bucket/mendwright/')
    monkeypatch.setenv('MENDWRIGHT_STORE_READ_ONLY', '1')
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert '  s3://team-bucket/mendwright/\n' in out
    assert 'doctor does not contact it' in out and 'read-only worker' in out
    assert 'the journal of this machine: ' + str(tmp_path / '.mendwright' / 'journal.jsonl') in out
    assert cli.main(['doctor', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['store']['remote'] is True and data['store']['path'] == 's3://team-bucket/mendwright/'
    assert data['env']['MENDWRIGHT_STORE'] == 's3://team-bucket/mendwright/'
    assert data['env']['MENDWRIGHT_STORE_READ_ONLY'] == '1'


def test_doctor_hides_a_store_url_it_refuses(capsys, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_STORE', 'https://user:SECRETVALUE@store.example/x/')
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out
    assert 'SECRETVALUE' not in out
    assert 'MENDWRIGHT_STORE is a store URL mendwright refuses' in out
    assert 'MENDWRIGHT_STORE=(a store URL mendwright refuses, value hidden)' in out
    assert cli.main(['doctor', '--json']) == 0
    assert 'SECRETVALUE' not in capsys.readouterr().out
