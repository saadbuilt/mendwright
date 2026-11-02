"""Per-key records across the processes of one host (mendwright.fleet.KeyRecords): a lease for
single-flight, the model attempts every process made for the key in the last hour, and the
model's declines per page signature, in <store folder>/keys/<hash>.json, written with a
compare-and-swap. Never in overrides.json."""

import json
import threading

import pytest

import mendwright
from mendwright import load_store
from mendwright.backends import MemoryBackend, Unavailable, key_record_name
from mendwright.fleet import KeyRecords, records_for


@pytest.fixture(params=['memory', 'file'])
def records(request, tmp_path):
    if request.param == 'memory':
        return KeyRecords(MemoryBackend())
    return records_for(load_store(None, str(tmp_path / 'overrides.json')))


def test_the_records_of_a_store_live_next_to_it(tmp_path):
    default = records_for(load_store(None, str(tmp_path / 'overrides.json')))
    assert default.name('pay') == key_record_name('pay')
    default.take('pay', 'sig', 'me', 100.0, 15)
    assert (tmp_path / key_record_name('pay')).is_file()
    # A store file of another name keeps its records apart, so two stores of one folder
    # never share a lease, a budget or a decline.
    other = records_for(load_store(None, str(tmp_path / 'o.json')))
    assert other.name('pay') == 'o.json.keys/' + key_record_name('pay')[len('keys/'):]
    assert records_for(load_store(None, str(tmp_path / 'o.json'), read_only=True)) is None


def test_a_lease_is_taken_renewed_and_given_back(records):
    assert records.look('k', 100.0) == ('none', None)
    assert records.take('k', 'sig', 'me', 100.0, 15) is True
    state, lease = records.look('k', 110.0)
    assert state == 'live' and lease == {'owner': 'me', 'sig': 'sig', 'at': 100.0, 'ttl': 15}
    assert records.take('k', 'sig', 'you', 110.0, 15) is False     # live and not yours
    assert records.renew('k', 'me', 112.0) is True
    assert records.look('k', 126.0)[0] == 'live'                   # 112 + 15
    assert records.renew('k', 'you', 113.0) is False
    records.release('k', 'you')                                    # not yours: nothing
    assert records.look('k', 113.0)[0] == 'live'
    records.release('k', 'me')
    assert records.look('k', 113.0) == ('none', None)


def test_an_expired_lease_is_taken_over_by_exactly_one(records):
    records.take('k', 'sig', 'dead', 0.0, 15)
    assert records.look('k', 100.0)[0] == 'expired'
    won, start = [], threading.Barrier(16)

    def take(i):
        start.wait()
        if records.take('k', 'sig', 'owner%d' % i, 100.0, 15):
            won.append(i)
    threads = [threading.Thread(target=take, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(won) == 1
    assert records.look('k', 100.0)[1]['owner'] == 'owner%d' % won[0]


def test_the_budget_of_a_key_is_shared(records):
    assert [records.spend('k', 3, 1000.0 + i) for i in range(4)] == [
        (True, 0), (True, 1), (True, 2), (False, 3)]
    assert records.spend('k', 3, 1000.0 + 3600.5) == (True, 2)     # the first left the hour
    assert records.spend('k', None, 5000.0)[0] is True             # no cap: always


def test_a_decline_is_kept_per_page_for_its_ttl(records):
    records.keep_decline('k', 'page-a', 'not_here', 'nothing fits', 100.0, 600)
    assert records.decline('k', 'page-a', 699.0) == {
        'code': 'not_here', 'reason': 'nothing fits', 'at': 100.0, 'until': 700.0}
    assert records.decline('k', 'page-b', 699.0) is None
    assert records.decline('k', 'page-a', 700.0) is None
    records.keep_decline('k', 'page-b', 'gate', 'nothing passed', 800.0, 600)
    record, _version = records.read('k')
    assert list(record['declines']) == ['page-b']                  # the expired one is dropped


def test_one_record_holds_the_lease_the_attempts_and_the_declines(tmp_path):
    records = records_for(load_store(None, str(tmp_path / 'overrides.json')))
    records.take('k', 'sig', 'me', 100.0, 15)
    records.spend('k', 3, 100.0)
    records.keep_decline('k', 'sig', 'not_here', 'nothing fits', 100.0, 600)
    record = json.loads((tmp_path / key_record_name('k')).read_text(encoding='utf-8'))
    assert record == {'schema': 1, 'key': 'k', 'attempts': [100.0],
                      'lease': {'owner': 'me', 'sig': 'sig', 'at': 100.0, 'ttl': 15},
                      'declines': {'sig': {'code': 'not_here', 'reason': 'nothing fits',
                                           'at': 100.0, 'until': 700.0}}}


def test_a_record_that_is_not_one_is_replaced_by_the_next_write(tmp_path):
    records = records_for(load_store(None, str(tmp_path / 'overrides.json')))
    path = tmp_path / key_record_name('k')
    path.parent.mkdir(parents=True)
    path.write_text('{"half": ', encoding='utf-8')
    assert records.read('k')[0] == {}
    assert records.take('k', 'sig', 'me', 1.0, 15) is True
    assert json.loads(path.read_text(encoding='utf-8'))['lease']['owner'] == 'me'


class Down(MemoryBackend):
    def get(self, name, *, if_none_match=None):
        raise Unavailable('down')


def test_records_that_cannot_be_reached_mean_no_coordination(tmp_path, make_page,
                                                             stub_provider, monkeypatch):
    """The heal goes on alone, under the caps of its process: no wait, no error."""
    import mendwright.fleet as fleet
    path = str(tmp_path / 'overrides.json')
    page = make_page("<form><button id='new'>Go</button></form>", url='https://shop.test/a')
    provider = stub_provider("//button[@id='new']")
    heal = mendwright.open(page, provider=provider, store_path=path, timeout_ms=0,
                           fingerprint=False)
    monkeypatch.setitem(fleet._RECORDS, fleet.store_name(heal.store), KeyRecords(Down()))
    heal.locator('go', '#old', role='button').click()
    assert len(provider.bundles) == 1 and page.actions[-1][1] == 'click'


def test_the_store_file_never_holds_coordination(tmp_path, make_page, stub_provider):
    path = tmp_path / 'overrides.json'
    page = make_page("<form><button id='new'>Go</button></form>", url='https://shop.test/a')
    heal = mendwright.open(page, provider=stub_provider("//button[@id='new']"),
                           store_path=str(path), timeout_ms=0, fingerprint=False)
    heal.locator('go', '#old', role='button').click()
    text = path.read_text(encoding='utf-8')
    assert 'lease' not in text and 'attempts' not in text and 'declines' not in text
    record = json.loads((tmp_path / key_record_name('go')).read_text(encoding='utf-8'))
    assert len(record['attempts']) == 1 and 'lease' not in record    # given back
