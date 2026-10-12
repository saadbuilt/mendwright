"""The cookies= preset (mendwright/interruptions.py) against small samples of the markup
each consent manager writes, and the generic fallback against banners no manager made.

Each sample is checked through the code a Healer runs (Healer._interruption_target on a
StandInPage), so the probe, the order of the selectors and the narrowing to one visible
match are covered too. The samples are trimmed copies of the managers' banners: their ids,
classes, roles and button labels, without scripts and styles.
"""

import lxml.html
import pytest

import mendwright
from mendwright.demo import StandInPage
from mendwright.interruptions import (COOKIE_BANNER_KEY, MANAGERS, Interruption,
                                      cookie_preset, generic_xpath, make_interruption,
                                      nth_targets, preset_selectors)
from mendwright.selectors import InvalidSelector

PAGE = ('<!doctype html><html><head><title>Shop</title></head><body>'
        '<main><h1>Sign up</h1><form><input id="email"><button id="next">Next</button>'
        '</form></main>{banner}</body></html>')

SAMPLES = {
    'OneTrust': ("""
<div id="onetrust-consent-sdk"><div id="onetrust-banner-sdk" class="otFlat" tabindex="0"
  role="region" aria-label="Cookie banner">
 <div id="onetrust-policy"><div id="onetrust-policy-text">We use cookies to improve your
  experience.</div></div>
 <div id="onetrust-button-group-parent"><div id="onetrust-button-group">
  <button id="onetrust-pc-btn-handler">Cookie Settings</button>
  <button id="onetrust-reject-all-handler">Reject All</button>
  <button id="onetrust-accept-btn-handler">Accept All Cookies</button></div></div>
 <div id="onetrust-close-btn-container"><button class="onetrust-close-btn-handler
  onetrust-close-btn-ui banner-close-button ot-close-icon" aria-label="Close"></button></div>
</div></div>""", 'Reject All', 'Accept All Cookies'),
    'Cookiebot': ("""
<div id="CybotCookiebotDialog" name="CybotCookiebotDialog" role="dialog" aria-modal="true"
  aria-label="Cookiebot" class="CybotEdge CybotCookiebotDialogActive">
 <div id="CybotCookiebotDialogBody"><h2 id="CybotCookiebotDialogBodyContentTitle">This website
  uses cookies</h2></div>
 <div id="CybotCookiebotDialogBodyButtonsWrapper">
  <button id="CybotCookiebotDialogBodyButtonDecline" class="CybotCookiebotDialogBodyButton">Deny</button>
  <button id="CybotCookiebotDialogBodyLevelButtonCustomize" class="CybotCookiebotDialogBodyButton">Customize</button>
  <button id="CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"
   class="CybotCookiebotDialogBodyButton">Allow all</button></div>
</div>""", 'Deny', 'Allow all'),
    'Didomi': ("""
<div id="didomi-host" class="didomi-host"><div id="didomi-notice" class="didomi-popup-notice"
  role="dialog" aria-labelledby="didomi-notice-title">
 <h1 id="didomi-notice-title">We care about your privacy</h1>
 <p>We and our partners store and access information on your device.</p>
 <div class="didomi-buttons">
  <button id="didomi-notice-learn-more-button" class="didomi-components-button">Learn more</button>
  <button id="didomi-notice-disagree-button" class="didomi-components-button">Disagree</button>
  <button id="didomi-notice-agree-button" class="didomi-components-button">Agree and close</button>
 </div></div></div>""", 'Disagree', 'Agree and close'),
    'TrustArc': ("""
<div id="truste-consent-track" role="dialog" aria-label="Cookie Consent Banner">
 <div id="truste-consent-content"><div id="truste-consent-text">This site uses cookies.</div>
 <div id="truste-consent-buttons">
  <button id="truste-consent-required" class="truste-button1">Required Only</button>
  <button id="truste-show-consent" class="truste-button2">Cookie Preferences</button>
  <button id="truste-consent-button" class="truste-button1">Accept All</button>
 </div></div></div>""", 'Required Only', 'Accept All'),
    'Usercentrics': ("""
<div id="usercentrics-root"><div data-testid="uc-default-banner" role="dialog"
  aria-label="Privacy Settings">
 <p>We use cookies and similar technologies on our website.</p>
 <button data-testid="uc-more-button">Settings</button>
 <button data-testid="uc-deny-all-button">Deny</button>
 <button data-testid="uc-accept-all-button">Accept All</button>
</div></div>""", 'Deny', 'Accept All'),
    'Quantcast Choice': ("""
<div id="qc-cmp2-container" class="qc-cmp2-container"><div id="qc-cmp2-ui" role="dialog"
  aria-label="qc-cmp2-ui">
 <div class="qc-cmp2-summary-section"><h2>We value your privacy</h2></div>
 <div class="qc-cmp2-summary-buttons">
  <button mode="secondary" size="large" class=" css-47sehv"><span>MORE OPTIONS</span></button>
  <button mode="secondary" size="large" class=" css-47sehv"><span>DISAGREE</span></button>
  <button mode="primary" size="large" class=" css-47sehv"><span>AGREE</span></button>
 </div></div></div>""", 'DISAGREE', 'AGREE'),
    'Osano': ("""
<div class="osano-cm-window"><div class="osano-cm-dialog osano-cm-dialog--type_bar"
  role="dialog" aria-label="Cookie Consent Banner">
 <span class="osano-cm-content">This website utilizes technologies such as cookies.</span>
 <div class="osano-cm-dialog__buttons">
  <button class="osano-cm-denyAll osano-cm-buttons__button osano-cm-button">Deny</button>
  <button class="osano-cm-accept-all osano-cm-buttons__button osano-cm-button">Accept</button>
 </div>
 <button class="osano-cm-dialog__close osano-cm-close" aria-label="Close this dialog"
  role="button"></button>
</div></div>""", 'Deny', 'Accept'),
    'cookieconsent': ("""
<div role="dialog" aria-live="polite" aria-label="cookieconsent"
  aria-describedby="cookieconsent:desc" class="cc-window cc-banner cc-type-opt-in cc-bottom">
 <span id="cookieconsent:desc" class="cc-message">This website uses cookies.
  <a aria-label="learn more about cookies" role="button" class="cc-link"
   href="/cookie-policy">Learn more</a></span>
 <div class="cc-compliance cc-highlight">
  <a aria-label="deny cookies" role="button" tabindex="0" class="cc-btn cc-deny">Decline</a>
  <a aria-label="allow cookies" role="button" tabindex="0" class="cc-btn cc-allow">Allow cookies</a>
 </div></div>""", 'Decline', 'Allow cookies'),
    'cookieconsent v3 (orestbida)': ("""
<div id="cc-main"><div class="cm-wrapper"><div class="cm cm--box" role="dialog"
  aria-modal="true" aria-labelledby="cm__title">
 <h2 id="cm__title" class="cm__title">We use cookies</h2>
 <div class="cm__btns"><div class="cm__btn-group">
  <button type="button" class="cm__btn" data-role="all">Accept all</button>
  <button type="button" class="cm__btn" data-role="necessary">Reject all</button></div>
  <button type="button" class="cm__btn cm__btn--secondary" data-role="show">Manage preferences</button>
 </div></div></div></div>""", 'Reject all', 'Accept all'),
    'CookieYes': ("""
<div class="cky-consent-container cky-box-bottom-left" tabindex="0" aria-label="We value your privacy"
  role="region"><div class="cky-consent-bar" data-cky-tag="notice">
 <p class="cky-title" role="heading" aria-level="1" data-cky-tag="title">We value your privacy</p>
 <div class="cky-notice-btn-wrapper" data-cky-tag="notice-buttons">
  <button class="cky-btn cky-btn-customize" aria-label="Customise" data-cky-tag="settings-button">Customise</button>
  <button class="cky-btn cky-btn-reject" aria-label="Reject All" data-cky-tag="reject-button">Reject All</button>
  <button class="cky-btn cky-btn-accept" aria-label="Accept All" data-cky-tag="accept-button">Accept All</button>
 </div>
 <button class="cky-banner-btn-close" data-cky-tag="close-button" aria-label="Close"></button>
</div></div>""", 'Reject All', 'Accept All'),
    'Cookie Law Info': ("""
<div id="cookie-law-info-bar" data-nosnippet="true"><span>This website uses cookies.
 <a role="button" id="cookie_action_close_header_reject" class="medium cli-plugin-button
  cli-plugin-main-button cookie_action_close_header_reject cli_action_button">Reject</a>
 <a role="button" id="cookie_action_close_header" class="medium cli-plugin-button
  cli-plugin-main-button cookie_action_close_header cli_action_button">Accept</a>
</span></div>""", 'Reject', 'Accept'),
    'Complianz': ("""
<div class="cmplz-cookiebanner cmplz-show banner-1 optin cmplz-bottom-right" aria-modal="true"
  role="dialog" aria-labelledby="cmplz-header-1-optin" aria-describedby="cmplz-message-1-optin">
 <div class="cmplz-header"><div class="cmplz-title" id="cmplz-header-1-optin">Manage Cookie
  Consent</div><div class="cmplz-close" tabindex="0" role="button" aria-label="Close dialog"></div></div>
 <div class="cmplz-body"><div class="cmplz-message" id="cmplz-message-1-optin">To provide the
  best experiences, we use technologies like cookies.</div></div>
 <div class="cmplz-buttons"><button class="cmplz-btn cmplz-accept">Accept</button>
  <button class="cmplz-btn cmplz-deny">Deny</button>
  <button class="cmplz-btn cmplz-view-preferences">View preferences</button></div>
</div>""", 'Deny', 'Accept'),
}


