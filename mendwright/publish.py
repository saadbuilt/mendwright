"""Publisher: patch an authoritative xpaths JSON with a canary-confirmed healed
candidate, then (separately) re-run a tool-supplied packager on the released bundle
to ship the changed JSON as an update delta.

Tool-agnostic: ``republish_bundle`` takes the packager as ``package_fn`` rather than
importing a specific tool's packaging module. Only confirmed candidates reach here.
"""

import json
import os
import tempfile
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix='.xpaths-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def publish(authoritative_json_path, key, xpath, *, source='heal', now=None):
    """Add/refresh a confirmed candidate under `key` (dedupe by xpath, stamp
    lastSucceededAt, cap active candidates at maxCandidates), then atomic-write."""
    now = now or _now()
    with open(authoritative_json_path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    selectors = data.get('selectors', {})
    if key not in selectors:
        raise ValueError('Unknown selector key: ' + key)
    entry = selectors[key]
    candidates = entry.setdefault('candidates', [])

    existing = next((c for c in candidates if c.get('xpath') == xpath), None)
    if existing is not None:
        existing['status'] = 'active'
        existing['lastSucceededAt'] = now
    else:
        candidates.append({'xpath': xpath, 'source': source, 'addedAt': now,
                           'lastSucceededAt': now, 'status': 'active'})

    cap = int(entry.get('maxCandidates', 5))
    actives = [c for c in candidates if c.get('status') == 'active']
    if len(actives) > cap:
        actives.sort(key=lambda c: (c.get('lastSucceededAt') or '', c.get('addedAt') or ''), reverse=True)
        keep = {id(c) for c in actives[:cap]}
        for c in candidates:
            if c.get('status') == 'active' and id(c) not in keep:
                c['status'] = 'quarantined'

    _atomic_write(authoritative_json_path, data)


def republish_bundle(bundle_root, authoritative_json_path, output_dir, version, *,
                     package_fn, bundle_json_relpaths):
    """Swap the patched JSON into each path under the released bundle and regenerate
    the update feed via the tool-supplied ``package_fn(bundle_root, output_dir,
    version)``. The updater ships only the changed file (hash delta)."""
    import shutil

    for relpath in bundle_json_relpaths:
        dest = os.path.join(bundle_root, relpath)
        if os.path.exists(os.path.dirname(dest)):
            shutil.copy2(authoritative_json_path, dest)
    return package_fn(bundle_root, output_dir, version)
