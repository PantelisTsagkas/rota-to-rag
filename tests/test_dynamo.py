import boto3
import pytest
from botocore.stub import ANY, Stubber

from mitos_rag.dynamo import DynamoLease, DynamoLimits, ip_key

TABLE = "mitos-rag-limits"
NOW = 1_792_000_000  # 2026-10-14 17:46 UTC, a fixed clock


def dynamodb() -> tuple[object, Stubber]:
    client = boto3.client("dynamodb", region_name="eu-central-1", aws_access_key_id="x", aws_secret_access_key="x")
    return client, Stubber(client)


def count_call(pk: str, limit: int) -> dict[str, object]:
    return {
        "TableName": TABLE,
        "Key": {"pk": {"S": pk}},
        "UpdateExpression": "ADD n :one SET expires_at = :expires",
        "ConditionExpression": "attribute_not_exists(n) OR n < :limit",
        "ExpressionAttributeValues": {":one": {"N": "1"}, ":limit": {"N": str(limit)}, ":expires": ANY},
    }


def test_a_question_counts_against_the_ip_minute_then_the_day() -> None:
    client, stub = dynamodb()
    limits = DynamoLimits(client, TABLE, per_ip_per_minute=3, daily=15, clock=lambda: NOW)
    stub.add_response("update_item", {}, count_call(f"ip#{ip_key('1.2.3.4')}#{NOW // 60}", 3))
    stub.add_response("update_item", {}, count_call("day#2026-10-14", 15))
    with stub:
        assert limits.check("1.2.3.4") is None
    stub.assert_no_pending_responses()


def test_a_failed_condition_is_the_matching_refusal() -> None:
    client, stub = dynamodb()
    limits = DynamoLimits(client, TABLE, per_ip_per_minute=3, daily=15, clock=lambda: NOW)
    stub.add_client_error("update_item", "ConditionalCheckFailedException")
    stub.add_response("update_item", {})
    stub.add_client_error("update_item", "ConditionalCheckFailedException")
    with stub:
        assert limits.check("1.2.3.4") == "rate"
        assert limits.check("5.6.7.8") == "daily"


def test_other_dynamodb_errors_are_not_mistaken_for_a_limit() -> None:
    client, stub = dynamodb()
    limits = DynamoLimits(client, TABLE, per_ip_per_minute=3, daily=15, clock=lambda: NOW)
    stub.add_client_error("update_item", "ProvisionedThroughputExceededException")
    with stub, pytest.raises(Exception, match="ProvisionedThroughputExceeded"):
        limits.check("1.2.3.4")


def test_the_ip_is_never_stored_in_the_clear() -> None:
    assert "1.2.3.4" not in ip_key("1.2.3.4") and len(ip_key("1.2.3.4")) == 16


def test_the_lease_is_taken_once_and_released_by_its_owner() -> None:
    client, stub = dynamodb()
    lease = DynamoLease(client, TABLE, lease_s=150, clock=lambda: NOW)
    stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {"pk": {"S": "busy"}, "owner": ANY, "expires_at": {"N": str(NOW + 150)}},
            "ConditionExpression": "attribute_not_exists(pk) OR expires_at < :now",
            "ExpressionAttributeValues": {":now": {"N": str(NOW)}},
        },
    )
    stub.add_client_error("put_item", "ConditionalCheckFailedException")  # someone else's question
    stub.add_response(
        "delete_item",
        {},
        {
            "TableName": TABLE,
            "Key": {"pk": {"S": "busy"}},
            "ConditionExpression": "#owner = :owner",
            "ExpressionAttributeNames": {"#owner": "owner"},
            "ExpressionAttributeValues": {":owner": ANY},
        },
    )
    with stub:
        assert lease.acquire() is True
        owner = lease.owner
        other = DynamoLease(client, TABLE, lease_s=150, clock=lambda: NOW)
        assert other.acquire() is False  # no timeout: one try
        lease.release()
    stub.assert_no_pending_responses()
    assert owner is not None and lease.owner is None


class FakeTime:
    def __init__(self) -> None:
        self.now = float(NOW)
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_a_second_question_waits_for_the_lease_then_takes_it() -> None:
    client, stub = dynamodb()
    fake = FakeTime()
    lease = DynamoLease(client, TABLE, lease_s=150, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        stub.add_client_error("put_item", "ConditionalCheckFailedException")  # still answering
    stub.add_response("put_item", {})  # the first answer ended
    with stub:
        assert lease.acquire(timeout=20.0) is True
    stub.assert_no_pending_responses()
    assert fake.sleeps == [1.0, 1.0, 1.0]


def test_the_wait_gives_up_at_the_timeout() -> None:
    client, stub = dynamodb()
    fake = FakeTime()
    lease = DynamoLease(client, TABLE, lease_s=150, clock=fake.clock, sleep=fake.sleep)
    for _ in range(21):
        stub.add_client_error("put_item", "ConditionalCheckFailedException")
    with stub:
        assert lease.acquire(timeout=20.0) is False
    stub.assert_no_pending_responses()
    assert sum(fake.sleeps) == 20.0  # 21 tries over 20 s, then "busy"


def test_releasing_an_expired_lease_someone_else_took_is_quiet() -> None:
    client, stub = dynamodb()
    lease = DynamoLease(client, TABLE, lease_s=150, clock=lambda: NOW)
    stub.add_response("put_item", {})
    stub.add_client_error("delete_item", "ConditionalCheckFailedException")
    with stub:
        assert lease.acquire()
        lease.release()
        lease.release()  # nothing held: no call
