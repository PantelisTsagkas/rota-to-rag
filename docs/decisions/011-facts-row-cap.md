# 011: A size cap on facts rows in the answer prompt

Status: adopted, 2026-10-22. Phase 5 robustness, designed from the whole corpus, not from the test item that showed the problem. No paid calls.

## The problem

Every candidate's facts row goes into the prompt in full (006). Most rows are small, but a few procedures list dozens of fines. On the answer test split, the dense top 5 for "ΕΝΦΙΑ" included 242765 (administrative fines, 98 cost rows, a 57,512-character row), and the dry-run prompt grew to 45,113 tokens, four times the usual size (009). A public API cannot let one procedure quadruple the cost of a question.

## The corpus (4,406 facts rows, as `prompt_facts` serialises them)

| | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| row characters | 374 | 974 | 1,323 | 2,421 | 58,238 |
| cost rows | 0 | | 3 | 5 | 99 |
| one cost description, characters | 100 | | 686 | 1,338 | 2,921 |

36 procedures have more than 5 cost rows and 3 more than 50. 66 rows are over 2,000 characters, 12 over 4,000 and 6 over 6,000. The tail is a handful of fines registries and certification fee lists.

## The rule

- **Budget: 6,000 characters per facts row** (`FACTS_ROW_CHARS`), about 4,000 Claude tokens of Greek, so about $0.013 of Sonnet 4.6 input at most per row. Rows under it, 99.9% of the corpus, are byte-identical to before, so every cached answer stays valid.
- An over-budget row first has each cost description longer than 1,000 characters trimmed (`TRIMMED_DESCRIPTION_CHARS`, with "…"). The description p99 is about 1,300, so trimming touches only the very long legal texts.
- If it is still over budget, cost rows are dropped from the end, in source order, and the row says so in its own fields: `costs_shown`, `costs_total` and a Greek `costs_note` ("Εμφανίζονται 6 από 98 χρεώσεις. Οι υπόλοιπες είναι στην επίσημη σελίδα της διαδικασίας."). The note is in the row, not in the system prompt, so the prompt and its cache stay unchanged.
- The checker is not affected: `validate` still gets the full facts row and the full doc text, so an amount from a dropped fee row is still checked against the registry.

## Which procedures are capped

| procedure | title (short) | cost rows shown / total | row characters after |
|---|---|---|---|
| 376453 | fines, illegal trade and transport of goods | 4 / 99 | 4,964 |
| 242765 | collecting administrative fines, domestic market | 6 / 98 | 5,888 |
| 237189 | ISM ship and company certificates | 24 / 73 | 5,901 |
| 375019 | ISTA seed analysis certificates | 31 / 36 | 5,991 |
| 871900 | seed laboratory analysis certificates | 30 / 36 | 5,832 |
| 125220 | national plant variety catalogue | 4 / 5 | 5,094 |

## Effect on the eval

A check over all 30 dev and test questions, with their cached rerank orders (S3 Vectors queries only), found a capped procedure in one place: 242765 in the dense top 5 of test q03. That path (`--rerank none`) was never run for test answers. The reranked candidates of every eval question contain no capped procedure, so no cached answer and no eval result changes. With the reranker, test q03's real prompt was already 10,828 tokens.

## Not done

- Nothing tells the user when fees were left out, beyond what the model writes. The answer always links the official page.
- A fines question that does hit one of these procedures has not been tried. One paid call (about $0.05) would show whether Sonnet mentions that the list is partial.
