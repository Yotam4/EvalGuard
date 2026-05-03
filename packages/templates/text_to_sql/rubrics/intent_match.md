# SQL intent-match rubric (1–5)

Compare the candidate SQL to the expected SQL on **semantic intent**
(does it compute the same result?), not surface form (whitespace,
table aliases, column order).

- **5** — candidate would return the same result set as the expected
  query for any reasonable instance of the schema.
- **4** — same result set under typical data, with at most a cosmetic
  difference (alias choice, redundant ``DISTINCT``).
- **3** — substantively right but off by one knob: incorrect
  ``ORDER BY``, missing ``LIMIT``, a wrong inclusive/exclusive bound.
- **2** — wrong ``GROUP BY`` / ``JOIN`` / aggregation, or returns the
  wrong cardinality.
- **1** — does not address the question, references columns not in
  the schema, or returns garbage.

Penalize the **silent wrong-result** failures (off-by-one bounds,
inclusive vs exclusive ranges) more heavily than verbose-but-correct
queries.
