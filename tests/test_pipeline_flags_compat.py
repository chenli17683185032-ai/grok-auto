"""Ensure feature flags default to legacy-compatible behaviour."""

from __future__ import annotations

import os
import unittest


class FlagCompatTests(unittest.TestCase):
    def test_defaults(self) -> None:
        for k in list(os.environ):
            if k.startswith("GROK2API_PIPELINE") or k.startswith("GROK2API_ROUTE") or k.startswith(
                "GROK2API_COOKIE"
            ) or k.startswith("GROK2API_PARALLEL") or k.startswith("GROK2API_ADAPTIVE") or k.startswith(
                "GROK2API_RUYIPAGE_WARM"
            ):
                os.environ.pop(k, None)

        from registration_queue import pipeline_v2_enabled
        from route_registry import route_sticky_enabled
        from registration_controller import parallel_token_poll_enabled, adaptive_enabled
        import cookie_bundle as cb

        self.assertFalse(pipeline_v2_enabled())
        self.assertFalse(route_sticky_enabled())
        self.assertFalse(parallel_token_poll_enabled())
        self.assertFalse(adaptive_enabled())
        self.assertEqual(cb.cookie_mode_default(), "sso_only")
        self.assertEqual(cb.experiment_percent(), 0.0)

    def test_sso_to_token_signature_backward_compatible(self) -> None:
        import inspect
        import sso_to_auth_json as m

        sig = inspect.signature(m.sso_to_token)
        # positional sso_cookie still first
        params = list(sig.parameters.values())
        self.assertEqual(params[0].name, "sso_cookie")
        # optional kwargs have defaults
        for name in (
            "route_id",
            "approver_endpoint",
            "proxy",
            "cookie_bundle_path",
            "cookie_mode",
            "parallel_poll",
        ):
            self.assertIn(name, sig.parameters)
            self.assertIsNot(sig.parameters[name].default, inspect.Parameter.empty)


if __name__ == "__main__":
    unittest.main()
