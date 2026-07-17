"""Regression tests for foreground-API resource priority defaults."""

from __future__ import annotations

import importlib
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class ApiPriorityDefaultsTests(unittest.TestCase):
    def test_registration_defaults_to_one_slot_without_prefetch(self) -> None:
        with patch.dict(
            os.environ,
            {
                "GROK2API_REG_CONCURRENCY": "",
                "GROK2API_REG_PREFETCH_SLOTS": "",
            },
            clear=False,
        ):
            os.environ.pop("GROK2API_REG_CONCURRENCY", None)
            os.environ.pop("GROK2API_REG_PREFETCH_SLOTS", None)

            import grok_build_adapter
            import registration_maintainer

            adapter = importlib.reload(grok_build_adapter)
            maintainer = importlib.reload(registration_maintainer)

            self.assertEqual(adapter.DEFAULT_CONCURRENCY, 1)
            self.assertEqual(adapter.REG_PREFETCH_SLOTS, 0)
            self.assertEqual(maintainer._concurrency(), 1)

    def test_server_compose_wires_api_priority_controls(self) -> None:
        compose = (ROOT / "docker-compose.server.yml").read_text(encoding="utf-8")

        self.assertIn('TURNSTILE_THREAD: "${TURNSTILE_THREAD:-1}"', compose)
        self.assertIn(
            'GROK2API_REG_CONCURRENCY: "${GROK2API_REG_CONCURRENCY:-1}"',
            compose,
        )
        self.assertIn(
            'GROK2API_REG_PREFETCH_SLOTS: "${GROK2API_REG_PREFETCH_SLOTS:-0}"',
            compose,
        )
        self.assertIn('TURNSTILE_NICE: "${TURNSTILE_NICE:-10}"', compose)

    def test_entrypoint_runs_solver_below_api_priority(self) -> None:
        entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")

        self.assertIn('solver_nice="${TURNSTILE_NICE:-10}"', entrypoint)
        self.assertIn('nice -n "${solver_nice}" python api_solver.py', entrypoint)
        self.assertIn('solver_nice must be an integer from 0 to 19', entrypoint)
        self.assertIn('10#${solver_nice}', entrypoint)

    def test_standalone_solver_defaults_match_production(self) -> None:
        start = (ROOT / "turnstile-solver" / "start.sh").read_text(
            encoding="utf-8"
        )
        entrypoint = (ROOT / "turnstile-solver" / "entrypoint.sh").read_text(
            encoding="utf-8"
        )
        solver = (ROOT / "turnstile-solver" / "api_solver.py").read_text(
            encoding="utf-8"
        )

        for script in (start, entrypoint):
            self.assertIn('TURNSTILE_THREAD:-1', script)
            self.assertIn('TURNSTILE_NICE:-10', script)
            self.assertIn('nice -n "${NICE}"', script)
        self.assertIn("add_argument('--thread', type=int, default=1", solver)


if __name__ == "__main__":
    unittest.main()
