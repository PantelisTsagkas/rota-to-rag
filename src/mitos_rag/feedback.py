"""Answer feedback: "did this help?" with an optional fixed reason (decision 014).

When the API answers (answered or not_found, not ambiguous), it records the answer under a
random `answer_id`: status, cited procedure ids, cost and UTC date. Never the question, never
the visitor's address. A vote updates that record, once, so only real answers get a vote and a
page reload cannot vote twice. There is no free text: people would type ΑΦΜ and names into it,
and the page promises that questions are not stored.

On Lambda the records live in the limits table (`dynamo.py`) as `answer#<id>` rows, deleted by
DynamoDB TTL after 90 days. Locally they live in memory. `mitos-feedback` summarises the table.
"""

import argparse
import json
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from botocore.exceptions import ClientError

from mitos_rag.dynamo import conditional_failed

REASONS = {
    "wrong_procedure": "Βρήκε λάθος διαδικασία",
    "missing_info": "Λείπουν πληροφορίες",
    "wrong_numbers": "Λάθος ποσό ή προθεσμία",
    "hard_to_read": "Δύσκολο να το καταλάβω",
    "other": "Κάτι άλλο",
}
ANSWER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")  # uuid4().hex
ANSWER_ROW_TTL_S = 90 * 24 * 3600
ROW_PREFIX = "answer#"


class FeedbackStore(Protocol):
    def record(self, answer_id: str, status: str, procedure_ids: list[str], cost_usd: float) -> None: ...

    def vote(self, answer_id: str, helpful: bool, reason: str | None = None) -> bool:
        """False when the answer is unknown or this vote was already given. With `reason`,
        the answer must already have a "no" and no reason yet."""
        ...


def today(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).date().isoformat()


class MemoryFeedback:
    """For `mitos-api` on a laptop: one process, so a dict is enough. Lost on restart."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.rows: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, answer_id: str, status: str, procedure_ids: list[str], cost_usd: float) -> None:
        with self._lock:
            self.rows[answer_id] = {
                "status": status, "procedure_ids": list(procedure_ids), "cost_usd": cost_usd, "day": today(self.clock),
            }

    def vote(self, answer_id: str, helpful: bool, reason: str | None = None) -> bool:
        with self._lock:
            row = self.rows.get(answer_id)
            if row is None:
                return False
            if reason is None:
                if "helpful" in row:
                    return False
                row["helpful"] = helpful
                return True
            if row.get("helpful") is not False or "reason" in row:
                return False
            row["reason"] = reason
            return True


class DynamoFeedback:
    """Same contract as `MemoryFeedback`, on the limits table. Each call is one conditional write."""

    def __init__(self, client: Any, table: str, clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.table = table
        self.clock = clock

    def record(self, answer_id: str, status: str, procedure_ids: list[str], cost_usd: float) -> None:
        now = int(self.clock())
        self.client.put_item(
            TableName=self.table,
            Item={
                "pk": {"S": ROW_PREFIX + answer_id},
                "status": {"S": status},
                "procedure_ids": {"L": [{"S": pid} for pid in procedure_ids]},
                "cost_usd": {"N": f"{cost_usd:.4f}"},
                "day": {"S": today(self.clock)},
                "expires_at": {"N": str(now + ANSWER_ROW_TTL_S)},
            },
        )

    def vote(self, answer_id: str, helpful: bool, reason: str | None = None) -> bool:
        if reason is None:
            update = {
                "UpdateExpression": "SET helpful = :helpful",
                "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(helpful)",
                "ExpressionAttributeValues": {":helpful": {"BOOL": helpful}},
            }
        else:
            update = {
                "UpdateExpression": "SET reason = :reason",
                "ConditionExpression": "helpful = :no AND attribute_not_exists(reason)",
                "ExpressionAttributeValues": {":reason": {"S": reason}, ":no": {"BOOL": False}},
            }
        try:
            self.client.update_item(TableName=self.table, Key={"pk": {"S": ROW_PREFIX + answer_id}}, **update)
        except ClientError as failure:
            if conditional_failed(failure):
                return False
            raise
        return True


# ---------- mitos-feedback: a summary of the table ----------


def parse_row(item: dict[str, Any]) -> dict[str, Any]:
    """A DynamoDB item in plain Python values."""
    return {
        "status": item["status"]["S"],
        "procedure_ids": [value["S"] for value in item.get("procedure_ids", {}).get("L", [])],
        "day": item.get("day", {}).get("S", ""),
        "helpful": item["helpful"]["BOOL"] if "helpful" in item else None,
        "reason": item["reason"]["S"] if "reason" in item else None,
    }


def summarise(rows: list[dict[str, Any]], titles: dict[str, str]) -> str:
    if not rows:
        return "No answers recorded yet."
    voted = [row for row in rows if row["helpful"] is not None]
    days = sorted(row["day"] for row in rows if row["day"])
    lines = [
        f"Answers recorded: {len(rows)} ({days[0]} to {days[-1]})" if days else f"Answers recorded: {len(rows)}",
        f"Voted: {len(voted)} ({len(voted) / len(rows):.0%})",
    ]
    for status in sorted({row["status"] for row in voted}):
        group = [row for row in voted if row["status"] == status]
        helpful = sum(1 for row in group if row["helpful"])
        lines.append(f"  {status}: {helpful} of {len(group)} helpful")
    reasons = Counter(row["reason"] for row in voted if row["reason"])
    if reasons:
        lines.append("Reasons for 'no':")
        lines += [f"  {count}  {REASONS.get(key, key)}" for key, count in reasons.most_common()]
    unhelpful = Counter(pid for row in voted if row["helpful"] is False for pid in row["procedure_ids"])
    if unhelpful:
        lines.append("Procedures in 'no' answers (eval candidates):")
        lines += [f"  {count}  {pid}  {titles.get(pid, '')}" for pid, count in unhelpful.most_common(10)]
    return "\n".join(lines)


def scan_rows(client: Any, table: str) -> Iterable[dict[str, Any]]:
    paginator = client.get_paginator("scan")
    pages = paginator.paginate(
        TableName=table,
        FilterExpression="begins_with(pk, :prefix)",
        ExpressionAttributeValues={":prefix": {"S": ROW_PREFIX}},
    )
    for page in pages:
        yield from (parse_row(item) for item in page["Items"])


def main() -> None:
    import boto3

    from mitos_rag.embed import REGION

    parser = argparse.ArgumentParser(description="Summarise answer feedback from the API's DynamoDB table.")
    parser.add_argument("--table", default="mitos-rag-limits")
    parser.add_argument("--facts", type=Path, default=Path("data/facts/facts.jsonl"), help="for procedure titles")
    args = parser.parse_args()

    titles: dict[str, str] = {}
    if args.facts.exists():
        for line in args.facts.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            titles[row["procedure_id"]] = row["title"]
    client = boto3.client("dynamodb", region_name=REGION)
    print(summarise(list(scan_rows(client, args.table)), titles))


if __name__ == "__main__":
    main()
