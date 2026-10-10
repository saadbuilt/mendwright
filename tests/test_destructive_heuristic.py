"""The facade treats pay/submit/confirm-like keys as destructive unless destructive=False
is passed. The check runs on every call and is never saved as a flag."""

import json

import pytest

import mendwright
from mendwright import AttentionRequired, HealCoordinator, load_store, resolve, resolve_with_heal
from mendwright.facade import _key_words

BROKEN = "//button[@id='gone']"


def _page(make_page, label='Go'):
    return make_page("<form><button id='b'>" + label + "</button></form>")


def _refused(healer, key, **kw):
    with pytest.raises(AttentionRequired) as info:
        healer.locator(key, BROKEN, role='button', timeout_ms=0, poll_ms=0, **kw)
    return str(info.value)


@pytest.mark.parametrize('key,word', [
    ('pay', 'pay'), ('payment_method', 'payment'), ('buy-now', 'buy'),
    ('checkoutButton', 'checkout'), ('confirm.email', 'confirm'), ('submitForm', 'submit'),
    ('btn_delete', 'delete'), ('purchase', 'purchase'), ('placeOrder', 'place order'),
    ('order_now', 'order now'), ('COMPLETE_ORDER', 'complete order'),
])
def test_a_destructive_word_in_the_key_blocks_the_heal(key, word, tmp_path, make_page,
                                                       stub_provider):
    provider = stub_provider("//button[@id='b']")
    healer = mendwright.open(_page(make_page), provider=provider, store_path=str(tmp_path / 'o.json'))
    message = _refused(healer, key)
    assert "its key contains " + repr(word) in message
    assert 'destructive=False' in message
    assert 'destructive control' in message and 'never auto-heals' in message
    assert provider.bundles == []


@pytest.mark.parametrize('key', ['payload', 'display', 'buyer', 'confirmation', 'submitted',
                                 'order', 'go', 'accept_terms', 'deleted_items'])
def test_similar_words_do_not_count(key, tmp_path, make_page, stub_provider):
    provider = stub_provider("//button[@id='b']")
    healer = mendwright.open(_page(make_page), provider=provider, store_path=str(tmp_path / 'o.json'))
    loc = healer.locator(key, BROKEN, role='button', timeout_ms=0, poll_ms=0)
    assert loc.count() == 1


def test_key_words_split_on_separators_and_camel_case():
    assert _key_words('placeOrder_btn-2.x') == ['place', 'order', 'btn', '2', 'x']
    assert _key_words('HTTPSubmit') == ['http', 'submit']


def test_expected_text_counts(tmp_path, make_page, stub_provider):
    provider = stub_provider("//button[@id='b']")
    healer = mendwright.open(_page(make_page), provider=provider, store_path=str(tmp_path / 'o.json'))
    message = _refused(healer, 'go', text='Pay now')
    assert "its text 'Pay now' contains 'pay'" in message


@pytest.mark.parametrize('label,word', [('Place order', 'place order'), ('Buy now', 'buy')])
def test_the_live_label_is_remembered_for_the_next_run(label, word, tmp_path, make_page,
                                                       stub_provider):
    sp = str(tmp_path / 'o.json')
    first = mendwright.open(_page(make_page, label), provider=stub_provider(None), store_path=sp)
    first.locator('go_on', "//button[@id='b']", timeout_ms=0, poll_ms=0)
    saved = json.loads(open(sp, encoding='utf-8').read())['selectors']['go_on']
    assert saved['expectedText'] == [label]
    assert 'destructive' not in saved                 # the flag itself is never saved

    # Next run: the button's id changed, so the selector misses and a heal is needed.
    provider = stub_provider("//button")
    second = mendwright.open(make_page("<form><button id='other'>" + label + "</button></form>"),
                             provider=provider, store_path=sp)
    with pytest.raises(AttentionRequired) as info:
        second.locator('go_on', "//button[@id='b']", timeout_ms=0, poll_ms=0)
    assert "its text " + repr(label) + " contains " + repr(word) in str(info.value)
    assert provider.bundles == []


