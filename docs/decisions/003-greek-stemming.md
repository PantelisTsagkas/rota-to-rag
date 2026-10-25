# 003: Greek stemming in BM25

Status: measured 2026-10-14 on the dev split. **Not adopted.** Section granularity with accent folding (002) stays the keyword baseline.

## Why we tried it

In 002, 4 of the 9 misses were inflection: the question says ταυτοτητα and the document says ταυτότητας. A stemmer maps both forms to one token. It is cheap, needs no service, and would strengthen the keyword side of a later hybrid search.

## Setup

- Stemmer: Snowball Greek (Ntais 2006), through PyStemmer 3.1.0. It is applied after accent folding (`text.py`, mode `stemmed`). The pure-Python `snowballstemmer` package gives identical stems on the words checked.
- Everything else is as in 002: BM25 k1 = 1.5 and b = 0.75, a procedure scores by its best section, no stopword list.
- Command: `uv run mitos-eval --split dev`. The `stemmed` rows sit next to the 002 rows.

## Results (dev, all procedures)

| config | hit@1 | hit@5 | hit@10 | MRR | recall@5 | recall@10 |
|---|---|---|---|---|---|---|
| section/normalised (002) | 0.39 | 0.50 | 0.56 | 0.43 | 0.47 | 0.50 |
| section/stemmed | 0.22 | 0.33 | 0.50 | 0.30 | 0.32 | 0.44 |
| doc/normalised (002) | 0.17 | 0.28 | 0.39 | 0.22 | 0.24 | 0.35 |
| doc/stemmed | 0.17 | 0.28 | 0.44 | 0.23 | 0.24 | 0.32 |

At section level, stemming loses three hit@5 items and does not gain one. Doc granularity barely moves, and doc is not the chosen granularity.

Rank of the first relevant procedure, section level:

| item | question | normalised | stemmed |
|---|---|---|---|
| q08 | εχασα την ταυτοτητα μου τι κανω | 1,919 | 92 |
| q22 | γεννηθηκε το μωρο μας τι πρεπει να κανουμε | 883 | 38 |
| q25 | ταυτοτητα | 8 | **1** |
| q07 | πως βγαζω καρτα ανεργιας | 5 | 2 |
| q10 | θελω πιστοποιητικο γεννησης για τη δουλεια | **1** | 7 |
| q16 | σε ποσες μερες βγαινει το ποινικο μητρωο | **1** | 8 |
| q20 | ποσο κοστιζει η πρωτη ταυτοτητα | **1** | 15 |
| q27 | επιδομα | **1** | 6 |

Seven other items got slightly worse but were misses either way. q01, q02, q04 and q11 did not change.

## Why it hurts

A per-term BM25 breakdown for the items that dropped shows everyday question words merging with frequent administrative words. Each figure below is that stem's BM25 points in the top-ranked wrong section:

- **q16**: ποσες→ποσ matches ποσού and ποσών (5.5 points). ποινικο→ποιν matches ποινή and ποινής (7.4 points). Together they rank "Εξαγορά Πειθαρχικής Ποινής" first.
- **q20**: ποσο→ποσ matches ποσού (6.2 points) and πρωτη→πρωτ matches πρώτης. The wrong section is the cost section of a municipal auction.
- **q10**: θελω→θελ matches θελήσουν (7.7 points). Without stemming, θελω occurs in a single doc.
- **q27**: επιδομα→επιδομ also matches επιδόματος and επιδομάτων, so procedures that mention benefits in the plural outrank "Επίδομα Στέγασης". For an ambiguous question this is not clearly wrong.

Two smaller effects show up too. The stemmer cuts function words to one or two letters (για→γ, του→τ, μου→μ). And κοστιζει→κοστ now matches the Κόστος heading inside every cost section (2.1 points in q20).

## The length guard (not adopted)

A scratch spike left words of 3 or fewer letters unstemmed, then 4 or fewer. These rules were chosen after looking at the dev failures, so they are fitted to dev.

| guard | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|
| words > 3 letters only | 0.22 | 0.33 | 0.50 | 0.30 |
| words > 4 letters only | 0.22 | 0.39 | 0.56 | 0.31 |

Both are still below the 002 baseline. Function words are not the main cause; merging is. No further variants were tried (stopword lists, stemming only the query, fusing stemmed and normalised rankings). With 18 items, each extra tweak would fit the noise.

## Conclusion

Folding inflection works: q08, q22 and q25 prove that. But rule-based stemming also merges words that mean different things (ποσό and πόσο, ποινή and ποινικό), and on this dev set the merging costs more than it gains. Both miss causes from 002, vocabulary and inflection, point at dense retrieval. An embedding model sees ταυτότητα and ταυτότητας as near neighbours without merging ποσό into πόσο.

The `stemmed` token mode and the PyStemmer dependency stay in the code so these numbers can be reproduced. The retrieval pipeline does not use them.
