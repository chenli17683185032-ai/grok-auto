"""Targeted tests for cache-detail passthrough in _normalize_usage / _sse_chunk."""

from __future__ import annotations

import json

import app


def test_standard_cached_tokens_preserved():
    out = app._normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 80},
        }
    )
    assert out["prompt_tokens"] == 100
    assert out["completion_tokens"] == 10
    assert out["total_tokens"] == 110
    assert out["prompt_tokens_details"]["cached_tokens"] == 80


def test_prompt_details_other_fields_preserved():
    out = app._normalize_usage(
        {
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "prompt_tokens_details": {
                "cached_tokens": 20,
                "text_tokens": 50,
                "audio_tokens": 0,
                "image_tokens": 0,
            },
        }
    )
    details = out["prompt_tokens_details"]
    assert details["cached_tokens"] == 20
    assert details["text_tokens"] == 50
    assert details["audio_tokens"] == 0
    assert details["image_tokens"] == 0


def test_input_tokens_details_promoted_to_prompt_details():
    out = app._normalize_usage(
        {
            "prompt_tokens": 200,
            "completion_tokens": 8,
            "input_tokens_details": {
                "cached_tokens": 150,
                "text_tokens": 200,
            },
        }
    )
    assert out["input_tokens_details"]["cached_tokens"] == 150
    assert out["input_tokens_details"]["text_tokens"] == 200
    assert out["prompt_tokens_details"]["cached_tokens"] == 150
    assert out["prompt_tokens_details"]["text_tokens"] == 200


def test_top_level_prompt_cache_hit_tokens():
    out = app._normalize_usage(
        {
            "prompt_tokens": 120,
            "completion_tokens": 4,
            "prompt_cache_hit_tokens": 99,
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 99


def test_top_level_cached_tokens():
    out = app._normalize_usage(
        {
            "prompt_tokens": 120,
            "completion_tokens": 4,
            "cached_tokens": 77,
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 77


def test_cache_read_input_tokens_lower_priority():
    out = app._normalize_usage(
        {
            "prompt_tokens": 120,
            "completion_tokens": 4,
            "cache_read_input_tokens": 55,
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 55

    # Higher-priority positive wins over cache_read_input_tokens.
    out2 = app._normalize_usage(
        {
            "prompt_tokens": 120,
            "completion_tokens": 4,
            "cached_tokens": 40,
            "cache_read_input_tokens": 55,
        }
    )
    assert out2["prompt_tokens_details"]["cached_tokens"] == 40


def test_zero_standard_prefers_positive_compat():
    out = app._normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 0},
            "prompt_cache_hit_tokens": 42,
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 42


def test_positive_conflict_uses_fixed_priority():
    out = app._normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 10},
            "input_tokens_details": {"cached_tokens": 20},
            "prompt_cache_hit_tokens": 30,
            "cached_tokens": 40,
            "cache_read_input_tokens": 50,
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 10


def test_invalid_cache_values_rejected():
    out = app._normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": -5},
            "prompt_cache_hit_tokens": True,
            "cached_tokens": "nope",
            "cache_read_input_tokens": 3.5,
        }
    )
    # No valid non-negative integer → do not invent a cache field.
    assert "prompt_tokens_details" not in out or "cached_tokens" not in out.get(
        "prompt_tokens_details", {}
    )
    # prompt_tokens_details may be absent entirely when only invalid values existed
    # and the details dict itself was only for invalid cached_tokens; if present
    # without a valid cached_tokens key, that is also fine.
    if "prompt_tokens_details" in out:
        assert "cached_tokens" not in out["prompt_tokens_details"] or out[
            "prompt_tokens_details"
        ].get("cached_tokens") is None


def test_invalid_does_not_override_valid_zero():
    out = app._normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 3,
            "prompt_tokens_details": {"cached_tokens": 0, "text_tokens": 100},
            "cached_tokens": -1,
            "prompt_cache_hit_tokens": False,
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 0
    assert out["prompt_tokens_details"]["text_tokens"] == 100


def test_prompt_completion_total_unchanged():
    out = app._normalize_usage(
        {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
            "prompt_tokens_details": {"cached_tokens": 5},
        }
    )
    assert out["prompt_tokens"] == 11
    assert out["completion_tokens"] == 7
    assert out["total_tokens"] == 18
    # Cache must NOT be subtracted from prompt_tokens.
    assert out["prompt_tokens"] == 11


def test_fallback_when_prompt_or_completion_missing():
    out = app._normalize_usage(
        {"total_tokens": 30},
        prompt_fallback=20,
        completion_fallback=10,
    )
    assert out["prompt_tokens"] == 20
    assert out["completion_tokens"] == 10
    assert out["total_tokens"] == 30
    assert "prompt_tokens_details" not in out

    out2 = app._normalize_usage(
        None,
        prompt_fallback=15,
        completion_fallback=5,
    )
    assert out2 == {
        "prompt_tokens": 15,
        "completion_tokens": 5,
        "total_tokens": 20,
    }


def test_sse_chunk_terminal_includes_nested_cache():
    usage = app._normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 60, "text_tokens": 100},
        }
    )
    frame = app._sse_chunk(
        chat_id="chatcmpl-test",
        model="grok-4.5",
        created=1,
        finish_reason="stop",
        usage=usage,
    )
    assert frame.startswith("data: ")
    payload = json.loads(frame[len("data: ") :].strip())
    assert payload["usage"]["prompt_tokens"] == 100
    assert payload["usage"]["completion_tokens"] == 8
    assert payload["usage"]["prompt_tokens_details"]["cached_tokens"] == 60
    assert payload["usage"]["prompt_tokens_details"]["text_tokens"] == 100


