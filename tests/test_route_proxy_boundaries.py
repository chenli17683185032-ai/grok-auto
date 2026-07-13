"""Route affinity: device/code + token poll use explicit proxy, no env mutation."""

from __future__ import annotations

import os
import unittest
from unittest import mock


class RouteProxyBoundaryTests(unittest.TestCase):
    def test_request_device_code_uses_proxy_param(self) -> None:
        import sso_to_auth_json as m

        seen = {}

        class Resp:
            def read(self):
                return b'{"device_code":"d","user_code":"u","interval":5,"expires_in":600}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=15, proxy=None):
            seen["proxy"] = proxy
            return Resp()

        with mock.patch.object(m, "_open_url", side_effect=fake_open):
            out = m.request_device_code(proxy="http://route2:7890")
        self.assertIsNotNone(out)
        self.assertEqual(seen.get("proxy"), "http://route2:7890")

    def test_no_process_environment_proxy_mutation(self) -> None:
        import sso_to_auth_json as m

        before = dict(os.environ)
        class Resp:
            def read(self):
                return b'{"access_token":"a","refresh_token":"r","expires_in":100}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(req, timeout=15, proxy=None):
            return Resp()

        with mock.patch.object(m, "_open_url", side_effect=fake_open):
            m.poll_token_cancellable("dc", 0, 10, timeout=1, proxy="http://route2:7890")
        # HTTP_PROXY must not be injected
        self.assertEqual(os.environ.get("HTTP_PROXY"), before.get("HTTP_PROXY"))
        self.assertEqual(os.environ.get("HTTPS_PROXY"), before.get("HTTPS_PROXY"))

    def test_route_sticky_off_keeps_legacy_route(self) -> None:
        import tempfile
        from pathlib import Path

        from registration_controller import RegistrationController
        from registration_queue import RegistrationQueue
        from route_registry import Route, reset_registry_for_tests

        os.environ["GROK2API_ROUTE_STICKY"] = "0"
        try:
            reset_registry_for_tests(
                (
                    Route(
                        route_id="route-1",
                        register_proxy="http://p1",
                        token_proxy="http://p1",
                        approver="http://a1",
                        approver_id="a1",
                        mihomo_id="m1",
                    ),
                    Route(
                        route_id="route-2",
                        register_proxy="http://p2",
                        token_proxy="http://p2",
                        approver="http://a2",
                        approver_id="a2",
                        mihomo_id="m2",
                    ),
                )
            )
            with tempfile.TemporaryDirectory() as td:
                q = RegistrationQueue(Path(td) / "q.db")
                os.environ["GROK2API_DATA_DIR"] = td
                ctrl = RegistrationController(q, worker_id="x")
                routes = set()
                for i in range(10):
                    job = ctrl.enqueue_after_sso(
                        session_id=f"s{i}",
                        email=f"e{i}@x.com",
                        sso="eyJ",
                        dual_write=True,
                    )
                    routes.add(job.route_id)
                self.assertEqual(routes, {"route-1"})
        finally:
            os.environ.pop("GROK2API_ROUTE_STICKY", None)
            os.environ.pop("GROK2API_DATA_DIR", None)


if __name__ == "__main__":
    unittest.main()
