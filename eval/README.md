# Evaluation set

`dev.jsonl` is for building and tuning. `test.jsonl` is **held out**: it is only used to report results, never to tune (PRD 8.1). Both are stratified across the six PRD categories.

| Category | dev | test |
|---|---|---|
| exact_lookup | 3 | 2 |
| natural_greek | 5 | 3 |
| fact_extraction | 5 | 2 |
| multi_procedure | 2 | 1 |
| ambiguous | 3 | 1 |
| out_of_scope | 2 | 1 |

## Item format

```json
{"id": "q14", "question": "ποσο κοστιζει διαβατηριο για παιδι 10 χρονων",
 "category": "fact_extraction", "expected_status": "answered",
 "expected_procedure_ids": ["144400"], "acceptable_procedure_ids": [],
 "expected_facts": [{"type": "cost", "amount": 71.0}], "note": "..."}
```

- `expected_procedure_ids`: must be retrieved (hit@k and recall@k).
- `acceptable_procedure_ids`: also correct if retrieved, not required (for example the birth allowance for "γεννηθηκε το μωρο μας").
- `expected_facts` types: `cost` (amount in euro), `estimated_duration` (ISO 8601 min and max), `validity` (ISO 8601), `online` (true or false).
- `expected_status`: `answered`, `ambiguous` or `not_found` (PRD 6.6).

## Retrieval metrics

`uv run mitos-eval --split dev` ranks procedures (deduplicated: a procedure's score is its best section) and computes, over items whose `expected_status` is not `not_found`:

- **hit@k**: any expected or acceptable procedure is in the top k.
- **MRR**: mean of 1 / rank of the first expected or acceptable procedure (0 if it is not ranked at all).
- **recall@k**: share of the `expected_procedure_ids` in the top k. Acceptable ids do not count. Items with no expected ids (q27) are skipped.

`not_found` items have nothing to retrieve, so they are left out of these numbers. Their top-1 score is reported instead, next to the answered items' scores, as evidence for a not-found threshold. Results are in `docs/decisions/`.

## Where the labels come from

Version 1 (2026-10-14) has 30 questions written by Claude in everyday Greek: no accents, everyday words such as "δίπλωμα" for "άδεια οδήγησης". Every procedure id and every expected fact was checked against the Mitos snapshot of 2026-10-14, and two labels were corrected after reading the procedure descriptions rather than the titles.

Known limitation: these are not questions from real users, so they share the blind spots of whoever wrote them. Questions from real people in Greece should be added next, and the test set should grow to keep about a third of all items.
