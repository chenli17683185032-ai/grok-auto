"""Focused regression tests for the production prompt-cache backport."""

from __future__ import annotations

import json
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO

import app
import account_pool
import conversation_affinity as affinity
from auth import GrokCredentials

# Keep unit tests hermetic: affinity policy/storage is not under test here.
affinity._enabled = lambda: True  # type: ignore[method-assign]
affinity._redis_mode = lambda: False  # type: ignore[method-assign]


def _variant_bodies() -> tuple[dict, dict]:
    body_a = {
        "model": "grok-4.5",
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": "You are helpful.\r\n\r\n"}],
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "index": 0,
                        "function": {
                            "name": "Read",
                            "arguments": '{\n  "limit": 10,\n  "path": "a.py"\n}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "  exact output\n"},
            {"role": "user", "content": "next"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "Write",
                    "description": "",
                    "parameters": {
                        "properties": {"x": {"type": "string"}},
                        "type": "object",
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "Read",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            },
        ],
        "metadata": {"volatile_request_id": "one"},
    }
    body_b = {
        "model": "grok-4.5",
        "messages": [
            {"role": "system", "content": "You are helpful.\n"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "arguments": '{"path":"a.py","limit":10}',
                            "name": "Read",
                        },
                        "type": "function",
                        "id": "call_1",
                    }
                ],
                "content": None,
            },
            {"content": "  exact output\n", "tool_call_id": "call_1", "role": "tool"},
            {"content": "next", "role": "user"},
        ],
        "tools": [
            {
                "function": {
                    "parameters": {
                        "properties": {"path": {"type": "string"}},
                        "type": "object",
                    },
                    "name": "Read",
                },
                "type": "function",
            },
            {
                "function": {
                    "name": "Write",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    },
                },
                "type": "function",
            },
        ],
        "metadata": {"volatile_request_id": "two"},
    }
    return body_a, body_b


def test_semantically_equal_requests_stabilize_identically() -> None:
    body_a, body_b = _variant_bodies()
    stats_a = app._stabilize_upstream_prompt_body(body_a)
    stats_b = app._stabilize_upstream_prompt_body(body_b)

    assert stats_a["prefix_hash"] == stats_b["prefix_hash"]
    assert [t["function"]["name"] for t in body_a["tools"]] == ["Read", "Write"]
    assert body_a["tools"] == body_b["tools"]
    assert body_a["messages"] == body_b["messages"]
    assert "metadata" not in body_a
    assert "metadata" not in body_b


def test_stabilization_is_idempotent_and_private_stats_are_not_forwarded() -> None:
    body, _ = _variant_bodies()
    app._stabilize_upstream_prompt_body(body)
    first = deepcopy(body)
    app._stabilize_upstream_prompt_body(body)
    assert body == first

    upstream = app._body_for_upstream(body)
    assert "_prompt_stabilize" not in upstream
    assert "_history_compact" not in upstream
    assert app._prompt_stabilize_headers(body)["X-Grok2API-Prompt-Stable"] == "1"


def test_tool_call_order_and_content_bytes_are_preserved() -> None:
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "Second", "arguments": '{"b":2}'},
                    },
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "First", "arguments": '{"a":1}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "  keep\nspaces\n"},
            {"role": "user", "content": "  user code\n"},
        ]
    }
    app._stabilize_upstream_prompt_body(body)
    names = [
        item["function"]["name"]
        for item in body["messages"][0]["tool_calls"]
    ]
    assert names == ["Second", "First"]
    assert body["messages"][1]["content"] == "  keep\nspaces\n"
    assert body["messages"][2]["content"] == "  user code\n"


def test_prompt_cache_key_does_not_depend_on_message_root() -> None:
    fp_a = affinity.conversation_fingerprint(
        [{"role": "user", "content": "turn a"}],
        api_key_id="key-1",
        prompt_cache_key="session-stable",
    )
    fp_b = affinity.conversation_fingerprint(
        [{"role": "user", "content": "different partial history"}],
        api_key_id="key-1",
        prompt_cache_key="session-stable",
    )
    assert fp_a == fp_b


def test_volatile_system_lines_do_not_change_fallback_affinity() -> None:
    root_a = affinity._conversation_root(
        [
            {
                "role": "system",
                "content": "You are helpful.\nCurrent date: 2026-07-14\ncwd: /tmp/a",
            },
            {"role": "user", "content": "fix the project"},
        ]
    )
    root_b = affinity._conversation_root(
        [
            {
                "role": "system",
                "content": "You are helpful.\nCurrent date: 2026-07-15\ncwd: /tmp/b",
            },
            {"role": "user", "content": "fix the project"},
        ]
    )
    assert root_a == root_b


def test_prompt_cache_header_variants() -> None:
    assert (
        affinity.extract_prompt_cache_key_from_headers(
            {"x-openai-prompt-cache-key": "  session-42  "}
        )
        == "session-42"
    )
    assert affinity.extract_prompt_cache_key_from_headers(
        {"x-prompt-cache-key": "undefined"}
    ) is None


def test_canonical_tool_arguments() -> None:
    assert app._canonical_json_text('{"z":1,"a":{"d":4,"c":3}}') == (
        '{"a":{"c":3,"d":4},"z":1}'
    )
    assert json.loads(app._canonical_json_text({"b": 2, "a": 1}) or "{}") == {
        "a": 1,
        "b": 2,
    }


def test_prompt_log_never_contains_content() -> None:
    body = {
        "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT"}],
        "_prompt_stabilize": {
            "prefix_hash": "0123456789abcdef",
            "messages_stabilized": 1,
            "tools_stabilized": 0,
        },
    }
    output = StringIO()
    with redirect_stdout(output):
        app._log_prompt_stability(
            body,
            request_id="request-123",
            conversation_fp="fp:abcdef0123456789",
        )
    logged = output.getvalue()
    assert "TOP-SECRET-PROMPT" not in logged
    assert "0123456789abcdef" in logged


def test_rr_window_cannot_drop_preferred_sticky_account() -> None:
    """A far RR cursor must not rotate the sticky account out of the window."""
    from store import pool_redis

    credentials = [
        GrokCredentials(
            token=f"token-{index}",
            auth_key=f"https://auth.x.ai::account-{index:03d}",
            user_id=f"user-{index:03d}",
        )
        for index in range(100)
    ]
    preferred = credentials[0]
    replacements = {
        "_ensure_multi_account_layout": lambda: None,
        "peek_credentials_by_id": (
            lambda account_id: preferred
            if account_id == preferred.auth_key
            else None
        ),
        "get_account_pool_meta": lambda _account_id: {"enabled": True},
        "get_cached_live_credentials": lambda **_kwargs: [],
        "get_cached_account_pool_state": lambda: None,
        "list_live_credentials": lambda **_kwargs: list(credentials),
        "get_account_pool_meta_many": lambda ids: {
            account_id: {"enabled": True} for account_id in ids
        },
        "get_account_mode": lambda: "round_robin",
    }
    originals = {
        name: getattr(account_pool, name) for name in replacements
    }
    original_rr_next = pool_redis.rr_next
    try:
        for name, replacement in replacements.items():
            setattr(account_pool, name, replacement)
        pool_redis.rr_next = lambda: 50
        chain = account_pool.try_acquire_sequence(
            max_attempts=4,
            model="grok-4.5",
            prefer_account_id=preferred.auth_key,
        )
    finally:
        for name, original in originals.items():
            setattr(account_pool, name, original)
        pool_redis.rr_next = original_rr_next

    assert len(chain) == 4
    assert chain[0].auth_key == preferred.auth_key
