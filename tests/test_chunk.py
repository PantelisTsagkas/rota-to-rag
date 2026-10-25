import json
from pathlib import Path

from mitos_rag.chunk import MAX_CHARS, chunk_markdown, split_text
from mitos_rag.render import render_service_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def rendered(name: str) -> str:
    return render_service_markdown(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


def test_short_text_stays_one_window() -> None:
    assert split_text("ένα\nδύο", budget=100) == ["ένα\nδύο"]


def test_windows_respect_the_budget_and_overlap() -> None:
    lines = [f"γραμμή {i:02d} " + "x" * 40 for i in range(20)]  # 50 characters each
    windows = split_text("\n".join(lines), budget=300, overlap=120)

    assert len(windows) > 1
    assert all(len(window) <= 300 for window in windows)
    for previous, current in zip(windows, windows[1:]):
        carried = previous.splitlines()[-2:]  # two 51-character lines fit in 120
        assert current.splitlines()[:2] == carried
    assert set(lines) <= {line for window in windows for line in window.splitlines()}


def test_no_overlap_when_the_last_line_is_longer_than_overlap() -> None:
    windows = split_text("α" * 90 + "\n" + "β" * 90 + "\n" + "γ" * 90, budget=200, overlap=50)

    assert windows == ["α" * 90 + "\n" + "β" * 90, "γ" * 90]


def test_over_long_line_is_wrapped_at_sentences_then_spaces() -> None:
    line = "Πρώτη πρόταση εδώ. " + "λέξη " * 60
    windows = split_text(line, budget=100, overlap=0)

    assert windows[0] == "Πρώτη πρόταση εδώ."
    assert all(len(window) <= 100 for window in windows)
    assert " ".join(windows).split() == line.split()


def test_passport_chunks_fit_and_carry_title_and_heading() -> None:
    chunks = chunk_markdown("144400", rendered("service_144400.json"))

    assert all(len(chunk.text) <= MAX_CHARS for chunk in chunks)
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert chunks[0].section == "header"
    for chunk in chunks[1:]:
        assert chunk.text.startswith("Έκδοση Ελληνικού Διαβατηρίου")
        assert f"## {chunk.section}\n\n" in chunk.text
    # The passport documents section is long, so it is split.
    assert sum(chunk.section == "Δικαιολογητικά" for chunk in chunks) > 1
    assert "Άλλες ονομασίες" not in {chunk.section for chunk in chunks}


def test_chunking_is_deterministic() -> None:
    markdown = rendered("service_977354.json")

    first = [chunk.content_hash for chunk in chunk_markdown("977354", markdown)]
    assert first == [chunk.content_hash for chunk in chunk_markdown("977354", markdown)]
