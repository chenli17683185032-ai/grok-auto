"""Focused regressions for inline Turnstile browser-pool recovery."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
SOLVER_DIR = ROOT / "turnstile-solver"


class _FakeQuart:
    def __init__(self, _name: str):
        self.routes: dict[str, object] = {}

    def before_serving(self, func):
        return func

    def route(self, path: str, **_kwargs):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator


def _load_api_solver():
    quart = types.ModuleType("quart")
    quart.Quart = _FakeQuart
    quart.request = types.SimpleNamespace()
    quart.jsonify = lambda value: value
    sys.modules["quart"] = quart

    camoufox = types.ModuleType("camoufox")
    camoufox_async = types.ModuleType("camoufox.async_api")
    camoufox_async.AsyncCamoufox = object
    camoufox.async_api = camoufox_async
    sys.modules["camoufox"] = camoufox
    sys.modules["camoufox.async_api"] = camoufox_async

    patchright = types.ModuleType("patchright")
    patchright_async = types.ModuleType("patchright.async_api")
    patchright_async.async_playwright = lambda: None
    patchright.async_api = patchright_async
    sys.modules["patchright"] = patchright
    sys.modules["patchright.async_api"] = patchright_async

    sys.path.insert(0, str(SOLVER_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "turnstile_api_solver_under_test", SOLVER_DIR / "api_solver.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SOLVER_DIR))


API_SOLVER = _load_api_solver()


class _FakeBrowser:
    def __init__(self, *, connected: bool = True):
        self.connected = connected
        self.close_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    async def close(self) -> None:
        self.close_calls += 1
        self.connected = False


def _server(*, threads: int = 2):
    server = API_SOLVER.TurnstileAPIServer(
        headless=True,
        useragent=None,
        debug=False,
        browser_type="camoufox",
        thread=threads,
        proxy_support=False,
    )
    server._pool_lock = asyncio.Lock()
    server.browser_acquire_timeout = 0.02
    server.browser_rebuild_timeout = 0.2
    return server


class BrowserPoolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_eager_startup_uses_bounded_pool_rebuild_path(self):
        server = _server(threads=1)
        server.lazy_browsers = False
        server.idle_sec = 0
        server.display_welcome = lambda: None
        server._ensure_pool = AsyncMock()

        with patch.object(API_SOLVER, "init_db", AsyncMock()):
            await server._startup()

        server._ensure_pool.assert_awaited_once_with(
            reason="eager-startup", force=True
        )

    async def test_empty_ready_pool_rebuilds_once_for_concurrent_waiters(self):
        server = _server()
        server._pool_ready = True
        server._shutdown_browsers = AsyncMock()
        calls = 0

        async def initialize():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            items = [
                (index, _FakeBrowser(), {"index": index})
                for index in range(1, 3)
            ]
            server._owned_browsers = list(items)
            for item in items:
                await server.browser_pool.put(item)
            server._pool_ready = True

        server._initialize_browser = initialize
        await asyncio.gather(
            server._ensure_pool(reason="waiter-a"),
            server._ensure_pool(reason="waiter-b"),
        )

        self.assertEqual(calls, 1)
        self.assertEqual(server.browser_pool.qsize(), 2)
        self.assertEqual(server._pool_rebuild_count, 1)

    async def test_force_request_skips_rebuild_after_peer_restores_full_pool(self):
        server = _server(threads=2)
        items = [
            (index, _FakeBrowser(), {"index": index})
            for index in range(1, 3)
        ]
        server._owned_browsers = list(items)
        server._pool_ready = True
        for item in items:
            await server.browser_pool.put(item)
        server._rebuild_pool_locked = AsyncMock()

        await server._ensure_pool(reason="stale-force", force=True)

        server._rebuild_pool_locked.assert_not_awaited()

    async def test_waiter_is_not_counted_as_an_active_browser_lease(self):
        server = _server(threads=1)
        server._pool_ready = True
        server._in_flight = 0

        async def initialize():
            item = (1, _FakeBrowser(), {"index": 1})
            server._owned_browsers = [item]
            await server.browser_pool.put(item)
            server._pool_ready = True

        server._shutdown_browsers = AsyncMock()
        server._initialize_browser = initialize

        item = await server._acquire_browser()
        self.assertEqual(item[0], 1)
        self.assertEqual(server._in_flight, 1)
        self.assertEqual(server._waiting_for_browser, 0)

    async def test_busy_pool_acquire_has_timeout_without_destructive_rebuild(self):
        server = _server()
        server._pool_ready = True
        server._in_flight = 2
        server._rebuild_pool_locked = AsyncMock()

        with self.assertRaises(API_SOLVER.BrowserPoolAcquireTimeout):
            await server._acquire_browser()

        server._rebuild_pool_locked.assert_not_awaited()
        self.assertEqual(server._pool_acquire_timeouts, 1)
        self.assertEqual(server._waiting_for_browser, 0)

    async def test_force_rebuild_is_ignored_while_a_real_lease_is_active(self):
        server = _server(threads=1)
        browser = _FakeBrowser()
        queued = (1, browser, {"index": 1})
        server._owned_browsers = [queued]
        server._pool_ready = True
        await server.browser_pool.put(queued)

        leased = await server._acquire_browser()
        server._rebuild_pool_locked = AsyncMock()
        await server._ensure_pool(reason="concurrent-waiter", force=True)

        server._rebuild_pool_locked.assert_not_awaited()
        self.assertEqual(server._in_flight, 1)
        self.assertTrue(server.browser_pool.empty())
        await server._finalize_browser_lease(leased, None)

    async def test_disconnected_browser_is_discarded_and_never_requeued(self):
        server = _server(threads=1)
        dead = _FakeBrowser(connected=False)
        dead_item = (1, dead, {"index": 1})
        server._owned_browsers = [dead_item]
        server._pool_ready = True
        await server.browser_pool.put(dead_item)

        healthy = _FakeBrowser()

        async def initialize():
            item = (1, healthy, {"index": 1})
            server._owned_browsers = [item]
            await server.browser_pool.put(item)
            server._pool_ready = True

        server._shutdown_browsers = AsyncMock()
        server._initialize_browser = initialize

        acquired = await server._acquire_browser()
        self.assertIs(acquired[1], healthy)
        self.assertEqual(dead.close_calls, 1)
        self.assertNotIn(dead_item, server._owned_browsers)
        self.assertTrue(server.browser_pool.empty())

    async def test_dead_empty_pool_closes_stale_driver_before_rebuild(self):
        server = _server(threads=1)

        class Driver:
            def __init__(self):
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1

        driver = Driver()
        server._camoufox_managers = [driver]
        server._pool_ready = False

        async def initialize():
            item = (1, _FakeBrowser(), {"index": 1})
            server._owned_browsers = [item]
            await server.browser_pool.put(item)
            server._pool_ready = True

        server._initialize_browser = initialize
        leased = await server._acquire_browser()

        self.assertEqual(driver.close_calls, 1)
        self.assertEqual(server._pool_rebuild_count, 1)
        await server._finalize_browser_lease(leased, None)

    async def test_camoufox_uses_and_closes_one_manager_per_browser(self):
        server = _server(threads=2)
        managers = []

        class Manager:
            def __init__(self, **_kwargs):
                self.browser = _FakeBrowser()
                self.exit_calls = 0
                managers.append(self)

            async def start(self):
                return self.browser

            async def __aexit__(self, *_args):
                self.exit_calls += 1

        with patch.object(API_SOLVER, "AsyncCamoufox", Manager):
            await server._initialize_browser()
            self.assertEqual(len(managers), 2)
            self.assertEqual(len(server._camoufox_managers), 2)
            self.assertEqual(server.browser_pool.qsize(), 2)

            await server._shutdown_browsers()

        self.assertEqual([manager.exit_calls for manager in managers], [1, 1])
        self.assertEqual(server._camoufox_managers, [])
        self.assertTrue(server.browser_pool.empty())

    async def test_two_dead_slots_trigger_only_one_rebuild(self):
        server = _server(threads=2)
        dead_items = [
            (index, _FakeBrowser(connected=False), {"index": index})
            for index in range(1, 3)
        ]
        server._owned_browsers = list(dead_items)
        server._pool_ready = True
        for item in dead_items:
            await server.browser_pool.put(item)

        rebuilds = 0

        async def initialize():
            nonlocal rebuilds
            rebuilds += 1
            items = [
                (index, _FakeBrowser(), {"index": index})
                for index in range(1, 3)
            ]
            server._owned_browsers = list(items)
            for item in items:
                await server.browser_pool.put(item)
            server._pool_ready = True

        server._initialize_browser = initialize
        leased = await asyncio.gather(
            server._acquire_browser(),
            server._acquire_browser(),
        )

        self.assertEqual(rebuilds, 1)
        self.assertEqual(server._pool_rebuild_count, 1)
        self.assertEqual(server._browser_disconnects, 2)
        await asyncio.gather(
            *(server._finalize_browser_lease(item, None) for item in leased)
        )

    async def test_release_drops_browser_that_disconnected_during_solve(self):
        server = _server(threads=1)
        browser = _FakeBrowser(connected=False)
        item = (1, browser, {"index": 1})
        server._owned_browsers = [item]
        server._pool_ready = True

        reusable = await server._release_browser(item)

        self.assertFalse(reusable)
        self.assertTrue(server.browser_pool.empty())
        self.assertNotIn(item, server._owned_browsers)
        self.assertEqual(browser.close_calls, 1)

    async def test_partial_capacity_rebuilds_after_last_lease_returns(self):
        server = _server(threads=2)
        healthy = _FakeBrowser()
        dead = _FakeBrowser(connected=False)
        healthy_item = (1, healthy, {"index": 1})
        dead_item = (2, dead, {"index": 2})
        server._owned_browsers = [healthy_item, dead_item]
        server._pool_ready = True
        server._in_flight = 2
        rebuild = AsyncMock()
        server._rebuild_pool_locked = rebuild

        await server._finalize_browser_lease(dead_item, None)
        rebuild.assert_not_awaited()
        self.assertEqual(server._in_flight, 1)

        await server._finalize_browser_lease(healthy_item, None)
        rebuild.assert_awaited_once_with("browser-disconnected-after-solve")
        self.assertEqual(server._in_flight, 0)

    async def test_health_snapshot_distinguishes_idle_and_dead_pool(self):
        server = _server(threads=2)
        idle = server._pool_status()
        self.assertTrue(idle["healthy"])
        self.assertEqual(idle["state"], "idle")

        server._camoufox_managers = [object()]
        leaked_driver = server._pool_status()
        self.assertFalse(leaked_driver["healthy"])
        self.assertEqual(leaked_driver["state"], "degraded")
        server._camoufox_managers = []

        server._pool_ready = True
        server._waiting_for_browser = 1
        dead = server._pool_status()
        self.assertFalse(dead["healthy"])
        self.assertEqual(dead["state"], "degraded")
        self.assertEqual(dead["available_browsers"], 0)

        server._waiting_for_browser = 0
        browser = _FakeBrowser()
        server._owned_browsers = [(1, browser, {"index": 1})]
        await server.browser_pool.put(server._owned_browsers[0])
        partial = server._pool_status()
        self.assertFalse(partial["healthy"])
        self.assertEqual(partial["state"], "degraded")
        self.assertEqual(partial["desired_browsers"], 2)

    async def test_rebuild_failure_is_bounded_and_leaves_retryable_pool(self):
        server = _server(threads=1)
        server._initialize_browser = AsyncMock(side_effect=RuntimeError("boom"))
        server._shutdown_browsers = AsyncMock()

        with self.assertRaises(RuntimeError):
            await server._ensure_pool(reason="test-failure")

        self.assertFalse(server._pool_ready)
        self.assertEqual(server._pool_rebuild_count, 1)
        self.assertEqual(server._pool_rebuild_failures, 1)
        self.assertIn("boom", server._last_pool_error)

    async def test_cancelled_rebuild_closes_partially_initialized_browser(self):
        server = _server(threads=1)
        browser = _FakeBrowser()
        started = asyncio.Event()

        async def initialize():
            item = (1, browser, {"index": 1})
            server._owned_browsers = [item]
            await server.browser_pool.put(item)
            started.set()
            await asyncio.Event().wait()

        server._initialize_browser = initialize
        rebuild = asyncio.create_task(
            server._ensure_pool(reason="cancel-partial-rebuild")
        )
        await asyncio.wait_for(started.wait(), timeout=0.2)
        rebuild.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await rebuild

        self.assertEqual(browser.close_calls, 1)
        self.assertEqual(server._owned_browsers, [])
        self.assertTrue(server.browser_pool.empty())
        self.assertFalse(server._pool_ready)
        self.assertEqual(server._pool_rebuild_failures, 1)
        self.assertEqual(server._last_pool_error, "cancelled")

    async def test_context_creation_error_always_releases_lease(self):
        server = _server(threads=1)

        class ContextFailBrowser(_FakeBrowser):
            async def new_context(self, **_kwargs):
                raise RuntimeError("context failed")

        browser = ContextFailBrowser()
        item = (1, browser, {"index": 1})
        server._owned_browsers = [item]
        server._pool_ready = True
        await server.browser_pool.put(item)

        await server._solve_turnstile("task-1", "https://example.test", "sitekey")

        self.assertEqual(server._in_flight, 0)
        self.assertEqual(server._waiting_for_browser, 0)
        self.assertEqual(server.browser_pool.qsize(), 1)
        self.assertEqual(server._consecutive_failures, 1)

    async def test_cancelled_solve_closes_context_and_releases_lease(self):
        server = _server(threads=1)
        page_started = asyncio.Event()

        class BlockingPage:
            async def set_viewport_size(self, _size):
                return None

            async def add_init_script(self, _script):
                return None

            async def route(self, _pattern, _handler):
                return None

            async def goto(self, *_args, **_kwargs):
                page_started.set()
                await asyncio.Event().wait()

        class Context:
            def __init__(self):
                self.close_calls = 0

            async def new_page(self):
                return BlockingPage()

            async def close(self):
                self.close_calls += 1

        context = Context()

        class Browser(_FakeBrowser):
            async def new_context(self, **_kwargs):
                return context

        browser = Browser()
        item = (1, browser, {"index": 1})
        server._owned_browsers = [item]
        server._pool_ready = True
        await server.browser_pool.put(item)

        solve = asyncio.create_task(
            server._solve_turnstile(
                "task-cancel", "https://example.test", "sitekey"
            )
        )
        await asyncio.wait_for(page_started.wait(), timeout=0.2)
        solve.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await solve

        self.assertEqual(context.close_calls, 1)
        self.assertEqual(server._in_flight, 0)
        self.assertEqual(server._waiting_for_browser, 0)
        self.assertEqual(server.browser_pool.qsize(), 1)

    async def test_success_resets_consecutive_failure_counter(self):
        server = _server(threads=1)
        server._record_failure("first")
        server._record_failure("second")

        server._record_success()

        self.assertEqual(server._consecutive_failures, 0)
        self.assertEqual(server._total_failures, 2)
        self.assertEqual(server._total_successes, 1)
        self.assertGreater(server._last_success_at, 0)


class LocalEndpointFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "grok-build-auth"))
        from xconsole_client.solver import YesCaptchaSolver

        cls.solver_class = YesCaptchaSolver

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(ROOT / "grok-build-auth"))

    def test_loopback_endpoint_uses_only_supported_standard_task(self):
        solver = self.solver_class("local", endpoint="http://127.0.0.1:5072")
        self.assertEqual(
            solver._turnstile_task_types(premium=False, fallback_non_premium=True),
            ["TurnstileTaskProxyless"],
        )
        self.assertEqual(
            solver._turnstile_task_types(premium=True, fallback_non_premium=True),
            ["TurnstileTaskProxyless"],
        )

    def test_external_endpoint_retains_premium_fallback(self):
        solver = self.solver_class("external", endpoint="https://api.yescaptcha.com")
        self.assertEqual(
            solver._turnstile_task_types(premium=False, fallback_non_premium=True),
            ["TurnstileTaskProxyless", "TurnstileTaskProxylessM1"],
        )


if __name__ == "__main__":
    unittest.main()
