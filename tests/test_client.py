import httpx
import pytest

from mitos_rag.client import MitosApiError, MitosClient


def make_client(handler: httpx.MockTransport) -> tuple[MitosClient, list[float]]:
    sleeps: list[float] = []
    client = MitosClient(transport=handler, backoff_seconds=0.5, max_retries=2, sleep=sleeps.append)
    return client, sleeps


def list_page(page: int, total_pages: int) -> dict[str, object]:
    return {
        "success": True,
        "total": total_pages * 2,
        "current_page": page,
        "next_page": page + 1 if page < total_pages else None,
        "limit": 2,
        "data": [
            {"id": f"{page}01", "title": {"el": f"Διαδικασία {page}α"}, "ns": "ΔΔ", "last_updated": "2026-01-01"},
            {"id": f"{page}02", "title": {"el": f"Διαδικασία {page}β", "en": "B"}, "ns": "ΔΔ", "last_updated": "2026-01-01"},
        ],
    }


def test_iter_service_summaries_follows_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/services"
        return httpx.Response(200, json=list_page(int(request.url.params["page"]), total_pages=3))

    client, _ = make_client(httpx.MockTransport(handler))
    ids = [s.id for s in client.iter_service_summaries(page_size=2)]
    assert ids == ["101", "102", "201", "202", "301", "302"]


def test_iter_stops_when_next_page_does_not_advance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = list_page(1, total_pages=5)
        payload["next_page"] = 1  # a buggy API pointing back to itself
        return httpx.Response(200, json=payload)

    client, _ = make_client(httpx.MockTransport(handler))
    assert len(list(client.iter_service_summaries(page_size=2))) == 2


def test_summary_handles_missing_english_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=list_page(1, total_pages=1))

    client, _ = make_client(httpx.MockTransport(handler))
    first, second = client.list_services_page(page=1, limit=2).items
    assert first.title_en is None
    assert second.title_en == "B"


def test_get_service_returns_data_object() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/services/439993"
        return httpx.Response(200, json={"success": True, "data": {"id": "439993", "metadata": {}}})

    client, _ = make_client(httpx.MockTransport(handler))
    assert client.get_service("439993")["id"] == "439993"


def test_retries_on_503_with_exponential_backoff() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"success": True, "data": {"id": "1"}})

    client, sleeps = make_client(httpx.MockTransport(handler))
    assert client.get_service("1") == {"id": "1"}
    assert sleeps == [0.5, 1.0]


def test_gives_up_after_max_retries() -> None:
    client, sleeps = make_client(httpx.MockTransport(lambda request: httpx.Response(429)))
    with pytest.raises(MitosApiError):
        client.get_service("1")
    assert len(sleeps) == 2


def test_does_not_retry_on_404() -> None:
    client, sleeps = make_client(httpx.MockTransport(lambda request: httpx.Response(404)))
    with pytest.raises(MitosApiError):
        client.get_service("missing")
    assert sleeps == []


def test_success_false_is_an_error() -> None:
    client, _ = make_client(httpx.MockTransport(lambda request: httpx.Response(200, json={"success": False})))
    with pytest.raises(MitosApiError):
        client.get_service("1")
