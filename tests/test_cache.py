"""ContentCache: key stability and round-trip."""

from __future__ import annotations

from pathlib import Path

from evalguard_cli.local.cache import ContentCache


def test_key_is_stable_for_same_inputs() -> None:
    a = ContentCache.key("openai", "gpt-4o", "hello", None, {"x": 1})
    b = ContentCache.key("openai", "gpt-4o", "hello", None, {"x": 1})
    assert a == b


def test_key_changes_when_any_input_changes() -> None:
    base = ContentCache.key("openai", "gpt-4o", "hello", None, {"x": 1})
    assert base != ContentCache.key("openai", "gpt-4o", "hello!", None, {"x": 1})
    assert base != ContentCache.key("openai", "gpt-5",  "hello",  None, {"x": 1})
    assert base != ContentCache.key("anthropic", "gpt-4o", "hello", None, {"x": 1})
    assert base != ContentCache.key("openai", "gpt-4o", "hello", {"t": 0}, {"x": 1})
    assert base != ContentCache.key("openai", "gpt-4o", "hello", None, {"x": 2})


def test_put_and_get_roundtrip(tmp_path: Path) -> None:
    c = ContentCache(tmp_path / "cache")
    key = ContentCache.key("p", "m", "prompt", None, "input")
    assert c.get(key) is None
    c.put(key, {"output": "hi", "latency_ms": 7, "raw": {}})
    assert c.get(key) == {"output": "hi", "latency_ms": 7, "raw": {}}


def test_disabled_cache_never_writes(tmp_path: Path) -> None:
    c = ContentCache(tmp_path / "cache", enabled=False)
    key = "abc"
    c.put(key, {"output": "x"})
    assert c.get(key) is None
