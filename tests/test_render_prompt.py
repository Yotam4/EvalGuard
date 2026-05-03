"""Prompt-template rendering — placeholder semantics and structured-value
serialization.

The executor's ``_render_prompt`` is the seam where dataset rows meet
prompt templates. RAG rows carry ``contexts`` (a list); text_to_sql
rows carry ``schema_ref`` (sometimes a dict); both must render without
the user manually JSON-serializing in YAML.
"""

from __future__ import annotations

import pytest

from evalguard_cli.local.local_executor import _render_prompt


def test_simple_string_placeholder():
    assert _render_prompt("hi {input}", {"input": "world"}) == "hi world"


def test_input_default_when_missing():
    assert _render_prompt("[{input}]", {}) == "[]"


def test_list_value_is_json_serialized():
    """RAG ``{contexts}``: list values render as JSON, not Python repr."""
    out = _render_prompt("ctx={contexts}", {
        "input": "q", "contexts": ["a", "b"],
    })
    assert out == 'ctx=["a", "b"]'


def test_dict_value_is_json_serialized():
    """text_to_sql ``{schema_ref}``: dict values render as JSON."""
    out = _render_prompt("schema={schema_ref}", {
        "input": "q", "schema_ref": {"table": "users", "cols": ["id", "name"]},
    })
    # JSON dict ordering is preserved by ensure_ascii=False default sort.
    assert out.startswith("schema={")
    assert '"table"' in out and '"users"' in out


def test_unknown_placeholder_raises():
    with pytest.raises(ValueError, match="missing field 'nope'"):
        _render_prompt("hi {nope}", {"input": "x"})


def test_json_braces_in_template_are_left_alone():
    """The placeholder regex requires identifier-shaped names, so example
    JSON inside a prompt template doesn't get mangled."""
    template = 'Reply: {{"summary": "..."}} for {input}'
    out = _render_prompt(template, {"input": "x"})
    assert '{"summary"' in out
    assert out.endswith("for x")


def test_none_value_renders_as_empty_string():
    assert _render_prompt("[{input}]", {"input": None}) == "[]"
