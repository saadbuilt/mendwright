"""A broken selector that can't be auto-healed must surface an AttentionRequired that
tells a HUMAN exactly why and what to do next — not a bare "no matching element".

The coordinator records a reason for every path that declines or fails a heal
(destructive control, guard closed, no provider configured, rate/spend cap, local-gate
rejection). resolve_with_heal appends that reason to the exception it re-raises, keeping
the original message as a prefix. These tests pin one actionable message per path.
"""

import os
import unittest
from unittest.mock import Mock

import lxml.html

from mendwright import AttentionRequired, HealCoordinator, resolve_with_heal
from mendwright import open as sh_open

HTML = "<form><label>Email<input type='text' id='email'></label><button>Pay</button></form>"


class FakeStore:
    """Minimal store: one broken candidate, so resolution always exhausts."""

    def candidates(self, key):
        return [{"xpath": "//input[@id='GONE']"}]

    def add_candidate(self, key, xpath, *, source="heal", **kw):
        pass

    def quarantine(self, key, xpath):
        pass


class StubProvider:
    def __init__(self, xpath="//nope"):
        self.xpath = xpath

    def propose(self, bundle):
        return {"xpath": self.xpath, "fallbacks": [], "reasoning": ""}

    def report_outcome(self, key, xpath, success):
        pass


def _broken_resolve(key):
    def resolve_fn():
        raise AttentionRequired("No matching element for: " + key)
    return resolve_fn


def _coord(*, guard=True, guardrails=None, provider=None):
    return HealCoordinator(
        FakeStore(),
        provider=provider or StubProvider(),
        html_provider=lambda: HTML,
        inventory_provider=lambda: [],
        guard=lambda: guard,
        guardrails=guardrails,
    )


class AttentionMessageTests(unittest.TestCase):
    def _raise_and_get_message(self, coord, key, meta):
        with self.assertRaises(AttentionRequired) as ctx:
            resolve_with_heal(_broken_resolve(key), coord, key, meta, {})
        return str(ctx.exception)

    def test_keeps_the_original_message_as_a_prefix(self):
        coord = _coord()
        meta = {"expectedRole": "button", "destructive": True, "expectedText": ["Pay"]}
        msg = self._raise_and_get_message(coord, "pay", meta)
        self.assertIn("No matching element for: pay", msg)

    def test_destructive_control_explains_the_safety_rule(self):
        coord = _coord()
        meta = {"expectedRole": "button", "destructive": True, "expectedText": ["Pay"]}
        msg = self._raise_and_get_message(coord, "pay", meta)
        self.assertIn("destructive", msg.lower())
        self.assertIn("never", msg.lower())

    def test_guard_closed_explains_the_pending_transaction(self):
        coord = _coord(guard=False)
        meta = {"expectedRole": "checkbox", "destructive": False}
        msg = self._raise_and_get_message(coord, "terms", meta)
        self.assertIn("guard", msg.lower())

    def test_rate_cap_tells_you_a_cap_was_reached(self):
        from mendwright.limits import Guardrails, RateLimiter
        # per_key_per_hour=0 => every attempt is over the cap => always fails closed.
        guard = Guardrails(RateLimiter(per_key_per_hour=0))
        coord = _coord(guardrails=guard)
        meta = {"expectedRole": "checkbox", "destructive": False}
        msg = self._raise_and_get_message(coord, "terms", meta)
        self.assertIn("cap", msg.lower())

    def test_local_gate_rejection_surfaces_the_reason(self):
        # The provider proposes an xpath that matches nothing in the snapshot, so the gate
        # rejects it; the surfaced message carries the gate's own reason.
        coord = _coord(provider=StubProvider("//does-not-exist"))
        meta = {"expectedRole": "checkbox", "destructive": False}
        msg = self._raise_and_get_message(coord, "terms", meta)
        self.assertIn("gate", msg.lower())

    def test_mock_provider_is_not_mistaken_for_unavailable(self):
        # A bare Mock()/MagicMock() answers EVERY attribute with a truthy child mock,
        # including unavailable_reason. The coordinator must not treat that as "no provider":
        # a Mock that proposes a valid selector must still heal.
        provider = Mock()
        provider.propose.return_value = {"xpath": "//input[@id='email']",
                                         "fallbacks": [], "reasoning": ""}
        coord = HealCoordinator(
            FakeStore(), provider=provider,
            html_provider=lambda: HTML, inventory_provider=lambda: [],
            guard=lambda: True)
        applied = coord.try_heal("email", {"expectedRole": "input", "destructive": False},
                                 [], {}, None)
        self.assertEqual(applied, "//input[@id='email']")
        provider.propose.assert_called_once()


class FacadeNoProviderTests(unittest.TestCase):
    class _Page:
        def __init__(self, html):
            self._doc = lxml.html.fromstring(html)
            self.url = "https://example.test/signup"

        def locator(self, sel):
            xp = sel[len("xpath="):] if sel.startswith("xpath=") else sel
            try:
                els = [e for e in self._doc.xpath(xp) if hasattr(e, "tag")]
            except Exception:
                els = []

            class _Loc:
                def __init__(self, n):
                    self._n = n

                def count(self):
                    return self._n

                def is_visible(self):
                    return self._n == 1

                def scroll_into_view_if_needed(self):
                    pass

            return _Loc(len(els))

        def frame_locator(self, sel):
            return self

        def content(self):
            return lxml.html.tostring(self._doc, encoding="unicode")

    def test_no_provider_configured_tells_you_how_to_configure_one(self, ):
        import mendwright.providers as providers

        def _boom(*a, **k):
            raise ValueError("no provider configured")

        original = providers.auto_provider
        providers.auto_provider = _boom
        try:
            import tempfile
            import os
            d = tempfile.mkdtemp(prefix="mendwright-attn-")
            healer = sh_open(self._Page(HTML), store_path=os.path.join(d, "o.json"))
            with self.assertRaises(AttentionRequired) as ctx:
                healer.locator("terms", "//input[@id='GONE']", role="checkbox",
                               timeout_ms=0, poll_ms=0)
            msg = str(ctx.exception).lower()
            self.assertIn("provider", msg)
            # Points the human at a concrete next step (an env var or the provider= arg).
            self.assertTrue("api_key" in msg or "provider=" in msg)
        finally:
            providers.auto_provider = original

    def test_missing_model_id_names_the_exact_env_var(self):
        # The common setup mistake: the key is set but the model id isn't. The surfaced
        # message must name ANTHROPIC_MODEL (the real cause), not just "set a key".
        import tempfile

        saved = {k: os.environ.get(k) for k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "MENDWRIGHT_PROVIDER")}
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        os.environ.pop("ANTHROPIC_MODEL", None)
        os.environ.pop("MENDWRIGHT_PROVIDER", None)
        try:
            d = tempfile.mkdtemp(prefix="mendwright-attn-")
            healer = sh_open(self._Page(HTML), store_path=os.path.join(d, "o.json"))
            with self.assertRaises(AttentionRequired) as ctx:
                healer.locator("terms", "//input[@id='GONE']", role="checkbox",
                               timeout_ms=0, poll_ms=0)
            self.assertIn("ANTHROPIC_MODEL", str(ctx.exception))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
