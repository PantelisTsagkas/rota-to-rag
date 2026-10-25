# 014: Answer feedback ("Σας βοήθησε;")

Date: 2026-10-22. Status: built, waiting for deploy.

## Why

The eval set was written by Claude, not by real users (`eval/README.md`). Votes on real answers show where the assistant fails people, and the procedures behind "no" votes are the best candidates for new eval items.

## What was chosen

He saw a local demo (saved answers, no AWS) and chose:

- **Ναι / Όχι at the end of the answer card**, on a neutral panel with soft tinted buttons: pale green Ναι (#EAF6EE, text #137A3E, 4.9:1) and pale red Όχι (#FCEDEB, text #B42318, 5.8:1). The first version had outlined buttons, which looked too intense. Each button keeps its icon and word, so colour is never the only signal.
- **After Όχι, optional fixed reasons**: Βρήκε λάθος διαδικασία, Λείπουν πληροφορίες, Λάθος ποσό ή προθεσμία, Δύσκολο να το καταλάβω, Κάτι άλλο. The "no" is counted at once; the reason is a second, optional tap.
- **No free text.** People would type ΑΦΜ, names and addresses, and the page promises that questions are not stored.
- No panel on "ambiguous" replies: the visitor picks an option instead.

## How it works

- Each answered or not_found reply gets a random `answer_id` (uuid4 hex) in the payload. The API records `answer#<id>`: status, cited procedure ids, cost and UTC date, with `expires_at` 90 days out for DynamoDB TTL. Never the question, never the IP.
- `POST /ask/feedback {answer_id, helpful, reason?}`. It sits under `/ask` because CloudFront sends only `/ask*` to Lambda (013). A vote is one conditional write: it needs an existing answer row and no earlier vote; a reason needs a "no" and no earlier reason. So only real answers get votes, each once, without a separate rate limit (answers themselves are limited to 15 a day). Bad ids or unknown reasons get 400, a repeat gets 409.
- If recording fails, the answer still goes out, with `answer_id: null` and no panel.
- On Lambda the rows live in the existing limits table (`DynamoFeedback`); the role already had `PutItem` and `UpdateItem` on it, so no Terraform change. Locally they live in memory (`MemoryFeedback`).
- `uv run mitos-feedback` scans the table and prints answers recorded, the share voted, helpful per status, the reasons, and the procedures in "no" answers with their titles.

## Cost

Two small DynamoDB writes per voted answer, one per unvoted answer: well under $0.01 a month at 15 answers a day. The summary scan reads the whole table, which holds at most a few thousand small rows.

## Checked

Unit tests for both stores (botocore `Stubber` for DynamoDB) and the API rules. An end-to-end run of the real `create_app` and `web/` with the test suite's fake assistant: the browser voted Όχι with "Λάθος ποσό ή προθεσμία", and the stored row held only status, procedure ids, cost, day, helpful and reason.

Against the real limits table (one throwaway row, deleted after): record, a "no", a refused second vote, a reason, a refused second reason and a refused unknown id all behaved as above, and `mitos-feedback` printed the row. The Lambda policy's `Limits` statement has no key condition, so `answer#` rows are allowed.
