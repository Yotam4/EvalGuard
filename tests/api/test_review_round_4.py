"""Round-4 review regressions — Phase 3a OTLP audit + deferred E items.

Each test pins a finding from the multi-agent review of commit
``8b7ff73`` (Phase 3a: OTLP / gen_ai.* ingest) or closes out the
deferred items from round-3 (Phase A-E).

OTLP-specific coverage lives in ``test_otlp.py`` next to the rest of
the OTLP path; this file holds the cross-cutting regressions that
don't naturally fit there.
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# E.2 (deferred) — RunOut secret-shape denylist on read is intentionally
# NOT applied. ``redact_secrets`` runs at write time; on read we honour
# whatever the trusted CLI / OTLP-synthesizer produced.


def test_run_out_does_not_strip_secret_shaped_keys_on_read(client, auth_headers):
    """E.2: RunOut's ``extra='allow'`` round-trips any field the
    ingest accepted. The deliberate trade-off: stripping ``password``
    / ``api_key`` / ``secret`` keys on read would erase fields that
    legitimately carry those names in a security-eval dataset.

    The CLI's ``redact_secrets`` walks audit payloads at write time;
    the response shape itself is intentionally loose.
    """
    # The strict ``RunIngest`` allows arbitrary fields under
    # ``trials[*].rows[*]`` (Row is ``extra='allow'`` to support
    # domain-specific keys). Push a row carrying a ``password`` field
    # to demonstrate it round-trips.
    payload = {
        "schema_version": "1.0.0",
        "run_id":          "run_trustinput12345",
        "project":         "default",
        "trials": [
            {
                "trial_id":    "trial_trustinput0001",
                "provider_id": "mock:m",
                "provider":    "mock",
                "model":       "m",
                "rows": [
                    {
                        "row_id":   "row_pw_detection_01",
                        "passed":   True,
                        "tags":     ["safety", "pii"],
                        # Domain-specific field — a PII detector eval
                        # legitimately surfaces the secret-shaped key.
                        "password": "hunter2",
                        "scores":   [],
                    },
                ],
            },
        ],
    }
    r = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert r.status_code in (200, 201), r.text

    fetched = client.get("/v1/runs/run_trustinput12345", headers=auth_headers).json()
    row = fetched["trials"][0]["rows"][0]
    assert row.get("password") == "hunter2", (
        "RunOut stripped a domain-specific field. If you're tightening "
        "the read surface, update the RunOut docstring AND the round-4 "
        "review notes so the deliberate trade-off doesn't quietly flip."
    )


# ---------------------------------------------------------------------------
# Phase 3a OTLP — source pin for the audit-block synthesis (regression
# protection without spinning up the route). The exact hash recipe is
# already covered in tests/api/test_otlp.py; this is a belt-and-braces
# pin against future refactors that drop the audit field entirely.


def test_otlp_synthesizes_audit_block():
    """OTLP runs MUST emit a one-event hash-chained audit block. Pin
    the source so a refactor that strips the field surfaces here
    instead of in production silence."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "apps" / "api" / "evalguard_api" / "otlp.py"
    ).read_text()
    # The synthesis function name + the field it injects.
    assert "_synthesize_audit_block" in src
    assert 'payload["audit"] = audit_block' in src


def test_otlp_uses_deterministic_run_id():
    """OTLP run_ids derive from sha256(trace_id) so collector retries
    are idempotent. Pin the source so a refactor back to ``token_hex``
    (which would silently re-enable double-ingest) regresses here."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "apps" / "api" / "evalguard_api" / "otlp.py"
    ).read_text()
    assert "_deterministic_ids" in src
    # The function MUST hash the trace_id when present.
    fn_start = src.index("def _deterministic_ids")
    fn_end = src.index("\ndef ", fn_start + 1)
    fn = src[fn_start:fn_end]
    assert re.search(r"hashlib\.sha256.*trace_id_hex", fn, re.DOTALL), \
        "_deterministic_ids must hash trace_id, not fall back to random"