@pytest.mark.parametrize('button', [
    "<button id='b' aria-label='Delete account'><svg></svg></button>",
    "<button id='b' aria-label='Delete account'>&#215;</button>",
    "<button id='b' title='Delete account'><svg></svg></button>",
    "<button id='b'><img src='t.png' alt='Delete account'></button>",
])
def test_an_attribute_label_is_remembered_for_the_next_run(button, tmp_path, make_page,
                                                           stub_provider):
    sp = str(tmp_path / 'o.json')
    first = mendwright.open(make_page('<form>' + button + '</form>'),
                            provider=stub_provider(None), store_path=sp)
    first.locator('row_action', "//button[@id='b']", timeout_ms=0, poll_ms=0)
    assert first.store.meta('row_action')['expectedText'] == ['Delete account']

    provider = stub_provider('//button')
    moved = button.replace("id='b'", "id='moved'")
    second = mendwright.open(make_page('<form>' + moved + '</form>'), provider=provider,
                             store_path=sp)
    with pytest.raises(AttentionRequired, match="its text 'Delete account' contains 'delete'"):
        second.locator('row_action', "//button[@id='b']", timeout_ms=0, poll_ms=0)
    assert provider.bundles == []


def test_a_harmless_attribute_label_is_not_remembered(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    first = mendwright.open(make_page("<form><button id='b' aria-label='Close'>x</button></form>"),
                            provider=stub_provider(None), store_path=sp)
    first.locator('dismiss', "//button[@id='b']", timeout_ms=0, poll_ms=0)
    assert first.store.meta('dismiss')['expectedText'] == []
    provider = stub_provider("//button[@id='moved']")
    second = mendwright.open(make_page("<form><button id='moved' aria-label='Close'>x</button>"
                                       "</form>"), provider=provider, store_path=sp)
    second.locator('dismiss', "//button[@id='b']", timeout_ms=0, poll_ms=0).click()
    assert len(provider.bundles) == 1


def test_a_submit_input_value_counts_as_its_label(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    page = make_page("<form><input type='submit' id='s' value='Complete order'></form>")
    healer = mendwright.open(page, provider=stub_provider(None), store_path=sp)
    healer.locator('finish', "//input[@id='s']", timeout_ms=0, poll_ms=0)
    assert healer.store.meta('finish')['expectedText'] == ['Complete order']


def test_harmless_labels_and_non_controls_are_not_saved(tmp_path, make_page, stub_provider):
    page = make_page("<form><button id='b'>Save</button><p id='p'>Delete your account</p></form>")
    healer = mendwright.open(page, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    healer.locator('save', "//button[@id='b']", timeout_ms=0, poll_ms=0)
    healer.locator('notice', "//p[@id='p']", timeout_ms=0, poll_ms=0)
    assert healer.store.meta('save')['expectedText'] == []
    assert healer.store.meta('notice')['expectedText'] == []


def test_destructive_false_opts_out_and_is_remembered(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    provider = stub_provider("//button[@id='b']")
    healer = mendwright.open(_page(make_page), provider=provider, store_path=sp)
    loc = healer.locator('submit_search', BROKEN, role='button', destructive=False,
                         timeout_ms=0, poll_ms=0)
    assert loc.count() == 1
    assert len(provider.bundles) == 1

    # Next run: the page changed again, so even the heal misses; destructive= is omitted.
    provider = stub_provider("//button[@id='c']")
    again = mendwright.open(make_page("<form><button id='c'>Go</button></form>"),
                            provider=provider, store_path=sp)
    loc = again.locator('submit_search', BROKEN, role='button', timeout_ms=0, poll_ms=0)
    assert loc.count() == 1
    assert len(provider.bundles) == 1


def test_destructive_false_cannot_override_destructive_keys(tmp_path, make_page, stub_provider):
    provider = stub_provider("//button[@id='b']")
    healer = mendwright.open(_page(make_page), provider=provider, store_path=str(tmp_path / 'o.json'),
                             destructive_keys={'pay'})
    message = _refused(healer, 'pay', destructive=False)
    assert 'is marked as a destructive control' in message
    assert provider.bundles == []


def test_the_low_level_api_does_not_apply_the_word_list(tmp_path, make_page, stub_provider):
    # HealCoordinator users decide destructive themselves (seed, destructive_keys).
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_meta('submit', role='button')
    store.add_candidate('submit', BROKEN, source='code')
    page = _page(make_page)
    provider = stub_provider("//button[@id='b']")
    coord = HealCoordinator(store, provider=provider, html_provider=page.content,
                            inventory_provider=lambda: [], guard=lambda: True)
    loc = resolve_with_heal(lambda: resolve(store, page, 'submit', timeout_ms=0),
                            coord, 'submit', store.meta('submit'), {})
    assert loc.count() == 1


# --- a neutral key is never healed onto a destructive-looking control ------------------------

CONTINUE_V1 = "<form><button id='continue'>Continue</button></form>"
PAY = "//button[@id='p']"


def _neutral_heal(make_page, stub_provider, tmp_path, target_html, xpath, **kw):
    """'next_step' matched #continue on v1; on v2 the provider proposes ``xpath``."""
    sp = str(tmp_path / 'o.json')
    first = mendwright.open(make_page(CONTINUE_V1), provider=stub_provider(None), store_path=sp)
    first.locator('next_step', '#continue', role='button', timeout_ms=0, poll_ms=0, **kw)
    page = make_page('<form>' + target_html + '</form>')
    provider = stub_provider(xpath)
    heal = mendwright.open(page, provider=provider, store_path=sp)
    return heal, page, provider


@pytest.mark.parametrize('target, xpath, kw', [
    ("<button id='p'>Pay now</button>", PAY, {}),
    ("<button id='p'>Pay now</button>", PAY, {'text': 'Continue'}),
    ("<input type='submit' id='p' value='Place order'>", "//input[@id='p']", {}),
    ("<button id='p' aria-label='Delete account'>&#215;</button>", PAY, {}),
    ("<button id='p' title='Delete account'><svg></svg></button>", PAY, {}),
    ("<a id='p' href='#'><img alt='Buy now' src='b.png'></a>", "//a[@id='p']", {}),
])
def test_a_neutral_key_is_not_healed_onto_a_destructive_label(target, xpath, kw, tmp_path,
                                                             make_page, stub_provider):
    role = 'link' if xpath.startswith('//a') else 'button'
    sp = str(tmp_path / 'o.json')
    first = mendwright.open(make_page("<form><a id='continue' href='#'>Continue</a>"
                                      "<button id='go'>Continue</button></form>"),
                            provider=stub_provider(None), store_path=sp)
    start = "//a[@id='continue']" if role == 'link' else "//button[@id='go']"
    first.locator('next_step', start, role=role, timeout_ms=0, poll_ms=0, **kw)
    page = make_page('<form>' + target + '</form>')
    provider = stub_provider(xpath)
    heal = mendwright.open(page, provider=provider, store_path=sp)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('next_step', start, role=role, timeout_ms=0, poll_ms=0, **kw).click()
    message = str(info.value)
    assert 'looks destructive' in message and 'destructive=False' in message
    assert len(provider.bundles) == 2                       # the proposal and one re-prompt
    assert 'looks destructive' in provider.bundles[1]['rejectReason']
    assert page.actions == []
    assert [c['source'] for c in heal.store.candidates('next_step')] == ['code']
    saved = json.loads(open(sp, encoding='utf-8').read())['selectors']['next_step']
    assert [c['source'] for c in saved['candidates']] == ['code']


def test_destructive_false_still_heals_onto_a_pay_button(tmp_path, make_page, stub_provider):
    heal, page, provider = _neutral_heal(make_page, stub_provider, tmp_path,
                                         "<button id='p'>Pay now</button>", PAY,
                                         destructive=False)
    heal.locator('next_step', '#continue', role='button', timeout_ms=0, poll_ms=0).click()
    assert page.actions[-1][:2] == ('xpath=' + PAY, 'click')
    assert len(provider.bundles) == 1


def test_a_neutral_target_still_heals(tmp_path, make_page, stub_provider):
    heal, page, provider = _neutral_heal(make_page, stub_provider, tmp_path,
                                         "<button id='c2'>Continue</button>",
                                         "//button[@id='c2']")
    heal.locator('next_step', '#continue', role='button', timeout_ms=0, poll_ms=0).click()
    assert page.actions[-1][:2] == ("xpath=//button[@id='c2']", 'click')
    assert len(provider.bundles) == 1


def test_dry_run_reports_a_destructive_target_as_invalid(tmp_path, make_page, stub_provider,
                                                         caplog):
    page = make_page("<form><button id='p'>Pay now</button></form>")
    heal = mendwright.open(page, provider=stub_provider(PAY), store_path=str(tmp_path / 'o.json'),
                           dry_run=True)
    with caplog.at_level('INFO', logger='mendwright'):
        with pytest.raises(AttentionRequired, match='dry-run'):
            heal.locator('next_step', '#continue', role='button', timeout_ms=0, poll_ms=0)
    lines = [r.getMessage() for r in caplog.records if 'heal dry-run' in r.getMessage()]
    assert len(lines) == 1 and 'valid=False' in lines[0]


def test_the_low_level_coordinator_refuses_a_destructive_target(tmp_path, make_page,
                                                                stub_provider):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.set_meta('next_step', role='button')
    store.add_candidate('next_step', BROKEN, source='code')
    page = make_page("<form><button id='p'>Pay now</button></form>")
    provider = stub_provider(PAY)
    coord = HealCoordinator(store, provider=provider, html_provider=page.content,
                            inventory_provider=lambda: [], guard=lambda: True)
    with pytest.raises(AttentionRequired, match='looks destructive'):
        resolve_with_heal(lambda: resolve(store, page, 'next_step', timeout_ms=0),
                          coord, 'next_step', store.meta('next_step'), {})
    assert len(provider.bundles) == 2
    assert store.status('next_step', PAY) is None

    # The low-level opt-outs: refuseDestructiveTarget=False in meta, or destructive=False.
    meta = dict(store.meta('next_step'), refuseDestructiveTarget=False)
    assert coord.try_heal('next_step', meta, [], {}) == PAY
    other = load_store(None, str(tmp_path / 'o2.json'))
    other.set_meta('go', role='button', destructive=False)
    coord = HealCoordinator(other, provider=stub_provider(PAY), html_provider=page.content,
                            inventory_provider=lambda: [], guard=lambda: True)
    assert coord.try_heal('go', other.meta('go'), [], {}) == PAY


def test_the_live_check_refuses_a_target_that_turned_destructive(tmp_path, make_page,
                                                                 stub_provider):
    # The snapshot has a harmless button. While the model works, the page re-renders and
    # the same xpath now matches a delete button.
    page = make_page("<form><button data-step='primary'>Save draft</button></form>")
    provider = stub_provider("//*[@data-step='primary']")
    real_propose = provider.propose

    def propose(bundle):
        page.set_html("<form><button data-step='primary'>Delete account</button></form>")
        return real_propose(bundle)
    provider.propose = propose
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    with pytest.raises(AttentionRequired, match='looks destructive'):
        heal.locator('save_draft', '#save', role='button', timeout_ms=0, poll_ms=0).click()
    assert page.actions == []
    assert [c['source'] for c in heal.store.candidates('save_draft')] == ['code']


# --- a heal saved earlier that points at a destructive control --------------------------------

def _saved_heal(sp, expected_text=None):
    entry = {'expectedRole': 'button', 'candidates': [
        {'xpath': '#continue', 'source': 'code', 'addedAt': None, 'lastSucceededAt': None,
         'status': 'active'},
        {'xpath': 'xpath=' + PAY, 'source': 'heal', 'addedAt': None,
         'lastSucceededAt': '2026-10-01T00:00:00+00:00', 'status': 'active'}]}
    if expected_text is not None:
        entry['expectedText'] = expected_text
    with open(sp, 'w', encoding='utf-8') as fh:
        json.dump({'schema': 2, 'selectors': {'next_step': entry}}, fh)


@pytest.mark.parametrize('expected_text, text, why', [
    (None, None, "its label 'Pay now' contains 'pay'"),
    (['Continue'], 'Continue', "its label 'Pay now' contains 'pay'"),
    (['Pay now'], None, "its text 'Pay now' contains 'pay'"),
])
def test_a_saved_heal_on_a_destructive_control_is_quarantined(expected_text, text, why, tmp_path,
                                                             make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    _saved_heal(sp, expected_text)
    page = make_page("<form><button id='p'>Pay now</button></form>")
    provider = stub_provider(PAY)
    heal = mendwright.open(page, provider=provider, store_path=sp)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('next_step', '#continue', role='button', text=text,
                     timeout_ms=0, poll_ms=0).click()
    message = str(info.value)
    assert why in message and 'quarantined that selector' in message
    assert page.actions == [] and provider.bundles == []
    assert heal.store.status('next_step', 'xpath=' + PAY) == 'quarantined'
    saved = json.loads(open(sp, encoding='utf-8').read())['selectors']['next_step']
    statuses = {c['xpath']: c['status'] for c in saved['candidates']}
    assert statuses['xpath=' + PAY] == 'quarantined'
    with open(tmp_path / 'journal.jsonl', encoding='utf-8') as fh:
        events = [json.loads(line) for line in fh]
    assert events[-1]['event'] == 'heal.quarantine'


def test_a_saved_heal_is_kept_when_the_key_opted_out(tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    _saved_heal(sp)
    page = make_page("<form><button id='p'>Pay now</button></form>")
    heal = mendwright.open(page, provider=stub_provider(None), store_path=sp)
    heal.locator('next_step', '#continue', role='button', destructive=False,
                 timeout_ms=0, poll_ms=0).click()
    assert page.actions[-1][:2] == ('xpath=' + PAY, 'click')
