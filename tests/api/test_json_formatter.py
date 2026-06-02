"""Direct unit coverage for the JSON-aware log formatter.

Round-4 ultra-review (Agent-2 H + Agent-3 E).  Five behaviours pinned:

1. A message carrying ``"evt"`` and parseable as JSON → emitted raw.
2. Plain text → wrapped in the standard ``%(asctime)s %(levelname)s ...`` prefix.
3. JSON dict WITHOUT ``"evt"`` → plain-text wrap (defence against
   user-controlled JSON-shaped strings being treated as first-party).
4. ``ts`` and ``level`` injected when absent.
5. Existing ``level`` in the message is NOT overridden.
"""

from __future__ import annotations

import json
import logging

from evalguard_api.main import _JsonAwareFormatter


def _make_record(msg: str, *, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="evalguard.api.test", level=level, pathname=__file__,
        lineno=0, msg=msg, args=(), exc_info=None,
    )


def test_json_message_with_evt_emitted_raw():
    fmt = _JsonAwareFormatter()
    msg = json.dumps({"evt": "http.request", "path": "/v1/runs"})
    out = fmt.format(_make_record(msg))
    parsed = json.loads(out)
    assert parsed["evt"]    == "http.request"
    assert parsed["path"]   == "/v1/runs"
    assert parsed["level"]  == "info"
    assert "ts" in parsed


def test_plain_text_stays_plain():
    fmt = _JsonAwareFormatter()
    out = fmt.format(_make_record("alembic upgrading to head"))
    # Plain-text format is "%(asctime)s %(levelname)s %(name)s %(message)s".
    assert "alembic upgrading to head" in out
    assert "INFO" in out
    assert "evalguard.api.test" in out
    # And critically, it is NOT a JSON object.
    assert not out.strip().startswith("{")


def test_json_dict_without_evt_treated_as_plain():
    """Defence against log injection: user-controlled JSON-shaped
    strings that happen to land in a logger.info call MUST NOT be
    emitted as first-party structured output."""
    fmt = _JsonAwareFormatter()
    user_controlled = json.dumps({"actually": "attacker-supplied"})
    out = fmt.format(_make_record(user_controlled))
    # Plain-text path: user string appears verbatim INSIDE the wrapper.
    assert user_controlled in out
    assert "INFO" in out


def test_existing_level_in_message_not_overridden():
    fmt = _JsonAwareFormatter()
    msg = json.dumps({"evt": "explicit", "level": "DEBUG"})
    out = fmt.format(_make_record(msg, level=logging.ERROR))
    parsed = json.loads(out)
    assert parsed["level"] == "DEBUG"  # caller wins, not the record


def test_existing_ts_in_message_not_overridden():
    fmt = _JsonAwareFormatter()
    msg = json.dumps({"evt": "explicit", "ts": "2020-01-01T00:00:00Z"})
    out = fmt.format(_make_record(msg))
    parsed = json.loads(out)
    assert parsed["ts"] == "2020-01-01T00:00:00Z"


def test_malformed_json_with_braces_treated_as_plain():
    fmt = _JsonAwareFormatter()
    msg = "{this isnt actually json}"
    out = fmt.format(_make_record(msg))
    # Falls back to plain wrap.
    assert msg in out
    assert "INFO" in out
