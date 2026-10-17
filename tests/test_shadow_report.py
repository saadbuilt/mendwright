"""mendwright report names the shadow root a selector runs in (see mendwright.shadow), in its
text, Markdown, JSON and HTML."""

import json

from mendwright.store import load_store

HOST = 'my-app[id="root"]'


# --- mendwright report -------------------------------------------------------------------------------

def test_the_report_says_which_shadow_root_a_heal_runs_in(tmp_path, capsys):
    from mendwright import cli
    path = tmp_path / 'o.json'
    store = load_store(None, str(path))
    store.add_candidate('save', HOST + ' >> inner-box >> #b', source='heal',
                        shadow_hosts=[HOST, 'inner-box'])
    assert cli.main(['report', '--store', str(path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    at = [i for i, line in enumerate(lines) if HOST + ' >> inner-box >> #b' in line][0]
    assert lines[at + 1].strip() == 'inside the shadow root of inner-box inside ' + HOST
    assert cli.main(['report', '--store', str(path), '--format', 'md']) == 0
    assert 'inside the shadow root of `inner-box` inside `' + HOST + '`' in capsys.readouterr().out
    assert cli.main(['report', '--store', str(path), '--format', 'json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data['keys'][0]['candidates'][0]['shadow'] == [HOST, 'inner-box']
    assert cli.main(['report', '--store', str(path), '--format', 'html']) == 0
    assert 'inside the shadow root of inner-box inside my-app[id=&quot;root&quot;]' in (
        capsys.readouterr().out)
