import json
from pathlib import Path

import httpx

from mitos_rag.client import MitosClient
from mitos_rag.download import download_all


def fake_api(
    last_updated: str, fail_ids: frozenset[str] = frozenset(), listed_ids: tuple[str, ...] = ("1", "2", "3")
) -> httpx.MockTransport:
    summaries = [
        {"id": service_id, "title": {"el": "Α"}, "ns": "ΔΔ", "last_updated": last_updated} for service_id in listed_ids
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/services":
            return httpx.Response(
                200, json={"success": True, "total": len(summaries), "next_page": None, "data": summaries}
            )
        service_id = request.url.path.rsplit("/", 1)[-1]
        if service_id in fail_ids:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"success": True, "data": {"id": service_id, "last_updated": last_updated, "metadata": {}}},
        )

    return httpx.MockTransport(handler)


def run(tmp_path: Path, transport: httpx.MockTransport, **kwargs: object):
    client = MitosClient(transport=transport, sleep=lambda _: None)
    return download_all(client, out_dir=tmp_path, sleep=lambda _: None, **kwargs)


def test_downloads_every_service_and_writes_index(tmp_path: Path) -> None:
    stats = run(tmp_path, fake_api("v1"))

    assert stats.listed == 3 and stats.downloaded == 3 and stats.failed == 0
    index_lines = (tmp_path / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in index_lines] == ["1", "2", "3"]
    assert json.loads((tmp_path / "services" / "2.json").read_text(encoding="utf-8"))["id"] == "2"


def test_second_run_skips_unchanged_and_refetches_changed(tmp_path: Path) -> None:
    run(tmp_path, fake_api("v1"))

    unchanged = run(tmp_path, fake_api("v1"))
    assert unchanged.downloaded == 0 and unchanged.skipped_unchanged == 3

    changed = run(tmp_path, fake_api("v2"))
    assert changed.downloaded == 3


def test_max_services_limits_the_sample(tmp_path: Path) -> None:
    stats = run(tmp_path, fake_api("v1"), max_services=2)
    assert stats.listed == 2
    assert sorted(p.name for p in (tmp_path / "services").iterdir()) == ["1.json", "2.json"]


def test_one_failure_does_not_stop_the_run(tmp_path: Path) -> None:
    stats = run(tmp_path, fake_api("v1", fail_ids=frozenset({"2"})))
    assert stats.downloaded == 2 and stats.failed == 1


def test_a_procedure_mitos_no_longer_lists_is_moved_out(tmp_path: Path) -> None:
    run(tmp_path, fake_api("v1"))

    stats = run(tmp_path, fake_api("v1", listed_ids=("1", "3", "4")))

    assert stats.removed == 1 and stats.downloaded == 1
    assert sorted(p.name for p in (tmp_path / "services").iterdir()) == ["1.json", "3.json", "4.json"]
    assert [p.name for p in (tmp_path / "removed").iterdir()] == ["2.json"]


def test_a_sample_or_empty_list_removes_nothing(tmp_path: Path) -> None:
    run(tmp_path, fake_api("v1"))

    sample = run(tmp_path, fake_api("v1"), max_services=1)
    empty = run(tmp_path, fake_api("v1", listed_ids=()))

    assert sample.removed == 0 and empty.removed == 0
    assert len(list((tmp_path / "services").iterdir())) == 3
