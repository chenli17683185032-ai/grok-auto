"""Tests for the bounded registration feedback controller."""

from __future__ import annotations

import unittest

from registration_controller import (
    AdaptiveRegistrationController,
    ResourceSnapshot,
)


def _snapshot(
    *,
    cpu: float | None = 60.0,
    memory: int = 4 * 1024 * 1024 * 1024,
    reg_memory: int = 900_000_000,
    pids: int = 100,
    oom: int = 0,
    api_ok: bool = True,
    api_age: float | None = 1.0,
    p95: float | None = 200.0,
    samples: int = 20,
    errors: float = 0.0,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        at=100.0,
        cpu_idle_pct=cpu,
        mem_available_bytes=memory,
        registration_memory_bytes=reg_memory,
        registration_pids=pids,
        registration_oom_kill=oom,
        api_healthy=api_ok,
        api_age_sec=api_age,
        api_local_p95_ms=p95,
        api_sample_count=samples,
        api_error_rate=errors,
    )


class RegistrationControllerTests(unittest.TestCase):
    def _controller(self) -> AdaptiveRegistrationController:
        return AdaptiveRegistrationController(
            promote_after_samples=2,
            cooldown_sec=30,
            startup_oom_kill=0,
        )

    def test_first_cpu_sample_holds_one_slot(self) -> None:
        decision = self._controller().evaluate(
            _snapshot(cpu=None), now=100.0, promotion_ready=False
        )
        self.assertFalse(decision.stop)
        self.assertEqual(decision.allowed_concurrency, 1)

    def test_requires_single_slot_success_before_promotion(self) -> None:
        controller = self._controller()
        first = controller.evaluate(_snapshot(), now=100.0, promotion_ready=False)
        second = controller.evaluate(_snapshot(), now=101.0, promotion_ready=False)
        self.assertEqual(first.allowed_concurrency, 1)
        self.assertEqual(second.allowed_concurrency, 1)
        self.assertFalse(second.promoted)

        controller.evaluate(_snapshot(), now=102.0, promotion_ready=True)
        promoted = controller.evaluate(_snapshot(), now=103.0, promotion_ready=True)
        self.assertTrue(promoted.promoted)
        self.assertEqual(promoted.allowed_concurrency, 2)

    def test_cpu_memory_and_api_limits_stop_registration(self) -> None:
        cases = (
            (_snapshot(cpu=24.9), "host_cpu_idle"),
            (_snapshot(memory=3 * 1024 * 1024 * 1024 - 1), "mem_available"),
            (_snapshot(reg_memory=1_900_000_001), "registration_memory"),
            (_snapshot(pids=301), "registration_pids"),
            (_snapshot(oom=1), "registration_oom_kill"),
            (_snapshot(p95=501.0), "api_local_p95"),
            (_snapshot(api_ok=False), "api_guard_unhealthy"),
        )
        for snapshot, reason in cases:
            with self.subTest(reason=reason):
                decision = self._controller().evaluate(snapshot, now=100.0)
                self.assertTrue(decision.stop)
                self.assertIn(reason, decision.reason)
                self.assertEqual(decision.allowed_concurrency, 1)

    def test_api_latency_is_not_gated_until_enough_samples(self) -> None:
        controller = self._controller()
        decision = controller.evaluate(
            _snapshot(p95=None, samples=0), now=100.0, promotion_ready=True
        )
        self.assertFalse(decision.stop)


if __name__ == "__main__":
    unittest.main()
