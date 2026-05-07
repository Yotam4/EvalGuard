"""Provider retry policy + ``provider.retry`` / ``provider.failed`` events.

Three layers are exercised:

1. ``RetryPolicy`` unit tests — pattern matching, backoff math.
2. ``call_with_retry`` semantic tests — exhaustion, on_retry callback,
   instant-success, non-retryable errors.
3. End-to-end through the executor — fault-injection via the mock
   provider's ``fail_first_n`` / ``fail_with`` knobs verifies that
   ``provider.retry`` and ``provider.failed`` audit events are emitted
   correctly and that a final failure marks the row failed without
   killing the trial.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evalguard_cli.local.audit import EVENT_KINDS, verify_chain
from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.retry import (
    ProviderFailed,
    RetryPolicy,
    call_with_retry,
)
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# ---------------------------------------------------------------------------
# 1. RetryPolicy unit


def test_default_patterns_match_429_and_5xx():
    p = RetryPolicy()
    assert p.is_retryable(RuntimeError("429 too many requests"))
    assert p.is_retryable(RuntimeError("HTTP 503 service unavailable"))
    assert p.is_retryable(TimeoutError("connection timed out"))
    assert p.is_retryable(ConnectionError("connection reset by peer"))
    assert not p.is_retryable(ValueError("invalid_request_error"))
    assert not p.is_retryable(KeyError("missing_field"))


def test_delay_grows_exponentially_and_caps():
    p = RetryPolicy(base_delay_ms=100, max_delay_ms=1_000, jitter=0)
    assert p.delay_ms(0) == 100
    assert p.delay_ms(1) == 200
    assert p.delay_ms(2) == 400
    assert p.delay_ms(3) == 800
    assert p.delay_ms(4) == 1_000   # capped
    assert p.delay_ms(10) == 1_000  # capped


def test_jitter_keeps_delay_within_band():
    p = RetryPolicy(base_delay_ms=1_000, max_delay_ms=10_000, jitter=0.5)
    for _ in range(50):
        d = p.delay_ms(0)
        # 1000 ± 500 → [500, 1500]
        assert 500 <= d <= 1_500


def test_event_kinds_registered():
    assert "provider.retry"  in EVENT_KINDS
    assert "provider.failed" in EVENT_KINDS


def test_run_schema_event_kind_includes_retry_kinds():
    """Schema-drift canary: the run schema's ``event.kind`` enum must
    include the new retry kinds (otherwise downstream consumers would
    silently reject them)."""
    import json
    schema = json.loads((Path(__file__).resolve().parents[1]
                         / "packages" / "schemas" / "evalguard.run.schema.json"
                        ).read_text())
    enum = set(schema["$defs"]["event"]["properties"]["kind"]["enum"])
    assert "provider.retry"  in enum
    assert "provider.failed" in enum


# ---------------------------------------------------------------------------
# 2. call_with_retry semantic


def _factory(results):
    """Build a coro_factory that returns/raises from ``results`` in order."""
    iterator = iter(results)

    async def make():
        item = next(iterator)
        if isinstance(item, BaseException):
            raise item
        return item
    return make


def test_call_with_retry_returns_first_success():
    p = RetryPolicy(max_retries=3, base_delay_ms=0, jitter=0)
    out = asyncio.run(call_with_retry(
        coro_factory=_factory(["ok"]),
        policy=p,
    ))
    assert out == "ok"


def test_call_with_retry_recovers_after_n_failures():
    p = RetryPolicy(max_retries=3, base_delay_ms=0, jitter=0)
    seen_retries: list[tuple[int, str, int]] = []

    def on_retry(attempt, exc, delay_ms):
        seen_retries.append((attempt, str(exc)[:20], delay_ms))

    out = asyncio.run(call_with_retry(
        coro_factory=_factory([
            RuntimeError("429 rate limit"),
            RuntimeError("503 unavailable"),
            "recovered",
        ]),
        policy=p,
        on_retry=on_retry,
    ))
    assert out == "recovered"
    # Two retries fired (after attempts 0 and 1 failed).
    assert len(seen_retries) == 2
    assert seen_retries[0][0] == 1
    assert seen_retries[1][0] == 2


def test_call_with_retry_exhausts_and_raises_provider_failed():
    p = RetryPolicy(max_retries=2, base_delay_ms=0, jitter=0)

    with pytest.raises(ProviderFailed) as ei:
        asyncio.run(call_with_retry(
            coro_factory=_factory([
                RuntimeError("429 rate limit"),
                RuntimeError("429 rate limit"),
                RuntimeError("429 rate limit"),
            ]),
            policy=p,
        ))
    fail = ei.value
    # 1 initial + 2 retries = 3 attempts.
    assert len(fail.attempts) == 3
    assert all(a["error"].startswith("429") for a in fail.attempts)


def test_call_with_retry_does_not_retry_non_retryable_errors():
    p = RetryPolicy(max_retries=5, base_delay_ms=0, jitter=0)
    with pytest.raises(ProviderFailed) as ei:
        asyncio.run(call_with_retry(
            coro_factory=_factory([ValueError("invalid api key")]),
            policy=p,
        ))
    # Single attempt — no retries on non-retryable error.
    assert len(ei.value.attempts) == 1


# ---------------------------------------------------------------------------
# 3. End-to-end through the executor


def _seed_run(tmp_path: Path, *, mock_cfg: dict, retry_yaml: str = "") -> tuple[SqliteStore, "object"]:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"a"}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    mock_cfg_yaml = "\n".join(f"      {k}: {v!r}" for k, v in mock_cfg.items())
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config:\n"
        + mock_cfg_yaml + "\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
        + retry_yaml
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return store, record


def test_executor_retries_then_succeeds_emits_retry_events(tmp_path: Path):
    """Two transient failures → two ``provider.retry`` events → one
    eventual ``provider.called`` (the success). No ``provider.failed``.
    Hash chain still verifies."""
    store, record = _seed_run(tmp_path,
        mock_cfg={"mode": "echo", "latency_ms": 0, "fail_first_n": 2},
        retry_yaml="retry: { max_retries: 3, base_delay_ms: 0, jitter: 0 }\n",
    )
    events = store.list_events(record.run_id)
    kinds = [e["kind"] for e in events]
    assert kinds.count("provider.retry") == 2
    assert kinds.count("provider.called") == 1
    assert kinds.count("provider.failed") == 0
    # Per-row state reflects success.
    [trial] = store.list_trials(record.run_id)
    r = store.get_row(record.run_id, "r1", trial_id=trial["trial_id"])
    assert r["output"]   # non-empty completion produced
    assert verify_chain(store, record.run_id)["ok"]


def test_executor_emits_provider_failed_on_exhaustion(tmp_path: Path):
    """Always-fail mock + small budget → max_retries+1 attempts → one
    ``provider.failed``. Row is recorded with no scores; trial doesn't
    crash."""
    store, record = _seed_run(tmp_path,
        mock_cfg={"mode": "echo", "latency_ms": 0,
                  "fail_with": "429 rate limit (always)"},
        retry_yaml="retry: { max_retries: 1, base_delay_ms: 0, jitter: 0 }\n",
    )
    events = store.list_events(record.run_id)
    kinds = [e["kind"] for e in events]
    # Initial attempt fails → 1 retry → still fails → provider.failed.
    assert kinds.count("provider.retry") == 1
    assert kinds.count("provider.failed") == 1
    # ``provider.called`` is only emitted on successful completions.
    assert kinds.count("provider.called") == 0

    failed_event = next(e for e in events if e["kind"] == "provider.failed")
    assert failed_event["payload"]["n_attempts"] == 2  # 1 initial + 1 retry
    assert failed_event["payload"]["error_type"] == "RuntimeError"
    assert "429" in failed_event["payload"]["error"]

    [trial] = store.list_trials(record.run_id)
    r = store.get_row(record.run_id, "r1", trial_id=trial["trial_id"])
    assert r["output"] == ""  # no completion was produced
    assert verify_chain(store, record.run_id)["ok"]


def test_run_continues_when_one_row_exhausts_retries(tmp_path: Path):
    """The plan calls out that today a 429 'bubbles up and aborts'. The
    retry path explicitly fixes this: when one row exhausts retries the
    trial keeps running so the rest of the dataset still produces
    results."""
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"good1","input":"a"}\n'
        # This row uses params override to inject a permanent failure.
        '{"id":"bad","input":"b","params":{"fail_with":"429 always"}}\n'
        '{"id":"good2","input":"c"}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config: { mode: echo, latency_ms: 0 }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
        "concurrency: 1\n"
        "retry: { max_retries: 1, base_delay_ms: 0, jitter: 0 }\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    [trial] = store.list_trials(record.run_id)
    rows = {r["row_id"]: r for r in store.list_rows(record.run_id, trial_id=trial["trial_id"])}
    # All three rows recorded; only the bad one is empty.
    assert set(rows) == {"good1", "bad", "good2"}
    good1 = store.get_row(record.run_id, "good1", trial_id=trial["trial_id"])
    bad   = store.get_row(record.run_id, "bad",   trial_id=trial["trial_id"])
    good2 = store.get_row(record.run_id, "good2", trial_id=trial["trial_id"])
    assert good1["output"] != ""
    assert bad["output"] == ""
    assert good2["output"] != ""

    events = store.list_events(record.run_id)
    failed_events = [e for e in events if e["kind"] == "provider.failed"]
    assert len(failed_events) == 1
    assert failed_events[0]["row_id"] == "bad"
    assert verify_chain(store, record.run_id)["ok"]


def test_pass_rate_counts_retry_failed_row_as_failed(tmp_path: Path):
    """Regression: a row whose provider call exhausted retries used
    to be classified as PASSING in compute_metrics because the old
    query only counted rows that had at least one failed *score* —
    zero-score rows leaked through. After the fix, pass_rate
    correctly reflects the failure: 1 of 2 rows passed → 0.5."""
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"good","input":"q"}\n'
        '{"id":"bad","input":"q","params":{"fail_with":"429 always"}}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
        "concurrency: 1\n"
        "retry: { max_retries: 0, base_delay_ms: 0, jitter: 0 }\n"
        "heuristics: [{ id: len, type: length, max: 1000 }]\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    metrics = store.compute_metrics(record.run_id)
    assert metrics["row_count"] == 2.0
    assert metrics["pass_rate"] == 0.5

    rows = {r["row_id"]: r for r in store.list_rows(record.run_id)}
    assert rows["good"]["passed"] is True
    assert rows["good"]["n_scores"] == 1
    assert rows["bad"]["passed"] is False        # zero-score → fail
    assert rows["bad"]["n_scores"] == 0


def test_row_level_retry_override(tmp_path: Path):
    """A row can carry a top-level ``retry: {...}`` block to override
    the trial-level retry budget on a per-row basis. The override
    must NOT live under ``params`` (which is reserved for SDK config)."""
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text(
        # Row 1: uses trial default (max_retries=1) and fails because
        # fail_first_n=4 needs 4 retries to succeed.
        '{"id":"r1","input":"a","params":{"fail_first_n":4}}\n'
    )
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
        "retry: { max_retries: 1, base_delay_ms: 0, jitter: 0 }\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    events = store.list_events(record.run_id)
    # Trial default of max_retries=1 → 1 retry, then fails.
    assert sum(1 for e in events if e["kind"] == "provider.retry") == 1
    assert sum(1 for e in events if e["kind"] == "provider.failed") == 1

    # Now redo with row-level retry override that's generous enough.
    (tmp_path / "datasets" / "g.jsonl").write_text(
        '{"id":"r2","input":"a","params":{"fail_first_n":4},'
        '"retry":{"max_retries":5,"base_delay_ms":0,"jitter":0}}\n'
    )
    cfg = load_config(cfg_path)
    record2 = asyncio.run(execute(cfg, store=store, quiet=True))
    events2 = store.list_events(record2.run_id)
    # Row-level override wins → 4 retries → succeeds.
    assert sum(1 for e in events2 if e["kind"] == "provider.retry") == 4
    assert sum(1 for e in events2 if e["kind"] == "provider.called") == 1
    assert sum(1 for e in events2 if e["kind"] == "provider.failed") == 0


def test_trial_started_audit_records_full_provider_config_including_retry(tmp_path: Path):
    """Audit fidelity: the ``trial.started`` event must carry the
    user's full retry config so an auditor can reconstruct exactly
    what budget was in force, even though retry is stripped before
    being passed to the SDK / load_provider."""
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text('{"id":"r1","input":"a"}\n')
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config:\n"
        "      mode: echo\n"
        "      latency_ms: 0\n"
        "      retry: { max_retries: 7, base_delay_ms: 50, jitter: 0.1 }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    [trial_started] = [e for e in store.list_events(record.run_id)
                       if e["kind"] == "trial.started"]
    cfg_in_event = trial_started["payload"]["provider_config"]
    assert cfg_in_event.get("retry") == {"max_retries": 7, "base_delay_ms": 50, "jitter": 0.1}
    # And the dedicated ``retry`` field at the top of the payload too.
    assert trial_started["payload"]["retry"]["max_retries"] == 7


def test_per_provider_retry_overrides_run_default(tmp_path: Path):
    """Provider-level ``config.retry`` wins over the run-level default."""
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text('{"id":"r1","input":"a"}\n')
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config:\n"
        "      mode: echo\n"
        "      latency_ms: 0\n"
        "      fail_first_n: 4\n"
        "      retry: { max_retries: 5, base_delay_ms: 0, jitter: 0 }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "cache: { enabled: false }\n"
        # Run-level says max_retries=1 (would not be enough to recover);
        # the per-provider override of 5 should win.
        "retry: { max_retries: 1, base_delay_ms: 0, jitter: 0 }\n"
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))

    events = store.list_events(record.run_id)
    kinds = [e["kind"] for e in events]
    assert kinds.count("provider.retry") == 4  # 4 fails, then succeed on 5th
    assert kinds.count("provider.called") == 1
    assert kinds.count("provider.failed") == 0


def test_retry_loop_aborts_on_cancel_event(monkeypatch):
    """B.1: cost-cap fires from another concurrent row mid-backoff;
    the retry loop must NOT keep waiting and burning the budget.

    The fix: ``call_with_retry`` accepts a ``cancel: asyncio.Event``
    and short-circuits the next attempt + slices the backoff sleep
    so cancellation propagates within ~100 ms of the event being set.
    """
    import asyncio
    from evalguard_cli.local.retry import (
        ProviderFailed, RetryPolicy, call_with_retry,
    )

    cancel = asyncio.Event()
    attempts_made = {"n": 0}

    class _RaisesAlways:
        async def complete(self):
            attempts_made["n"] += 1
            # First attempt raises a retryable, second triggers the
            # cancel event mid-backoff and we expect to never make
            # the third attempt.
            if attempts_made["n"] == 2:
                # Simulate another coroutine flipping the cap.
                cancel.set()
            raise RuntimeError("429 rate limit (mock)")

    async def _drive():
        provider = _RaisesAlways()
        return await call_with_retry(
            coro_factory=provider.complete,
            policy=RetryPolicy(max_retries=10, base_delay_ms=200, jitter=0),
            cancel=cancel,
        )

    with pytest.raises(ProviderFailed) as exc_info:
        asyncio.run(_drive())

    # Cancellation should have prevented attempt 3 (and all the rest).
    assert attempts_made["n"] == 2
    assert exc_info.value.cancelled is True
    # Cancelled failures don't accrue cost (the mock didn't bill).
    assert exc_info.value.total_cost_usd == 0.0


def test_retry_loop_credits_cost_on_failed_attempts():
    """B.1: providers that bill on failure (rare but real for some
    OpenAI partial-generation cases) attach ``cost_usd`` to the
    raised exception. ``ProviderFailed.total_cost_usd`` aggregates
    so the executor can credit it to the run total instead of
    silently under-counting."""
    import asyncio
    from evalguard_cli.local.retry import (
        ProviderFailed, RetryPolicy, call_with_retry,
    )

    class _BillsOnFailure:
        async def complete(self):
            err = RuntimeError("503 service unavailable")
            err.cost_usd = 0.07
            raise err

    async def _drive():
        return await call_with_retry(
            coro_factory=_BillsOnFailure().complete,
            policy=RetryPolicy(max_retries=2, base_delay_ms=0, jitter=0),
        )

    with pytest.raises(ProviderFailed) as exc_info:
        asyncio.run(_drive())
    fail = exc_info.value
    assert len(fail.attempts) == 3
    # 3 attempts × $0.07 each = $0.21
    assert fail.total_cost_usd == pytest.approx(0.21)
    # Per-attempt cost is also recorded.
    assert all(a["cost_usd"] == pytest.approx(0.07) for a in fail.attempts)
