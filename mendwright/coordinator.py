"""The heal itself. When every candidate selector misses, HealCoordinator captures the
page, looks for the element the key's fingerprint describes (with fingerprint=True, no
model needed), else asks a provider for a fix, checks and saves it, and resolve_with_heal()
retries the step in the same call. A guard can block healing while a payment or registration is being
submitted, keys marked destructive are never healed, a key is not healed on a page where
it was never found (a change in the flow, see mendwright.flow), and a failed canary
quarantines the healed candidate. When a heal does not happen, the coordinator records a readable reason
(see ``explain``), and the AttentionRequired it raises tells the caller what to do next.
A list whose selectors all match nothing is never healed (see decline_list).
"""

import contextlib
import logging
import time

from . import fingerprint as _fingerprint
from .capture import build_bundle
from .client import DEFAULT_AUTH_HEADER, apply
from .exceptions import AttentionRequired
from .fit import fit_bundle, has_position
from .flow import flow_change_message, journal_event
from .journal import NullJournal
from .providers import GatewayProvider
from .validation import validate

logger = logging.getLogger('mendwright')


def _old_selectors(broken_candidates):
    """The selector strings of the candidates that stopped matching."""
    out = []
    for c in broken_candidates or ():
        x = c.get('xpath') if isinstance(c, dict) else c
        if x:
            out.append(str(x))
    return out


def _role_needed(selector_key):
    """The sentence that asks for role= when a key's expected role is unknown."""
    return ("does not know what kind of element '" + selector_key + "' is (its expected "
            "role is unknown), so it cannot check a repaired selector. Pass role= to "
            "heal.locator(), for example role='textbox' for a text field, role='button', "
            "role='checkbox', role='link' or role='combobox', or set expectedRole for the key "
            "in the seed file.")


# The provider name heals from the fingerprint tier get in the journal and the log.
FINGERPRINT_PROVIDER = 'fingerprint'
# The source of a key's own selector that matched inside an iframe and is now looked up
# there (see Healer.locator): no model and no new selector, only a new place.
FRAME_PROVIDER = 'frame'


def _no_fingerprint(selector_key):
    return ("'" + selector_key + "' has no fingerprint yet (mendwright records one each time "
            'its selector matches)')


def _destructive_message(selector_key):
    return ("'" + selector_key + "' is marked as a destructive control, and "
            "mendwright never auto-heals those, because a validated but wrong selector here "
            "could act on a live account. Fix the selector in your code or seed file by hand.")


def _list_missing_message(selector_key, count):
    """Why a list whose selectors all match nothing is not healed (see decline_list). count
    is how many elements the list had when its list fingerprint was recorded."""
    had = (str(count) + ' elements' if isinstance(count, int) and not isinstance(count, bool)
           and count >= 2 else 'two or more elements')
    return ("'" + selector_key + "' matched " + had + ' when its list fingerprint was recorded, '
            'and none of its selectors matches now. Either the list is empty at this point, or '
            "its selector broke: mendwright can't tell the two apart, since the items of an "
            "emptied list can show in another list, such as a cart's in Saved for later, so it "
            'never heals a list that matches nothing. If the selector broke, change it in your '
            'code.')


