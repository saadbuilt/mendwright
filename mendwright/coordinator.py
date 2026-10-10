"""The heal itself. When every candidate selector misses, HealCoordinator captures the
page, asks a provider for a fix, checks and saves it, and resolve_with_heal() retries the
step in the same call. A guard can block healing while a payment or registration is being
submitted, keys marked destructive are never healed, and a failed canary quarantines the
healed candidate. When a heal does not happen, the coordinator records a readable reason
(see ``explain``), and the AttentionRequired it raises tells the caller what to do next.
"""

import logging
import time

from .capture import build_bundle
from .client import DEFAULT_AUTH_HEADER, apply
from .exceptions import AttentionRequired
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


def _destructive_message(selector_key):
    return ("'" + selector_key + "' is marked as a destructive control, and "
            "mendwright never auto-heals those, because a validated but wrong selector here "
            "could act on a live account. Fix the selector in your code or seed file by hand.")


class HealCoordinator:
    """Runs one heal: capture, propose, validate, apply.

    Diagnostics go to the 'mendwright' logger, prefixed with 'mendwright: ': one WARNING
    line per applied heal and one per declined heal, so a heal shows on stderr even when
    the application configured no logging. Pass log=<callable taking a str> to send the
    same lines (without the prefix) somewhere else instead, e.g. log=print. clock is the
    monotonic clock used to time a heal; tests inject a fake one.
    """

    def __init__(self, store, gateway=None, download_key=None, *, provider=None, http=None,
                 html_provider, inventory_provider, guard, dry_run=False, log=None,
                 auth_header=DEFAULT_AUTH_HEADER, driver=None, guardrails=None, journal=None,
                 clock=time.monotonic):
        self.store = store
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
        # selector_key -> applied_xpath awaiting an action-outcome canary. A dict (not a
        # single slot) so healing several fields on one page never drops an earlier
        # unproven selector's canary.
        self.pending = {}
        # selector_key -> the human-readable reason the last heal attempt did not apply a
        # fix. resolve_with_heal appends this to the AttentionRequired it surfaces, so a
        # person sees WHY (and what to do) instead of a bare "no matching element".
        self.heal_reasons = {}
        # Every heal this coordinator applied, oldest first: {'key', 'old', 'new',
        # 'confirmed'}. confirmed is None until the canary reports, then True or False.
        self.heals = []

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
        human-readable reason via ``self.heal_reasons`` (see ``explain``)."""
        if meta.get('destructive'):
            # A key marked destructive (pay, submit, delete) is not auto-healed: a validated
            # but wrong selector would act on a live account, and the canary IS the real click.
            return self._decline(selector_key, _destructive_message(selector_key))
        # A provider that advertises it is unavailable (e.g. the facade's no-provider
        # stand-in) carries its own actionable reason; surface it verbatim. The str check
        # matters: a bare Mock()/MagicMock() test double answers every attribute with a
        # truthy child mock, which must not be mistaken for a real "unavailable" reason.
        role = meta.get('expectedRole')
        role_unknown = not role or role == 'unknown'
        unavailable = getattr(self.provider, 'unavailable_reason', None)
        if isinstance(unavailable, str) and unavailable:
            summary = getattr(self.provider, 'unavailable_summary', None)
            if role_unknown:
                # Both need fixing: say so now, so fixing one does not just reveal the other.
                unavailable = unavailable.rstrip() + ' Also, mendwright ' + _role_needed(selector_key)
            return self._decline(selector_key, unavailable,
                                 logged=summary if isinstance(summary, str) else None)
        if role_unknown:
            # Every proposal would fail the gate's role check, so asking costs model calls
            # for nothing.
            return self._decline(selector_key, 'mendwright ' + _role_needed(selector_key))
        if not self.guard():
            return self._decline(selector_key,
                "a transaction guard is currently blocking heals (a payment or registration "
                "submission is pending), so mendwright did not try to heal. Run the step again "
                "once the guard allows heals.")
        # Direct-mode budget: fail closed when the rate/spend cap is reached (one heal
        # attempt == one slot), mirroring the gateway's server-side needs_human.
        if self.guardrails is not None and not self.guardrails.allow(selector_key):
            self.journal.rejected(selector_key, reason='rate/spend cap')
            return self._decline(selector_key,
                "the direct-mode rate/spend cap for '" + selector_key + "' was reached, so "
                "mendwright stopped (fail-closed). Wait for the hourly window to reset, or raise "
                "the cap via mendwright.open(rate_per_key_per_hour=..., rate_global_per_hour=...).")
        self.journal.requested(selector_key)
        started = self.clock()
        html = self.html_provider()
        bundle = build_bundle(selector_key, meta, broken_candidates,
                              self.inventory_provider(), html, context, row)
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
                                  reasoning=proposal.get('reasoning'))
            if self.dry_run:
                valid = None
                for xpath in [proposal.get('xpath')] + list(proposal.get('fallbacks') or []):
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
            applied, reason = apply(self.store, selector_key, proposal, html, meta, driver=self.driver)
            if applied:
                seconds = max(0.0, self.clock() - started)
                self.journal.applied(selector_key, xpath=applied,
                                     old=_old_selectors(broken_candidates),
                                     provider=type(self.provider).__name__,
                                     model=self._provider_model(),
                                     latency_ms=int(round(seconds * 1000)))
                self.heal_reasons.pop(selector_key, None)  # a prior failure no longer applies
                self.heals.append({'key': selector_key,
                                   'old': tuple(_old_selectors(broken_candidates)),
                                   'new': applied, 'confirmed': None})
                self._log_applied(selector_key, broken_candidates, applied, seconds)
                return applied
            # Feed the actual validation reason back so the re-prompt can fix the actual problem.
            self.journal.rejected(selector_key, xpath=proposal.get('xpath'), reason=reason)
            bundle = dict(bundle, rejectedXpath=proposal.get('xpath'), rejectReason=reason)
        self._decline(selector_key,
            "no selector the model proposed for '" + selector_key + "' passed mendwright's local "
            "gate (last reason: " + str(reason) + "). The gate requires a non-brittle xpath that "
            "matches exactly one element of the expected role and is present on the live page.")
        return None

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

    def _log_applied(self, selector_key, broken_candidates, applied, seconds):
        old = _old_selectors(broken_candidates)
        if not old:
            was = '(no stored selector)'
        elif len(old) == 1:
            was = old[0]
        else:
            was = old[0] + ' (+' + str(len(old) - 1) + ' more)'
        self._emit(logging.WARNING, 'healed ' + repr(selector_key) + ': ' + was + ' -> '
                   + applied + ' (' + type(self.provider).__name__ + ', '
                   + format(seconds, '.1f') + 's)')

    def confirm(self, selector_key, xpath, success):
        """Canary report: route the outcome through the provider, and quarantine a healed
        candidate whose retry failed."""
        try:
            self.provider.report_outcome(selector_key, xpath, success)
        except Exception:
            pass
        self.journal.canary(selector_key, xpath=xpath, success=bool(success))
        for heal in reversed(self.heals):
            if heal['key'] == selector_key and heal['new'] == xpath and heal['confirmed'] is None:
                heal['confirmed'] = bool(success)
                break
        if not success:
            self.store.quarantine(selector_key, xpath)
            self.journal.quarantine(selector_key, xpath=xpath)

    def confirm_pending(self, selector_key, success):
        """Report the action outcome for a just-healed key, then clear its marker.
        A failed action quarantines the healed candidate so it can't outrank the seed."""
        xpath = self.pending.pop(selector_key, None)
        if xpath is not None:
            self.confirm(selector_key, xpath, success)

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
