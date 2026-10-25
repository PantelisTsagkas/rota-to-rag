"""Limits and the "one question at a time" lease in DynamoDB, for the API on Lambda (013).

Lambda instances share no memory, so the in-process `Limits` and lock in `api.py` cannot see
each other there. One on-demand table holds three kinds of row, each with an `expires_at` epoch
second that DynamoDB TTL uses to delete it eventually:

- `day#<UTC date>`: the questions asked today. One conditional update adds 1 only while the
  count is below the cap, so two requests can never both slip past it.
- `ip#<hash>#<minute>`: questions from one client in one clock minute (a fixed window, not the
  sliding one in `Limits`). The IP is stored as a SHA-256 prefix, never in the clear.
- `busy`: the lease. Taken with a conditional put unless it exists and has not expired, and
  deleted by its owner when the answer ends. A Lambda that dies mid-answer frees it by expiry:
  TTL deletion can lag by up to 48 hours, so the condition compares `expires_at` itself.

The table name comes from `MITOS_LIMITS_TABLE`; `api.main` uses these classes only when it is set.
"""

import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError

# A day's row outlives the day, so a late TTL never resets a count early.
DAY_ROW_TTL_S = 3 * 24 * 3600
IP_ROW_TTL_S = 120
# How often a waiting question tries the lease again: one small conditional write a second.
LEASE_POLL_S = 1.0


def conditional_failed(failure: ClientError) -> bool:
    return failure.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def ip_key(ip: str) -> str:
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:16]


class DynamoLimits:
    """Same contract as `api.Limits.check`: the error key if refused, else None (and counted)."""

    def __init__(
        self,
        client: Any,
        table: str,
        per_ip_per_minute: int,
        daily: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.table = table
        self.per_ip_per_minute = per_ip_per_minute
        self.daily = daily
        self.clock = clock

    def _count_below(self, pk: str, limit: int, expires_at: int) -> bool:
        """Add 1 to the row's count if it is below `limit`. False when it is not."""
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"pk": {"S": pk}},
                UpdateExpression="ADD n :one SET expires_at = :expires",
                ConditionExpression="attribute_not_exists(n) OR n < :limit",
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":limit": {"N": str(limit)},
                    ":expires": {"N": str(expires_at)},
                },
            )
        except ClientError as failure:
            if conditional_failed(failure):
                return False
            raise
        return True

    def check(self, ip: str) -> str | None:
        now = int(self.clock())
        minute = now // 60
        if not self._count_below(f"ip#{ip_key(ip)}#{minute}", self.per_ip_per_minute, (minute + 1) * 60 + IP_ROW_TTL_S):
            return "rate"
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        if not self._count_below(f"day#{day}", self.daily, now + DAY_ROW_TTL_S):
            return "daily"
        return None


class DynamoLease:
    """Same contract as the `threading.Lock` it replaces: `acquire(timeout=...)` and `release()`.

    A second question waits up to `timeout` seconds, trying again every `LEASE_POLL_S`, like the
    local lock does. Until 2026-10-22 it never waited, to keep Lambdas short, but a phone asking
    while a laptop's answer was 5 s from done got "busy" at once. The wait holds a second Lambda
    open (under $0.001 for 20 s at 1 GB); CloudFront allows the origin 60 s before the first byte.
    """

    def __init__(
        self,
        client: Any,
        table: str,
        lease_s: int,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.table = table
        self.lease_s = lease_s
        self.clock = clock
        self.sleep = sleep
        self.owner: str | None = None

    def acquire(self, timeout: float = 0.0) -> bool:
        deadline = self.clock() + timeout
        while not self._try_take():
            if self.clock() + LEASE_POLL_S > deadline:
                return False
            self.sleep(LEASE_POLL_S)
        return True

    def _try_take(self) -> bool:
        now = int(self.clock())
        owner = uuid.uuid4().hex
        try:
            self.client.put_item(
                TableName=self.table,
                Item={"pk": {"S": "busy"}, "owner": {"S": owner}, "expires_at": {"N": str(now + self.lease_s)}},
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
                ExpressionAttributeValues={":now": {"N": str(now)}},
            )
        except ClientError as failure:
            if conditional_failed(failure):
                return False
            raise
        self.owner = owner
        return True

    def release(self) -> None:
        owner, self.owner = self.owner, None
        if owner is None:
            return
        try:
            self.client.delete_item(
                TableName=self.table,
                Key={"pk": {"S": "busy"}},
                ConditionExpression="#owner = :owner",
                ExpressionAttributeNames={"#owner": "owner"},
                ExpressionAttributeValues={":owner": {"S": owner}},
            )
        except ClientError as failure:
            # The lease expired and someone else holds it now: theirs to release.
            if not conditional_failed(failure):
                raise
