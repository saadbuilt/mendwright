"""The urllib heal client MUST send a real User-Agent, or Cloudflare's edge rejects
the request with 403 error 1010 before it reaches the worker (silently breaking heal).
"""

import urllib.request

import mendwright.client as client


def test_urllib_client_sends_user_agent(monkeypatch):
    captured = {}

    class _FakeResp:
        def getcode(self):
            return 200

        def read(self):
            return b'{"status":"ok","xpath":"x"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        captured['headers'] = dict(req.header_items())
        return _FakeResp()

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    client._UrllibHttp().post('https://gw.example/heal', json={'k': 1},
                              headers={'X-Custom-Heal-Key': 'abc'})

    # urllib capitalizes header names; look for a User-Agent that isn't the python default.
    ua = next((v for k, v in captured['headers'].items() if k.lower() == 'user-agent'), None)
    assert ua and 'python-urllib' not in ua.lower()
