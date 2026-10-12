"""Typed, exported data shapes for mendwright. The runtime still accepts/returns plain
dicts (back-compat with the functional core and the JS gateway contract); these
dataclasses give type-checkers and IDEs a precise surface and document the fields."""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Proposal:
    """A repaired-selector suggestion from a HealProvider. Untrusted until validated."""
    xpath: str
    fallbacks: Tuple[str, ...] = ()
    reasoning: str = ""
    frame_chain: Tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d):
        if not d or not d.get('xpath'):
            return None
        return cls(xpath=str(d['xpath']),
                   fallbacks=tuple(str(f) for f in (d.get('fallbacks') or [])),
                   reasoning=str(d.get('reasoning') or ''),
                   frame_chain=tuple(d.get('frame_chain') or ()))

    def to_dict(self):
        out = {'xpath': self.xpath, 'fallbacks': list(self.fallbacks), 'reasoning': self.reasoning}
        if self.frame_chain:
            out['frame_chain'] = list(self.frame_chain)
        return out


@dataclass(frozen=True)
class HealBundle:
    """The redacted capture a provider reasons over (mirrors capture.build_bundle output)."""
    key: str
    expectedRole: Optional[str] = None
    expectedText: Tuple[str, ...] = ()
    destructive: bool = False
    candidates: Tuple[str, ...] = ()
    inventory: tuple = ()
    html: str = ""
    url: Optional[str] = None
    stage: Optional[str] = None
    version: Optional[str] = None
    page: Optional[str] = None          # the page path now (mendwright.flow.page_path)
    lastPage: Optional[str] = None      # the page path the key was last found on


@dataclass
class HealResult:
    """Outcome of a heal attempt, for events/journal/report."""
    key: str
    applied: Optional[str] = None       # the applied xpath, or None
    reason: str = ""                    # validation reason when not applied
    provider: str = ""
    reasoning: str = ""
    confirmed: Optional[bool] = None    # canary outcome once known
