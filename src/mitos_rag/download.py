"""Download Mitos procedures to disk.

Output layout:
    <out>/index.jsonl              one ServiceSummary per line (the full list)
    <out>/services/<id>.json       full procedure, exactly as the API returned it

Re-running is incremental: a procedure is fetched again only when its
`last_updated` in the list differs from the copy on disk.

Usage:
    uv run mitos-download --out data/raw --max-services 20   # quick sample
    uv run mitos-download --out data/raw                     # everything (~4,400)
"""

import argparse
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mitos_rag.client import MitosApiError, MitosClient, ServiceSummary

logger = logging.getLogger(__name__)


@dataclass
class DownloadStats:
    listed: int = 0
    downloaded: int = 0
    skipped_unchanged: int = 0
    failed: int = 0


def service_path(out_dir: Path, service_id: str) -> Path:
    return out_dir / "services" / f"{service_id}.json"


def is_up_to_date(path: Path, summary: ServiceSummary) -> bool:
    if not path.exists():
        return False
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return stored.get("last_updated") == summary.last_updated


def download_all(
    client: MitosClient,
    out_dir: Path,
    max_services: int | None = None,
    delay_seconds: float = 0.25,
    page_size: int = 100,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadStats:
    """Fetch the procedure list, then each changed procedure. Returns counts."""
    stats = DownloadStats()
    (out_dir / "services").mkdir(parents=True, exist_ok=True)

    summaries: list[ServiceSummary] = []
    for summary in client.iter_service_summaries(page_size=page_size):
        summaries.append(summary)
        if max_services is not None and len(summaries) >= max_services:
            break
    stats.listed = len(summaries)

    index_path = out_dir / "index.jsonl"
    with index_path.open("w", encoding="utf-8") as index_file:
        for summary in summaries:
            index_file.write(json.dumps(summary.to_dict(), ensure_ascii=False) + "\n")

    for position, summary in enumerate(summaries, start=1):
        path = service_path(out_dir, summary.id)
        if is_up_to_date(path, summary):
            stats.skipped_unchanged += 1
            continue
        try:
            service = client.get_service(summary.id)
        except MitosApiError as error:
            logger.warning("Failed %s (%s): %s", summary.id, summary.title_el, error)
            stats.failed += 1
            continue
        path.write_text(json.dumps(service, ensure_ascii=False, indent=2), encoding="utf-8")
        stats.downloaded += 1
        if position % 100 == 0:
            logger.info("Progress: %d/%d", position, len(summaries))
        sleep(delay_seconds)  # be polite to a public government API

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Download procedures from the Mitos API.")
    parser.add_argument("--out", type=Path, default=Path("data/raw"))
    parser.add_argument("--max-services", type=int, default=None, help="Stop after N (for a quick sample).")
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds to wait between detail requests.")
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with MitosClient() as client:
        stats = download_all(
            client,
            out_dir=args.out,
            max_services=args.max_services,
            delay_seconds=args.delay,
            page_size=args.page_size,
        )
    logger.info(
        "Done. listed=%d downloaded=%d unchanged=%d failed=%d",
        stats.listed,
        stats.downloaded,
        stats.skipped_unchanged,
        stats.failed,
    )


if __name__ == "__main__":
    main()
