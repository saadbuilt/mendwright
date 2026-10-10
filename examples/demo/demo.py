"""The smallest real use of mendwright: a selector breaks, mendwright heals it, and the
step runs again.

    python examples/demo/demo.py

(With nothing cloned, `mendwright demo` tells the same story, plus a pay button marked
destructive=True, which mendwright refuses to heal.)

v1.html and v2.html, next to this file, are one signup page before and after a redesign
that removed the checkbox's id. The code in main() is what you would write in a test.
Two things are stand-ins, both from mendwright.demo, so it runs offline:

- StandInPage, an lxml page shaped like a Playwright page. Use your real page instead.
- ScriptedProvider, which runs mendwright's real prompt and parser on a canned reply
  instead of calling a model. Leave provider= out and mendwright.open() picks a provider
  from your environment (ANTHROPIC_API_KEY and ANTHROPIC_MODEL, for example).
"""

import sys
import tempfile
from pathlib import Path

import mendwright
from mendwright.demo import HEALED_XPATH, SCRIPTED_REPLY, ScriptedProvider, StandInPage

HERE = Path(__file__).resolve().parent
KEY = 'accept_terms'
STALE_SELECTOR = '#accept-terms'  # written against v1.html; v2.html has no such id

__all__ = ['main', 'KEY', 'STALE_SELECTOR', 'HEALED_XPATH']


def load(page, version):
    """Show v1.html or v2.html in the page, as page.set_content() does in Playwright."""
    page.set_content((HERE / (version + '.html')).read_text(encoding='utf-8'))


def main(override_path=None):
    """Run the example and return what happened. healed is the whole red -> green arc."""
    with tempfile.TemporaryDirectory(prefix='mendwright-example-',
                                     ignore_cleanup_errors=True) as tmp:
        page = StandInPage()
        provider = ScriptedProvider({KEY: SCRIPTED_REPLY})
        heal = mendwright.open(page, provider=provider, timeout_ms=0,
                               store_path=override_path or str(Path(tmp) / 'overrides.json'))

        # v1: the selector in the code matches. mendwright reads the role from the page.
        load(page, 'v1')
        box = heal.locator(KEY, STALE_SELECTOR)
        box.check()
        v1_resolved = box.is_checked()
        print('v1  ' + STALE_SELECTOR + ' matched, box checked')

        # v2: after the redesign the same selector matches nothing.
        load(page, 'v2')
        v2_broke = not heal.present(KEY)
        print('v2  ' + STALE_SELECTOR + ' no longer matches')

        # The same line as before. It misses, asks the provider, checks the proposal
        # against the page, saves it, and retries, all inside this one call.
        box = heal.locator(KEY, STALE_SELECTOR)
        # expect= confirms the heal only if the action really worked.
        box.check(expect=box.is_checked)
        v2_resolved_after_heal = box.is_checked()

        event = heal.heals[-1] if heal.heals else None
        report = {
            'v1_resolved': v1_resolved,
            'v2_broke': v2_broke,
            'v2_resolved_after_heal': v2_resolved_after_heal,
            'healed_xpath': event.new if event else None,
            'provider_prompted': bool(provider.prompts),
            'canary_outcome': event.confirmed if event else None,
        }
        report['healed'] = bool(v1_resolved and v2_broke and v2_resolved_after_heal
                                and report['canary_outcome'])
        if event:
            print('    healed: ' + ' '.join(event.old) + ' -> ' + event.new
                  + (' (confirmed)' if event.confirmed else ''))
        print('result: ' + ('red -> green' if report['healed'] else 'not healed'))
        return report


if __name__ == '__main__':
    sys.exit(0 if main()['healed'] else 1)