def test_no_cache_fields_does_not_fabricate():
    out = app._normalize_usage(
        {
            "prompt_tokens": 40,
            "completion_tokens": 6,
            "total_tokens": 46,
        }
    )
    assert out == {
        "prompt_tokens": 40,
        "completion_tokens": 6,
        "total_tokens": 46,
    }
    assert "prompt_tokens_details" not in out
    assert "input_tokens_details" not in out
    assert "completion_tokens_details" not in out


def test_completion_tokens_details_passthrough():
    out = app._normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "completion_tokens_details": {
                "reasoning_tokens": 12,
                "accepted_prediction_tokens": 0,
            },
        }
    )
    assert out["completion_tokens_details"]["reasoning_tokens"] == 12
    assert out["completion_tokens_details"]["accepted_prediction_tokens"] == 0
    assert "prompt_tokens_details" not in out


def test_input_tokens_alias_for_prompt():
    out = app._normalize_usage(
        {
            "input_tokens": 33,
            "output_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 12},
        }
    )
    assert out["prompt_tokens"] == 33
    assert out["completion_tokens"] == 4
    assert out["prompt_tokens_details"]["cached_tokens"] == 12


def test_details_shallow_copy_is_independent():
    original = {"cached_tokens": 9, "text_tokens": 40}
    usage = {
        "prompt_tokens": 40,
        "completion_tokens": 1,
        "prompt_tokens_details": original,
    }
    out = app._normalize_usage(usage)
    out["prompt_tokens_details"]["cached_tokens"] = 999
    assert original["cached_tokens"] == 9


def test_explicit_zero_without_positive_compat():
    out = app._normalize_usage(
        {
            "prompt_tokens": 50,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
    )
    assert out["prompt_tokens_details"]["cached_tokens"] == 0


def test_usage_from_body_and_output_preserves_cache():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = app._usage_from_body_and_output(
        body,
        content="hello",
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "prompt_tokens_details": {"cached_tokens": 7},
        },
    )
    assert out["prompt_tokens"] == 10
    assert out["completion_tokens"] == 2
    assert out["prompt_tokens_details"]["cached_tokens"] == 7