def _healer(banner, tmp_path):
    page = StandInPage(PAGE.format(banner=banner))
    return mendwright.open(page, provider=None, store_path=str(tmp_path / 'o.json')), page


def _picked(heal, page, item):
    """What the healer would act on: the element's own text (or aria-label), or None."""
    target = heal._interruption_target(item, heal.driver)
    if target is None:
        return None
    body = target[len('xpath='):] if target.startswith('xpath=') else target
    found = page._doc.xpath(body)
    assert len(found) == 1, target
    el = found[0]
    return ' '.join(el.text_content().split()) or el.get('aria-label') or el.get('value')


def test_every_listed_manager_has_a_sample():
    covered = {name.split(' (')[0] for name in SAMPLES}
    assert set(MANAGERS) <= covered


@pytest.mark.parametrize('name', list(SAMPLES))
def test_reject_clicks_the_managers_reject_button(name, tmp_path):
    banner, reject, _accept = SAMPLES[name]
    heal, page = _healer(banner, tmp_path)
    assert _picked(heal, page, cookie_preset('reject')) == reject


@pytest.mark.parametrize('name', list(SAMPLES))
def test_accept_clicks_the_managers_accept_button(name, tmp_path):
    banner, _reject, accept = SAMPLES[name]
    heal, page = _healer(banner, tmp_path)
    assert _picked(heal, page, cookie_preset('accept')) == accept


