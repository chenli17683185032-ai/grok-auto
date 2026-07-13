"""Per-call proxy helper must not require process env mutation."""

from __future__ import annotations

import unittest
from unittest import mock

import sso_to_auth_json as m


class ProxyOpenUrlTests(unittest.TestCase):
    def test_open_url_with_proxy_uses_proxy_handler(self) -> None:
        req = mock.Mock()
        fake_resp = mock.MagicMock()
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        fake_resp.read.return_value = b'{"access_token":"x"}'

        with mock.patch.object(m.urllib.request, "build_opener") as bo:
            opener = mock.Mock()
            opener.open.return_value = fake_resp
            bo.return_value = opener
            with m._open_url(req, timeout=5, proxy="http://proxy.example:7890") as resp:
                self.assertIs(resp, fake_resp)
            bo.assert_called_once()
            opener.open.assert_called_once()

    def test_open_url_without_proxy_uses_urlopen(self) -> None:
        req = mock.Mock()
        fake_resp = mock.MagicMock()
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch.object(m.urllib.request, "urlopen", return_value=fake_resp) as uo:
            with m._open_url(req, timeout=5, proxy=None) as resp:
                self.assertIs(resp, fake_resp)
            uo.assert_called_once()


if __name__ == "__main__":
    unittest.main()
