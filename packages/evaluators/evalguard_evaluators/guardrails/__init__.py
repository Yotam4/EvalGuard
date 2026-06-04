"""Layer-4 guardrails — the inline policy layer of the eval pyramid.

L1 (heuristics) and L2 (metrics) run cheaply on every row; L3
(``judge_offline``) runs the heavy LLM-as-judge offline / on a sampled
cadence; **L4 (``judge_online``) runs inline on every ``/invoke`` and
can block the response before the customer sees it**.

Guardrails are the only evaluators with the authority to refuse a
response in real time.  Everything else in this package observes.

Industry analogues: NeMo Guardrails (NVIDIA), Llama Guard (Meta),
guardrails.ai, OpenAI Moderation.  Operators wire their preferred
guardrail (regex PII, classifier model, content-filter API) by
registering it under the ``evalguard.evaluators`` entry-point group
with a ``guardrail.<name>`` key.  This package ships only a
``MockGuardrail`` for examples and tests; production deployments are
expected to plug in a real evaluator.
"""
