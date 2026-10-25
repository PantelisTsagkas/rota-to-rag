from botocore.stub import Stubber
import boto3

from mitos_rag.feedback import ANSWER_ROW_TTL_S, DynamoFeedback, MemoryFeedback, parse_row, summarise

TABLE = "mitos-rag-limits"
NOW = 1_792_000_000  # 2026-10-14 UTC, a fixed clock
ANSWER_ID = "a" * 32


def dynamodb() -> tuple[object, Stubber]:
    client = boto3.client("dynamodb", region_name="eu-central-1", aws_access_key_id="x", aws_secret_access_key="x")
    return client, Stubber(client)


def test_an_answer_is_recorded_without_the_question_and_expires() -> None:
    client, stub = dynamodb()
    stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {
                "pk": {"S": f"answer#{ANSWER_ID}"},
                "status": {"S": "answered"},
                "procedure_ids": {"L": [{"S": "144400"}]},
                "cost_usd": {"N": "0.0480"},
                "day": {"S": "2026-10-14"},
                "expires_at": {"N": str(NOW + ANSWER_ROW_TTL_S)},
            },
        },
    )
    with stub:
        DynamoFeedback(client, TABLE, clock=lambda: NOW).record(ANSWER_ID, "answered", ["144400"], 0.048)
    stub.assert_no_pending_responses()


def test_a_vote_and_a_reason_are_conditional_writes() -> None:
    client, stub = dynamodb()
    key = {"pk": {"S": f"answer#{ANSWER_ID}"}}
    stub.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": key,
            "UpdateExpression": "SET helpful = :helpful",
            "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(helpful)",
            "ExpressionAttributeValues": {":helpful": {"BOOL": False}},
        },
    )
    stub.add_client_error("update_item", "ConditionalCheckFailedException")  # a second vote
    stub.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": key,
            "UpdateExpression": "SET reason = :reason",
            "ConditionExpression": "helpful = :no AND attribute_not_exists(reason)",
            "ExpressionAttributeValues": {":reason": {"S": "missing_info"}, ":no": {"BOOL": False}},
        },
    )
    store = DynamoFeedback(client, TABLE, clock=lambda: NOW)
    with stub:
        assert store.vote(ANSWER_ID, False) is True
        assert store.vote(ANSWER_ID, True) is False
        assert store.vote(ANSWER_ID, False, "missing_info") is True
    stub.assert_no_pending_responses()


def test_memory_store_keeps_the_same_rules() -> None:
    store = MemoryFeedback(clock=lambda: NOW)
    assert store.vote(ANSWER_ID, True) is False  # unknown answer
    store.record(ANSWER_ID, "answered", ["144400"], 0.048)
    assert store.vote(ANSWER_ID, False, "other") is False  # a reason needs a "no" first
    assert store.vote(ANSWER_ID, True) is True
    assert store.vote(ANSWER_ID, False, "other") is False  # and only after "no"
    assert store.rows[ANSWER_ID]["day"] == "2026-10-14"


def test_the_summary_counts_votes_reasons_and_unhelpful_procedures() -> None:
    items = [
        {"pk": {"S": "answer#1"}, "status": {"S": "answered"}, "procedure_ids": {"L": [{"S": "144400"}]},
         "day": {"S": "2026-10-22"}, "helpful": {"BOOL": True}},
        {"pk": {"S": "answer#2"}, "status": {"S": "answered"}, "procedure_ids": {"L": [{"S": "906805"}]},
         "day": {"S": "2026-10-23"}, "helpful": {"BOOL": False}, "reason": {"S": "missing_info"}},
        {"pk": {"S": "answer#3"}, "status": {"S": "not_found"}, "procedure_ids": {"L": []}, "day": {"S": "2026-10-23"}},
    ]
    text = summarise([parse_row(item) for item in items], {"906805": "Νομική συνδρομή"})
    assert "Answers recorded: 3 (2026-10-22 to 2026-10-23)" in text
    assert "Voted: 2 (67%)" in text
    assert "answered: 1 of 2 helpful" in text
    assert "1  Λείπουν πληροφορίες" in text
    assert "1  906805  Νομική συνδρομή" in text
    assert summarise([], {}) == "No answers recorded yet."
