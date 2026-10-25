# 010: A text-grounded second tier for the answer checker

Status: adopted, 2026-10-22. **Adopted: the text-grounded tier.** Measured on dev by re-scoring the cached 009 answers (0 fresh calls, cost under $0.001 for S3 Vectors queries). The rules below were committed before the answer test split was run.

## The problem

The checker (`validate.py`, 006) took every euro amount and numeral duration in an answer and required it to be in the cited procedure's facts row: the cost table, deadline, estimated duration or validity. A number not there got the warning "Προσοχή: δεν μπόρεσα να επιβεβαιώσω...". On the 009 dev run, Sonnet 4.6 showed that warning on 6 of 20 answers, and most of the flagged numbers were correct, taken from the procedure's own text:

- q06, q18 (passport): "διάρκειας 13 μηνών". The 13-month emergency passport is in a cost row's description, not in a duration field.
- q02 (tax clearance): debts "άνω των 30 €". A requirement, not a fee.
- q15 (driving licence renewal): the 30 € fee for a renewal under a medical time limit, written only in the procedure's notes.

A warning that appears on correct answers teaches users to ignore it.

## The options

1. **Keep the strict check.** Every number must be in the facts row. Safe, but noisy: 6 warnings on 20 answers, most of them false.
2. **Add a second tier (adopted).** The facts row stays the strong check. A number not there, but written in the text of a procedure the answer cites, is text-grounded: listed apart, with no warning. The warning stays for numbers in neither.

## The rules

- Text matching compares parsed values (`euro_amounts`, `text_durations` run over the rendered doc), never substrings, so a law number like "4174/2013" never grounds a "4174 €".
- Only the cited procedures' text counts, read whole from `data/docs/<id>.md` with the attribution stripped. Not joined chunks: a hard-wrapped line can split "13 μηνών" across two chunks.
- **Typed facts stay strict.** When the model says a cost, duration, validity or "online" value comes from `<facts>`, it must match the facts row. q11's typed cost "30" for 664541 still fails, because that fee is in the text only.
- **Totals stay strict.** An amount on a line that mentions a total ("Σύνολο", "συνολικά", matched accent-folded) only passes if the facts row has it. A sum is never in the registry, so a match in the text would be a coincidence (Haiku q08: "Σύνολο: περίπου 19,5 €").
- The number reader now accepts the legal style "τριάντα (30) ευρώ" and "έξι (6) μήνες", in answers and in docs. Without it, q02's 30 € was not found in its own procedure.
- Durations written in words ("δύο μήνες", "δίμηνο") are still not read, so q11's "2 μήνες πριν τη λήξη" stays flagged although it is correct.

## Results (dev, 20 items, cached 009 answers)

| | Sonnet + rerank, before | after | Haiku + rerank, before | after |
|---|---|---|---|---|
| answers with the warning | 6 | **2** | 3 | **2** |
| unsupported claims | 8 | 3 | 5 | 3 |
| text-grounded numbers | n/a | 5 | n/a | 2 |
| status accuracy as scored | 1.00 | 1.00 | 0.85 | 0.85 |
| fact correct | 5/5 | 5/5 | 3/5 | 3/5 |

Status and fact scores do not change: the checker only decides the warning and the unsupported count.

The warnings that remain:

- q04 (both models): the typed fact "online: yes" for 895302 (ακατάσχετος λογαριασμός), where Mitos has `can_apply_online` null, that is unknown. The warning is right: the registry does not say.
- q11 (Sonnet): the typed cost "30" (in the text only) and "2 μήνες" (written "δύο μήνες" / "δίμηνο" in the doc). Correct content, flagged by design.
- q08 (Haiku): the totals 19,5 € and 14,5 €. Flagged by design.

## A new score: every expected procedure cited

`all_expected_cited` is reported for answered items with more than one expected procedure (q21, q22 on dev). It closes the gap 009 found, where q22 counted as correct while missing the name declaration and AMKA. Ambiguous items are left out: their expected procedures are the possible readings, and a clarifying question need not list every one. Dev: 0.50 for both models (q21 yes, q22 no).

## Known weakness

Full-text matching can ground a wrong number by coincidence. The passport doc (144400) contains "5 έτη" and "10 έτη" in its criminal-ban clause, so a wrong passport validity of 5 or 10 years written in prose would pass the second tier. The risk is limited because only cited procedures count and typed facts stay strict, and it is the tradeoff accepted for a warning users can trust. `text_grounded` is reported per item, so these cases stay visible in every eval.

## On the test split

Run once on 2026-10-22, after this decision was committed (results in 009): 0 unsupported claims and 0 warnings on 10 answers, 15 text-grounded numbers. 14 of them are q13's child-benefit income brackets and amounts, each checked by hand against 375051's text. The strict checker would have warned on that answer.