def test_a_manager_close_button_is_used_when_there_is_no_reject_button(tmp_path):
    banner = SAMPLES['OneTrust'][0].replace(
        '<button id="onetrust-reject-all-handler">Reject All</button>', '')
    heal, page = _healer(banner, tmp_path)
    assert _picked(heal, page, cookie_preset('reject')) == 'Close'


def test_a_hidden_reject_button_is_not_clicked(tmp_path):
    banner = SAMPLES['Cookiebot'][0].replace(
        'id="CybotCookiebotDialogBodyButtonDecline"',
        'id="CybotCookiebotDialogBodyButtonDecline" style="display:none"')
    heal, page = _healer(banner, tmp_path)
    assert _picked(heal, page, cookie_preset('reject')) is None


# --- the generic fallback -------------------------------------------------------------------

GENERIC = [
    # (banner, what reject picks, what accept picks)
    ('<section class="CookieNotice_root__x1" aria-label="Privacy notice"><p>We use cookies.</p>'
     '<button>Only necessary</button><button>Accept all</button></section>',
     'Only necessary', 'Accept all'),
    ('<div id="consent-bar"><p>Can we use cookies?</p><a href="#" role="button">Decline</a>'
     '<a href="#" role="button">Yes, I agree</a></div>', 'Decline', 'Yes, I agree'),
    ('<div class="gdpr-cookie-popup" role="dialog"><p>Cookies help us.</p>'
     '<button aria-label="Close banner">×</button><button>Allow</button></div>',
     '×', 'Allow'),     # the close icon, found by its aria-label
    ('<div class="cookie-box"><p>We use cookies.</p><input type="button" value="Deny">'
     '<input type="submit" value="Accept cookies"></div>', 'Deny', 'Accept cookies'),
    ('<div class="cookie-banner"><p>Cookies.</p><button>Accept and close</button>'
     '<button>Don’t allow</button><a href="/legal">Disclosure</a></div>', None, 'Accept and close'),
    ('<div class="cookie-banner"><p>Cookies.</p><button>Continue without accepting</button>'
     '<button>Disagree</button></div>', 'Continue without accepting', None),
]