class HealCoordinator:
    """Runs one heal: capture, propose, validate, apply.

    Diagnostics go to the 'mendwright' logger, prefixed with 'mendwright: ': one WARNING
    line per applied heal and one per declined heal, so a heal shows on stderr even when
    the application configured no logging. Pass log=<callable taking a str> to send the
    same lines (without the prefix) somewhere else instead, e.g. log=print. clock is the
    monotonic clock used to time a heal; tests inject a fake one.

    The page guard: when the context passed to try_heal() names the current page
    (context['page']) and the pages the key was found on before (context['pages'], or
    context['lastPage']), and the current page is none of them, the heal is declined
    before any model call, because that looks like a step that moved to another page.
    heal_across_pages=True turns this off. entry is the function named in that message.
    A context without 'page' (the low-level default) is never guarded.

    fingerprint=True turns on the first tier: before any model call, the elements of the
    captured page are scored against the fingerprint stored for the key (meta
    'fingerprint', see mendwright.fingerprint), and a confident match is checked by the
    same gate as a model's proposal and applied, with provider name 'fingerprint'. It
    needs no provider and does not count toward the rate limit. It is off by default here;
    mendwright.open() turns it on.
    """

    def __init__(self, store, gateway=None, download_key=None, *, provider=None, http=None,
                 html_provider, inventory_provider, guard, dry_run=False, log=None,
                 auth_header=DEFAULT_AUTH_HEADER, driver=None, guardrails=None, journal=None,
                 clock=time.monotonic, heal_across_pages=False, entry='mendwright.open()',
                 fingerprint=False):
        self.store = store
        self.fingerprint = bool(fingerprint)
        # Direct-mode guardrails (rate limit + spend cap) and the event journal. In gateway
        # mode these live server-side, so they default off / no-op here; the facade wires
        # real ones for direct in-process providers.
        self.guardrails = guardrails
        self.journal = journal or NullJournal()
        # Optional live Driver: when set, every proposal must pass the live gate
        # (unique + visible on the current page) before it is committed. The facade wires
        # this automatically; the legacy low-level path gets its live check via the retry.
        self.driver = driver
        # The model seam. Back-compat: (gateway, download_key) still builds the default
        # GatewayProvider; pass provider=... for a direct/in-process AI instead.
        if provider is None:
            provider = GatewayProvider(gateway, download_key, http=http, auth_header=auth_header)
        self.provider = provider
        self.html_provider = html_provider
        self.inventory_provider = inventory_provider
        self.guard = guard
        self.dry_run = dry_run
        self.log = log
        self.clock = clock
        self.auth_header = auth_header
        self.heal_across_pages = bool(heal_across_pages)
        self.entry = entry
        # The selector of the iframe a heal runs in (see scoped()), saved with what it applies.
        self.frame_xpath = None
        # Set by mendwright.open() under MENDWRIGHT_DISABLE=1; stops the iframe follow too.
        self.disabled = False
        # selector_key -> applied_xpath awaiting an action-outcome canary. A dict (not a
        # single slot) so healing several fields on one page never drops an earlier
        # unproven selector's canary.
        self.pending = {}
        # selector_key -> the human-readable reason the last heal attempt did not apply a
        # fix. resolve_with_heal appends this to the AttentionRequired it surfaces, so a
        # person sees WHY (and what to do) instead of a bare "no matching element".
        self.heal_reasons = {}
        # Every heal this coordinator applied, oldest first: {'key', 'old', 'new',
        # 'confirmed', 'source'}. confirmed is None until the canary reports, then True or
        # False. source is 'fingerprint', 'model', or 'intent' for a key that had no selector.
        self.heals = []

    @contextlib.contextmanager
    def scoped(self, driver, frame_xpath):
        """Heal inside an iframe for the duration: capture, inventory and checks use
        ``driver``, a driver for the iframe's document, and an applied heal is saved with
        ``frame_xpath``, the iframe's selector in the page, so the resolver looks for it
        there. Restores the page's driver afterwards."""
        from .drivers import driver_inventory
        saved = (self.driver, self.html_provider, self.inventory_provider, self.frame_xpath)

        def html():
            try:
                return driver.outer_html() or ''
            except Exception:
                return ''

        self.driver = driver
        self.html_provider = html
        self.inventory_provider = lambda: driver_inventory(driver)
        self.frame_xpath = frame_xpath
        try:
            yield self
        finally:
            self.driver, self.html_provider, self.inventory_provider, self.frame_xpath = saved

    def record_frame(self, selector_key, old, new, frame):
        """Journal, remember and log that the key's own selector ``old`` matched only inside
        the iframe ``frame`` and is now saved there as ``new``. It counts as a heal (strict
        mode fails on it, and the canary checks it) with source 'frame'."""
        self.journal.applied(selector_key, xpath=new, old=[old], provider=FRAME_PROVIDER,
                             model=None, latency_ms=0, source=FRAME_PROVIDER, frame=frame)
        self.heal_reasons.pop(selector_key, None)
        self.heals.append({'key': selector_key, 'old': (old,), 'new': new, 'confirmed': None,
                           'source': FRAME_PROVIDER})
        self._emit(logging.WARNING, 'found ' + repr(selector_key) + ' inside the iframe '
                   + frame + ': ' + old + ' matches nothing on the page, so it is looked up '
                   'in that iframe from now on')

    def _emit(self, level, message):
        """Send one diagnostic line to log= when it was given, else to the logger."""
        sink = self.log
        if callable(sink):
            try:
                sink(message)
            except Exception:
                pass  # a broken log sink must not break a heal
        else:
            logger.log(level, 'mendwright: ' + message)

    def _decline(self, selector_key, reason, level=logging.WARNING, logged=None):
        """Record why a heal did not apply (for resolve_with_heal to surface), log it, and
        return the falsy value try_heal uses for 'not applied'. ``logged`` is a shorter
        reason for the log line, when the full one was already logged elsewhere."""
        self.heal_reasons[selector_key] = reason
        self._emit(level, 'did not heal ' + repr(selector_key) + ': ' + str(logged or reason))
        return False

    def try_heal(self, selector_key, meta, broken_candidates, context, row=None):
        """Heal + apply a validated selector. Returns the applied xpath (truthy) if a
        candidate was applied, else None/False. Never heals a key marked destructive, and
        never heals while the guard blocks. On every non-applied path it records a
        human-readable reason via ``self.heal_reasons`` (see ``explain``).

        With fingerprint=True the fingerprint tier runs first (after the destructive, page,
        role and guard checks), and the provider is asked only when it does not heal."""
        if meta.get('destructive'):
            # A key marked destructive (pay, submit, delete) is not auto-healed: a validated
            # but wrong selector would act on a live account, and the canary IS the real click.
            return self._decline(selector_key, _destructive_message(selector_key))
        moved = self._flow_change(selector_key, context)
        if moved:
            return self._decline(selector_key, moved)
        # A provider that advertises it is unavailable (e.g. the facade's no-provider
        # stand-in) carries its own actionable reason; surface it verbatim. The str check
        # matters: a bare Mock()/MagicMock() test double answers every attribute with a
        # truthy child mock, which must not be mistaken for a real "unavailable" reason.
        role = meta.get('expectedRole')
        role_unknown = not role or role == 'unknown'
        unavailable = getattr(self.provider, 'unavailable_reason', None)
        unavailable = unavailable if isinstance(unavailable, str) and unavailable else None
        summary = getattr(self.provider, 'unavailable_summary', None)
        summary = summary if isinstance(summary, str) else None
        if unavailable and (role_unknown or not self.fingerprint):
            if role_unknown:
                # Both need fixing: say so now, so fixing one does not just reveal the other.
                unavailable = unavailable.rstrip() + ' Also, mendwright ' + _role_needed(selector_key)
            return self._decline(selector_key, unavailable, logged=summary)
        if role_unknown:
            # Every proposal would fail the gate's role check, so asking costs model calls
            # for nothing.
            return self._decline(selector_key, 'mendwright ' + _role_needed(selector_key))
        if not self.guard():
            return self._decline(selector_key,
                "a transaction guard is currently blocking heals (a payment or registration "
                "submission is pending), so mendwright did not try to heal. Run the step again "
                "once the guard allows heals.")
        started = self.clock()
        html, requested = None, False
        # 'intent' when the key had no selector at all and the model looks for it from its
        # intent= description; the journal names that source, and the plain model none.
        source = 'intent' if not broken_candidates and meta.get('intent') else 'model'
        if source == 'intent' and unavailable:
            return self._decline(
                selector_key, "'" + selector_key + "' has no selector yet, and only a model can "
                'find it from intent=, but no model provider is configured. ' + unavailable,
                logged=("'" + selector_key + "' has no selector yet, and " + summary)
                if summary else None)
        if self.fingerprint and source != 'intent':
            html = self.html_provider()
            applied, note, requested = self._fingerprint_tier(selector_key, meta,
                                                              broken_candidates, html, started)
            if applied is not None:
                return applied
            if unavailable:
                # note says why the fingerprint did not heal; the provider's reason says
                # what to set so a model can try.
                return self._decline(
                    selector_key, note + ', and no model provider is configured to try '
                    'instead. ' + unavailable,
                    logged=(note + ', and ' + summary) if summary else None)
        # Direct-mode budget: fail closed when the rate/spend cap is reached (one heal
        # attempt == one slot), mirroring the gateway's server-side needs_human.
        if self.guardrails is not None and not self.guardrails.allow(selector_key):
            self.journal.rejected(selector_key, reason='rate/spend cap')
            return self._decline(selector_key,
                "the direct-mode rate/spend cap for '" + selector_key + "' was reached, so "
                "mendwright stopped (fail-closed). Wait for the hourly window to reset, or raise "
                "the cap via mendwright.open(rate_per_key_per_hour=..., rate_global_per_hour=...).")
        tag = source if source != 'model' else None
        if not requested:
            self.journal.requested(selector_key)
        if html is None:
            html = self.html_provider()
        # A provider that asks for it (LocalModelProvider, a model on this machine) also gets
        # the key's saved fingerprint; other providers and gateways never do.
        wants = getattr(self.provider, 'wants_fingerprint', False) is True
        bundle = build_bundle(selector_key, meta, broken_candidates,
                              self.inventory_provider(), html, context, row,
                              with_fingerprint=wants)
        # A provider that builds a prompt gets the page fitted to it (see mendwright.fit).
        # When elements had to be left out, positions in what the model read no longer match
        # the page, so a positional XPath is refused.
        left_out = 0
        if getattr(self.provider, 'wants_full_html', False) is not True:
            try:
                bundle, left_out = fit_bundle(bundle, meta.get('fingerprint'))
            except Exception:
                logger.debug('mendwright: could not fit the page to the prompt', exc_info=True)
        for _ in range(2):  # initial heal + one re-prompt on validation failure
            proposal = self.provider.propose(bundle)
            if not proposal:
                error = self._provider_error()
                if error:
                    # The provider's reason already names the cause and its next step (a bad
                    # key, a rate limit, a weak reply, a gateway's needs_human), so it is
                    # quoted as it is. Adding a guess of our own would contradict it.
                    self.journal.rejected(selector_key, reason='no proposal', error=error)
                    return self._decline(selector_key,
                        type(self.provider).__name__ + " returned no selector for '"
                        + selector_key + "': " + error)
                self.journal.rejected(selector_key, reason='no proposal')
                return self._decline(selector_key,
                    "the model returned no usable selector for '" + selector_key + "'. Try a "
                    "different or stronger model, or repair the selector by hand.")
            self.journal.proposed(selector_key, xpath=proposal.get('xpath'),
                                  reasoning=proposal.get('reasoning'), source=tag)
            if self.dry_run:
                valid = None
                for xpath in [proposal.get('xpath')] + list(proposal.get('fallbacks') or []):
                    if left_out and xpath and has_position(xpath):
                        continue   # apply() would refuse it (see mendwright.fit)
                    if xpath and validate(xpath, html, expected_role=meta.get('expectedRole'),
                                          expected_text=meta.get('expectedText'),
                                          destructive=bool(meta.get('destructive'))).ok:
                        valid = xpath
                        break
                self._emit(logging.INFO, '[heal dry-run] key=' + selector_key + ' proposed=' +
                           str(proposal.get('xpath')) + ' valid=' + str(bool(valid)) + ' (not applied)')
                return self._decline(selector_key,
                    "dry-run mode is on, so the proposal for '" + selector_key + "' was checked "
                    "but not applied. Turn off dry_run to let mendwright apply and retry.",
                    level=logging.INFO)
            applied, reason = apply(self.store, selector_key, proposal, html, meta,
                                    driver=self.driver, positional=not left_out,
                                    frame_xpath=self.frame_xpath)
            if applied:
                self._record_applied(selector_key, broken_candidates, applied, started,
                                     type(self.provider).__name__, self._provider_model(),
                                     source)
                return applied
            # Feed the actual validation reason back so the re-prompt can fix the actual problem.
            self.journal.rejected(selector_key, xpath=proposal.get('xpath'), reason=reason,
                                  source=tag)
            bundle = dict(bundle, rejectedXpath=proposal.get('xpath'), rejectReason=reason)
        self._decline(selector_key,
            "no selector the model proposed for '" + selector_key + "' passed mendwright's local "
            "gate (last reason: " + str(reason) + "). The gate requires a non-brittle xpath that "
            "matches exactly one element of the expected role and is present on the live page.")
        return None

    def _fingerprint_tier(self, selector_key, meta, broken_candidates, html, started):
        """The first tier: (applied, note, requested). applied is the xpath applied, False
        when dry run declined the heal (with its reason recorded), or None to go on to the
        model; note says why the tier did not heal, and requested whether heal.requested
        was journaled."""
        stored = meta.get('fingerprint')
        if not _fingerprint.valid(stored):
            return None, _no_fingerprint(selector_key), False
        self.journal.requested(selector_key)
        match = _fingerprint.find(stored, html, meta.get('expectedRole'),
                                  visible=self._live_visible if self.driver is not None else None)
        if not match.xpath:
            return None, ("the fingerprint match for '" + selector_key + "' was not confident: "
                          + match.reason), True
        reasoning = ('fingerprint score ' + format(match.score, '.2f') + ', next best '
                     + format(match.runner_up, '.2f'))
        self.journal.proposed(selector_key, xpath=match.xpath, reasoning=reasoning,
                              source=FINGERPRINT_PROVIDER)
        if self.dry_run:
            ok = validate(match.xpath, html, expected_role=meta.get('expectedRole'),
                          expected_text=meta.get('expectedText'),
                          destructive=bool(meta.get('destructive'))).ok
            self._emit(logging.INFO, '[heal dry-run] key=' + selector_key + ' proposed='
                       + match.xpath + ' (fingerprint) valid=' + str(ok) + ' (not applied)')
            return self._decline(selector_key,
                "dry-run mode is on, so the fingerprint match for '" + selector_key + "' was "
                "checked but not applied. Turn off dry_run to let mendwright apply and retry.",
                level=logging.INFO), '', True
        proposal = {'xpath': match.xpath, 'fallbacks': [], 'reasoning': reasoning}
        applied, reason = apply(self.store, selector_key, proposal, html, meta, driver=self.driver,
                                frame_xpath=self.frame_xpath)
        if applied:
            self._record_applied(selector_key, broken_candidates, applied, started,
                                 FINGERPRINT_PROVIDER, None, FINGERPRINT_PROVIDER)
            return applied, '', True
        self.journal.rejected(selector_key, xpath=match.xpath, reason=reason,
                              source=FINGERPRINT_PROVIDER)
        return None, ("the fingerprint match for '" + selector_key + "', " + match.xpath
                      + ", failed mendwright's checks (" + str(reason) + ')'), True

    def decline_list(self, selector_key, meta):
        """Decline to heal list key, whose selectors all match nothing (see
        Healer.locator_all), and return the reason, recorded and logged as for any heal that
        did not apply (see ``explain``). mendwright never heals such a list: an emptied list
        whose items show in another list on the page now, such as a cart's in Saved for
        later, can be the same page as a redesign but for a class name or a heading's text.
        So nothing is looked for on the page, no model is asked and nothing is saved."""
        lfp = meta.get('listFingerprint') if isinstance(meta, dict) else None
        self._decline(selector_key, _list_missing_message(
            selector_key, lfp.get('count') if isinstance(lfp, dict) else None))
        return self.explain(selector_key)

    def _live_visible(self, xpath):
        """For the fingerprint tier: False when xpath does not reach one visible element on
        the live page, True when it does, None when the page could not be read."""
        try:
            return self.driver.count(xpath) == 1 and bool(self.driver.is_visible(xpath))
        except Exception:
            return None

    def _record_applied(self, selector_key, broken_candidates, applied, started, provider,
                        model, source):
        """Journal, remember and log a heal that was applied."""
        seconds = max(0.0, self.clock() - started)
        self.journal.applied(selector_key, xpath=applied, old=_old_selectors(broken_candidates),
                             provider=provider, model=model,
                             latency_ms=int(round(seconds * 1000)),
                             source=source if source != 'model' else None)
        self.heal_reasons.pop(selector_key, None)  # a prior failure no longer applies
        self.heals.append({'key': selector_key, 'old': tuple(_old_selectors(broken_candidates)),
                           'new': applied, 'confirmed': None, 'source': source})
        self._log_applied(selector_key, broken_candidates, applied, seconds, provider, source)

    def _flow_change(self, selector_key, context):
        """The reason to decline when the current page is not one the key was found on,
        else ''. Journals the decision as flow.page_guard."""
        if self.heal_across_pages or not isinstance(context, dict):
            return ''
        here = context.get('page')
        last = context.get('lastPage')
        seen = [p for p in (context.get('pages') or ()) if isinstance(p, str) and p]
        if isinstance(last, str) and last and last not in seen:
            seen.insert(0, last)
        if not (isinstance(here, str) and here and seen) or here in seen:
            return ''
        last = seen[0]
        journal_event(self.journal, 'flow.page_guard', selector_key, last_page=last, page=here)
        return flow_change_message(selector_key, last, here, self.entry)

    def _provider_model(self):
        """The provider's model name when it has a str .model attribute, else None."""
        model = getattr(self.provider, 'model', None)
        return model if isinstance(model, str) and model else None

    def _provider_error(self):
        """The provider's last_error when it is a non-empty str, else ''. Providers set it
        when propose() returns None because the call failed. A Mock answers every
        attribute with a child Mock, hence the type check."""
        error = getattr(self.provider, 'last_error', None)
        if isinstance(error, str) and error.strip():
            return error.strip()
        return ''

    def _log_applied(self, selector_key, broken_candidates, applied, seconds, provider,
                     source='model'):
        old = _old_selectors(broken_candidates)
        timing = ' (' + provider + ', ' + format(seconds, '.1f') + 's)'
        if source == 'intent':
            self._emit(logging.WARNING, 'found ' + repr(selector_key) + ' from its intent: '
                       + applied + timing)
            return
        if not old:
            was = '(no stored selector)'
        elif len(old) == 1:
            was = old[0]
        else:
            was = old[0] + ' (+' + str(len(old) - 1) + ' more)'
        self._emit(logging.WARNING, 'healed ' + repr(selector_key) + ': ' + was + ' -> '
                   + applied + timing)

    def confirm(self, selector_key, xpath, success, reason=None):
        """Canary report: route the outcome through the provider, and quarantine a healed
        candidate whose retry failed. A heal from the fingerprint tier, or a key followed
        into an iframe, is not reported to the provider, which did not propose it. reason,
        when given, is journaled with the canary (see mendwright.values)."""
        heal = next((h for h in reversed(self.heals) if h['key'] == selector_key
                     and h['new'] == xpath and h['confirmed'] is None), None)
        if heal is None or heal.get('source') not in (FINGERPRINT_PROVIDER, FRAME_PROVIDER):
            try:
                self.provider.report_outcome(selector_key, xpath, success)
            except Exception:
                pass
        self.journal.canary(selector_key, xpath=xpath, success=bool(success), reason=reason)
        if heal is not None:
            heal['confirmed'] = bool(success)
        if not success:
            self.store.quarantine(selector_key, xpath)
            self.journal.quarantine(selector_key, xpath=xpath, reason=reason)

    def is_pending(self, selector_key, xpath):
        """True when xpath is the heal of selector_key this process applied and has not
        confirmed yet."""
        return xpath is not None and self.pending.get(selector_key) == xpath

    def confirm_pending(self, selector_key, success, reason=None, xpath=None):
        """Report the action outcome for a just-healed key, then clear its marker.
        A failed action quarantines the healed candidate so it can't outrank the seed.
        With xpath, only when the key's heal waiting is the one that applied xpath."""
        if xpath is not None and self.pending.get(selector_key) != xpath:
            return
        xpath = self.pending.pop(selector_key, None)
        if xpath is not None:
            self.confirm(selector_key, xpath, success, reason)

    def explain(self, selector_key):
        """The human-readable reason the last heal attempt for ``selector_key`` did not
        apply a fix, with a hint on what to do next, or '' if nothing was recorded."""
        return self.heal_reasons.get(selector_key, '')


def resolve_with_heal(resolve_fn, coordinator, selector_key, meta, context, row=None):
    """Resolve a selector; if all candidates are exhausted, heal once and retry the
    same resolution inline. Re-raises AttentionRequired if no heal is possible."""
    __tracebackhide__ = True  # pytest shows the caller
    try:
        return resolve_fn()
    except AttentionRequired as exc:
        if coordinator is None:
            raise
        broken = list(coordinator.store.candidates(selector_key))
        applied = coordinator.try_heal(selector_key, meta, broken, context, row)
        if not applied:
            # Enrich the surfaced error with WHY the heal didn't happen and what to do
            # next, keeping the original locator message as the prefix. Falls back to the
            # bare re-raise when the coordinator recorded no reason.
            reason = coordinator.explain(selector_key)
            if reason:
                raise AttentionRequired(str(exc).rstrip() + '\n  -> ' + str(reason)) from exc
            raise
        coordinator.pending[selector_key] = applied  # canary set on action outcome
        return resolve_fn()
