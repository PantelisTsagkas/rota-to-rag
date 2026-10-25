"""Small, explicit client for the public Mitos (EMDD) API.

The API is the official registry of Greek public administration procedures,
run by GRNET. Content is licensed CC BY-SA 4.0 (attribution + share-alike).

Endpoints used (verified 2026-10-12, no API key needed):
    GET {base}/v1/services?page=N&limit=M   -> paginated list of procedures
    GET {base}/v1/services/{id}             -> full procedure

List response shape:
    {"success": true, "total": 4406, "current_page": 1, "next_page": 2,
     "limit": 100, "data": [{"id", "title": {"el", "en"?}, "ns", "last_updated"}]}

Detail response shape:
    {"success": true, "data": {"id", "uuid", "url", "ns", "title",
                               "last_updated", "metadata": {...}}}
"""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.digigov.grnet.gr"
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class MitosApiError(RuntimeError):
    """Raised when the API returns an error we should not retry."""


@dataclass(frozen=True)
class ServiceSummary:
    """One row of the procedures list."""

    id: str
    title_el: str
    title_en: str | None
    ns: str
    last_updated: str

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> "ServiceSummary":
        title: dict[str, str] = item.get("title") or {}
        return cls(
            id=str(item["id"]),
            title_el=title.get("el", ""),
            title_en=title.get("en"),
            ns=item.get("ns", ""),
            last_updated=item.get("last_updated", ""),
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "id": self.id,
            "title_el": self.title_el,
            "title_en": self.title_en,
            "ns": self.ns,
            "last_updated": self.last_updated,
        }


@dataclass(frozen=True)
class ServicePage:
    items: list[ServiceSummary]
    total: int
    next_page: int | None


class MitosClient:
    """Read-only client with retries and exponential backoff."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 30.0,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "mitos-rag/0.1 (+https://pantelis-tsagkas.com)",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MitosClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def list_services_page(self, page: int, limit: int) -> ServicePage:
        payload = self._get_json("/v1/services", params={"page": page, "limit": limit})
        items = [ServiceSummary.from_api(item) for item in payload.get("data") or []]
        next_page = payload.get("next_page")
        return ServicePage(
            items=items,
            total=int(payload.get("total", 0)),
            next_page=int(next_page) if next_page else None,
        )

    def iter_service_summaries(self, page_size: int = 100) -> Iterator[ServiceSummary]:
        page: int | None = 1
        while page is not None:
            result = self.list_services_page(page=page, limit=page_size)
            if not result.items:
                return
            yield from result.items
            # Guard against an API that keeps returning the same next_page.
            page = result.next_page if result.next_page and result.next_page > page else None

    def get_service(self, service_id: str) -> dict[str, Any]:
        """Return the full procedure (the `data` object of the detail response)."""
        payload = self._get_json(f"/v1/services/{service_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MitosApiError(f"Service {service_id}: response has no data object")
        return data

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as error:
                last_error = error
            else:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error = MitosApiError(f"GET {path} returned {response.status_code}")
                elif response.is_error:
                    raise MitosApiError(f"GET {path} returned {response.status_code}")
                else:
                    payload: dict[str, Any] = response.json()
                    if payload.get("success") is not True:
                        raise MitosApiError(f"GET {path} returned success=false")
                    return payload

            if attempt < self._max_retries:
                self._sleep(self._backoff_seconds * (2**attempt))

        raise MitosApiError(f"GET {path} failed after {self._max_retries + 1} attempts") from last_error