@pytest.mark.parametrize('banner, reject, accept', GENERIC)
def test_the_generic_fallback(banner, reject, accept, tmp_path):
    heal, page = _healer(banner, tmp_path)
    assert _picked(heal, page, cookie_preset('reject')) == reject
    assert _picked(heal, page, cookie_preset('accept')) == accept


def test_a_page_wide_cookie_class_does_not_make_every_close_button_a_match(tmp_path):
    page_html = ('<html><body class="cookie-banner-open has-consent-bar"><main class="consent-'
                 'pending"><h1>Account</h1><button>Close account</button><button>Close</button>'
                 '<a href="#">Reject changes</a></main></body></html>')
    page = StandInPage(page_html)
    heal = mendwright.open(page, provider=None, store_path=str(tmp_path / 'o.json'))
    assert heal._interruption_target(cookie_preset('reject'), heal.driver) is None
    assert heal._interruption_target(cookie_preset('accept'), heal.driver) is None


def test_the_fallback_needs_a_cookie_or_consent_container():
    doc = lxml.html.fromstring('<div class="newsletter"><button>Close</button><button>Reject'
                               '</button></div>')
    assert doc.xpath(generic_xpath('reject')) == []


def test_the_preset_selectors_are_xpath_except_one_css_for_shadow_roots():
    for choice in ('reject', 'accept'):
        selectors = preset_selectors(choice)
        assert all(s.startswith('xpath=') for s in selectors[:-1])
        assert selectors[-1].startswith('[data-testid=')
        preset = cookie_preset(choice)
        assert preset.key == COOKIE_BANNER_KEY == 'cookie_banner'
        assert preset.action == 'click' and preset.preset == choice
        assert preset.probe.startswith('xpath=')
    with pytest.raises(ValueError, match='cookies="reject"'):
        preset_selectors('maybe')


def test_nth_targets():
    assert nth_targets('#a', 1) == ['#a']
    assert nth_targets('#a', 2) == []                  # CSS cannot be narrowed everywhere
    assert nth_targets('//a', 0) == []
    assert nth_targets('//a | //b', 3) == ['xpath=(//a | //b)[1]', 'xpath=(//a | //b)[2]',
                                           'xpath=(//a | //b)[3]']
    assert len(nth_targets('//a', 50)) == 5


def test_make_interruption_normalizes_and_checks():
    item = make_interruption('cookies', ['css=#accept', '#accept', '//button[@id="ok"]'],
                             actions=('click',))
    assert item == Interruption(key='cookies', selectors=('#accept', '//button[@id="ok"]'))
    with pytest.raises(InvalidSelector):
        make_interruption('cookies', '//button[', actions=('click',))
    assert make_interruption('m', '#x', role='textbox', actions=('click',)).roles == (
        'input', 'textarea')
