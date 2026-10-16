"""Drift: whether the selectors saved for each key still find their elements on a page, and
what a run would do about the ones that don't, without acting on the page, saving anything
or asking a model. `mendwright check` runs it on the page where each key was last found,
and heal.drift() on the page a script has reached.
"""

from urllib.parse import urlsplit


def same_page(last_url, last_page):
    """Whether a key's saved address (lastUrl, see mendwright.flow.page_url) is on its saved
    page (lastPage, see mendwright.flow.page_path): their paths have as many non-empty
    segments, and each segment of the address is '*' or the page's segment once lower-cased.
    A root address is on the page None. False for anything that is not an address."""
    if not isinstance(last_url, str):
        return False
    try:
        path = urlsplit(last_url).path
    except ValueError:
        return False
    url_parts = [part for part in path.split('/') if part]
    page_parts = [part for part in (last_page or '').split('/') if part]
    if len(url_parts) != len(page_parts):
        return False
    return all(u == '*' or u.lower() == p for u, p in zip(url_parts, page_parts, strict=True))
