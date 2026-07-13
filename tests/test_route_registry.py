"""Unit tests for route affinity registry."""

from __future__ import annotations

import os
import unittest

from route_registry import (
    Route,
    RouteRegistry,
    reset_registry_for_tests,
    route_sticky_enabled,
)


class RouteRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.routes = (
            Route(
                route_id="route-1",
                register_proxy="http://p1:7890",
                token_proxy="http://p1:7890",
                approver="http://a1:8765",
                approver_id="approver-1",
                mihomo_id="mihomo-1",
            ),
            Route(
                route_id="route-2",
                register_proxy="http://p2:7890",
                token_proxy="http://p2:7890",
                approver="http://a2:8765",
                approver_id="approver-2",
                mihomo_id="mihomo-2",
            ),
        )
        self.reg = reset_registry_for_tests(self.routes)

    def test_stable_assignment(self) -> None:
        a = self.reg.assign_route("sess-abc")
        b = self.reg.assign_route("sess-abc")
        self.assertEqual(a, b)
        self.assertIn(a, {"route-1", "route-2"})

    def test_prefer_binding(self) -> None:
        rid = self.reg.assign_route("sess-pref", prefer="route-2")
        self.assertEqual(rid, "route-2")
        self.assertEqual(self.reg.assign_route("sess-pref"), "route-2")

    def test_bind_existing_restore(self) -> None:
        self.reg.bind_existing("sess-x", "route-1")
        self.assertEqual(self.reg.assign_route("sess-x"), "route-1")
        self.assertEqual(self.reg.proxy_for("route-1", "token"), "http://p1:7890")
        self.assertEqual(self.reg.approver_for("route-2"), "http://a2:8765")

    def test_unknown_route(self) -> None:
        with self.assertRaises(KeyError):
            self.reg.get("route-99")

    def test_round_robin_pins(self) -> None:
        r1 = self.reg.assign_round_robin("rr-1")
        r2 = self.reg.assign_round_robin("rr-2")
        self.assertEqual(self.reg.assign_round_robin("rr-1"), r1)
        self.assertIn(r1, {"route-1", "route-2"})
        self.assertIn(r2, {"route-1", "route-2"})

    def test_flag_default_off(self) -> None:
        os.environ.pop("GROK2API_ROUTE_STICKY", None)
        self.assertFalse(route_sticky_enabled())
        os.environ["GROK2API_ROUTE_STICKY"] = "1"
        self.assertTrue(route_sticky_enabled())
        os.environ["GROK2API_ROUTE_STICKY"] = "0"


if __name__ == "__main__":
    unittest.main()
