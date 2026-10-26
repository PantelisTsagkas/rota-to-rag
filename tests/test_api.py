import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
from fastapi.testclient import TestClient

from mitos_rag.answer import ATTRIBUTION, WARNING, Answer, Cancelled, Candidate, Evidence, Generation, render
from mitos_rag.feedback import MemoryFeedback
from mitos_rag.api import ERRORS, Limits, apply_online, at_a_glance, cost_el, create_app, duration_el, page_url, procedure_link, time_el
from mitos_rag.validate import validate

PASSPORT = {
    "procedure_id": "144400",
    "title": "Έκδοση Ελληνικού Διαβατηρίου",
    "source_url": "https://id.mitos.gov.gr/144400",
    "last_updated": "2026-09-14T10:57:32+03:00",
    "costs": [{"amount_min": 80.0, "amount_max": 80.0}],
    "deadline": "P5D",
    "can_apply_online": False,
}
FACTS = {"144400": PASSPORT}
DASHBOARD = Path(__file__).parents[1] / "terraform" / "app" / "dashboard.tf"


def output(**overrides: Any) -> dict[str, Any]:
    base = {
        "status": "answered",
        "answer_text": "Κοστίζει 80 € — με e-Παράβολο.",
        "procedure_ids": ["144400"],
        "facts_used": [{"type": "cost", "value": "80", "procedure_id": "144400"}],
        "missing_facts": [],
        "clarifying_question": "",
        "options": [],
    }
    return {**base, **overrides}


def answer_for(model_output: dict[str, Any]) -> Answer:
    evidence = Evidence("διαβατηριο", [Candidate("144400", PASSPORT["title"], 0.7)], [], FACTS)
    cleaned, validation = validate(model_output, evidence.candidate_ids, FACTS)
    generation = Generation(model_output, 11000, 400, 8.0, False)
    text = render(cleaned, validation, FACTS)
    return Answer("διαβατηριο", cleaned["status"], text, evidence, model_output, cleaned, validation, generation, {"generate": 8.0})


class FakeAssistant:
    def __init__(self, model_output: dict[str, Any] | None = None, fail: bool = False, leave: bool = False) -> None:
        self.model_output = model_output or output()
        self.fail = fail
        self.leave = leave
        self.questions: list[str] = []
        self.stops_seen: list[bool] = []

    def ask(self, question: str, stop: Any = lambda: False, on_event: Any = None) -> Answer:
        self.questions.append(question)
        self.stops_seen.append(stop())
        if self.leave:
            raise Cancelled
        if self.fail:
            raise RuntimeError("ThrottlingException")
        if on_event is not None:
            on_event({"type": "found", "titles": [PASSPORT["title"]]})
            for piece in ("Κοστίζει ", "80 € - με e-Παράβολο."):
                on_event({"type": "text", "text": piece})
        return answer_for(self.model_output)


def client(assistant: Any, limits: Limits | None = None, busy_timeout_s: float = 1.0) -> TestClient:
    return TestClient(create_app(assistant, limits or Limits(per_ip_per_minute=100, daily=100), busy_timeout_s))


def test_ask_returns_structured_fields_with_sources() -> None:
    response = client(FakeAssistant()).post("/ask", json={"question": "  ποσο κανει το διαβατηριο  "})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "answered"
    assert body["answer"] == "Κοστίζει 80 € - με e-Παράβολο."  # no em dashes reach the user
    assert body["sources"] == [
        {"procedure_id": "144400", "title": PASSPORT["title"], "url": "https://id.mitos.gov.gr/144400",
         "page_url": page_url(PASSPORT["title"]), "last_updated": "2026-09-14", "at_a_glance": [], "apply_online": None}
    ]
    assert body["warning"] is None and body["attribution"] == ATTRIBUTION
    assert body["clarifying_question"] is None and body["options"] == []
    assert "https://id.mitos.gov.gr/144400" in body["text"]


def test_an_unchecked_amount_carries_the_warning() -> None:
    wrong = output(answer_text="Κοστίζει 75 €.", facts_used=[])
    assert client(FakeAssistant(wrong)).post("/ask", json={"question": "διαβατηριο"}).json()["warning"] == WARNING


def test_ambiguous_returns_the_question_and_linked_options() -> None:
    options = [{"label": "Ενήλικας", "procedure_id": "144400"}, {"label": "Ανήλικος", "procedure_id": "144400"}]
    ambiguous = output(status="ambiguous", clarifying_question="Για ποιον είναι;", options=options, facts_used=[])
    body = client(FakeAssistant(ambiguous)).post("/ask", json={"question": "διαβατηριο"}).json()
    assert body["clarifying_question"] == "Για ποιον είναι;"
    assert [opt["label"] for opt in body["options"]] == ["Ενήλικας", "Ανήλικος"]
    assert body["options"][0]["url"] == "https://id.mitos.gov.gr/144400"
    assert body["options"][0]["page_url"].startswith("https://mitos.gov.gr/index.php/")


def fee(amount_min: float | None, amount_max: float | None, alternative_of: int | None = None, **extra: Any) -> dict[str, Any]:
    return {"cost_type": "e-Παράβολο", "amount_min": amount_min, "amount_max": amount_max,
            "calculation": ["Εφάπαξ"], "alternative_of": alternative_of, **extra}


def test_the_cost_tile_shows_one_plain_answer_or_nothing() -> None:
    assert cost_el({"is_free": True, "costs": []}) == "Δωρεάν"
    assert cost_el({"is_free": False, "costs": [fee(80.0, 80.0)]}) == "80 €"
    assert cost_el({"is_free": False, "costs": [fee(62.5, 62.5)]}) == "62,50 €"
    # The passport: one fee and its alternatives (under 14, replacement) is a range, not "from 58".
    passport = [fee(80.0, 80.0), fee(71.0, 71.0, alternative_of=1), fee(58.0, 58.0, alternative_of=1)]
    assert cost_el({"is_free": False, "costs": passport}) == "58 έως 80 €"
    # Fines are not fees.
    assert cost_el({"is_free": False, "costs": [fee(80.0, 80.0), fee(500.0, 500.0, cost_type="Πρόστιμο")]}) == "80 €"
    # Unknown, never 0, and no tile when the row cannot give one plain answer.
    for costs in (
        [fee(10.0, 10.0), fee(0.5, 0.5)],  # two fees that add up
        [fee(None, 10.0)],
        [fee(30.0, 10.0)],  # min > max
        [fee(10.0, 10.0, calculation=["Ποσοστό"])],
        [],
    ):
        assert cost_el({"is_free": False, "costs": costs}) is None


def test_durations_read_in_greek() -> None:
    assert duration_el("P1M") == "1 μήνα" and duration_el("P3M") == "3 μήνες"
    assert duration_el("P1Y6M") == "1 έτος και 6 μήνες"
    assert duration_el("PT15M") == "15 λεπτά" and duration_el("PT1H") == "1 ώρα"
    for unreadable in (None, "", "P", "PT0S", "PT30S", "P0D", "3 μήνες"):
        assert duration_el(unreadable) is None
    assert time_el({"estimated_min": None, "estimated_max": "P7D"}) == "Έως 7 ημέρες"
    assert time_el({"estimated_min": "P2D", "estimated_max": "P3D"}) == "2 έως 3 ημέρες"
    assert time_el({"estimated_min": "P10D", "estimated_max": "P1M"}) == "10 ημέρες έως 1 μήνα"
    assert time_el({"estimated_min": "P2D", "estimated_max": None}) is None


def test_at_a_glance_keeps_only_the_tiles_the_row_can_fill() -> None:
    criminal_record = {
        "is_free": True, "costs": [], "submission_channels": ["in_person", "post", "digital"],
        "estimated_min": "P2D", "estimated_max": "P3D", "validity": "P3M",
    }
    assert at_a_glance(criminal_record) == [
        {"label": "Κόστος", "value": "Δωρεάν"},
        {"label": "Αίτηση", "value": "Ηλεκτρονικά, αυτοπροσώπως ή ταχυδρομικά"},
        {"label": "Διάρκεια", "value": "2 έως 3 ημέρες"},
        {"label": "Ισχύει για", "value": "3 μήνες"},
    ]
    assert at_a_glance({"is_free": False, "costs": [], "submission_channels": ["in_person"]}) == [
        {"label": "Αίτηση", "value": "Αυτοπροσώπως"}
    ]


def test_the_online_link_needs_digital_applications_and_https() -> None:
    url = "https://www.gov.gr/el/services/1001086/apodeiktiko-phorologikes-enemerotetas"
    assert apply_online({"can_apply_online": True, "online_urls": [url]}) == {"url": url, "host": "gov.gr"}
    # Some rows list an information portal although the procedure takes no digital applications.
    assert apply_online({"can_apply_online": False, "online_urls": [url]}) is None
    assert apply_online({"can_apply_online": None, "online_urls": [url]}) is None
    assert apply_online({"can_apply_online": True, "online_urls": []}) is None
    assert apply_online({"can_apply_online": True, "online_urls": ["http://example.gr", "javascript:alert(1)"]}) is None
    assert apply_online({"can_apply_online": True, "online_urls": ["http://a.gr", "https://www1.aade.gr/x"]})["host"] == "www1.aade.gr"


def test_page_url_is_the_mitos_page_named_after_the_title() -> None:
    # Checked by hand against mitos.gov.gr on 2026-10-22: each of these answered 200.
    assert page_url("Αποζημίωση θυμάτων από εγκλήματα βίας") == (
        "https://mitos.gov.gr/index.php/%CE%94%CE%94%3A"
        "%CE%91%CF%80%CE%BF%CE%B6%CE%B7%CE%BC%CE%AF%CF%89%CF%83%CE%B7_%CE%B8%CF%85%CE%BC%CE%AC%CF%84%CF%89%CE%BD_"
        "%CE%B1%CF%80%CF%8C_%CE%B5%CE%B3%CE%BA%CE%BB%CE%AE%CE%BC%CE%B1%CF%84%CE%B1_%CE%B2%CE%AF%CE%B1%CF%82"
    )
    # "/" stays a path character; "&", "%" and "+" are encoded so they stay part of the name.
    assert page_url("Α/Β & Γ 5% +1").endswith("%3A%CE%91/%CE%92_%26_%CE%93_5%25_%2B1")


def test_page_link_follows_the_namespace_and_falls_back_to_the_id_address() -> None:
    title = "Διοικητική έρευνα"
    assert page_url(title, "ΠΑ").startswith("https://mitos.gov.gr/index.php/%CE%A0%CE%91%3A")
    assert procedure_link({"procedure_id": "1", "title": title, "ns": "ΠΑ", "source_url": "x"}) == page_url(title, "ΠΑ")
    # Rows written before `ns` existed are ΔΔ.
    assert procedure_link({"procedure_id": "1", "title": title, "source_url": "x"}) == page_url(title)
    # 990784 failed the page check, so it links the id address, which redirects to the live page.
    row = {"procedure_id": "990784", "title": title, "ns": "ΔΔ", "source_url": "https://id.mitos.gov.gr/990784"}
    assert procedure_link(row) == "https://id.mitos.gov.gr/990784"


def test_question_length_is_checked_before_any_call() -> None:
    assistant = FakeAssistant()
    api = client(assistant)
    for question in ("α", "α" * 301):
        response = api.post("/ask", json={"question": question})
        assert response.status_code == 400 and response.json() == {"error": ERRORS["length"]}
    assert assistant.questions == []


def test_per_ip_and_daily_limits() -> None:
    now = [0.0]
    per_ip = Limits(per_ip_per_minute=2, daily=100, clock=lambda: now[0])
    assert [per_ip.check("1.1.1.1") for _ in range(3)] == [None, None, "rate"]
    assert per_ip.check("2.2.2.2") is None  # another client is not affected
    now[0] = 60.0
    assert per_ip.check("1.1.1.1") is None  # the window moved on

    day = ["2026-10-22"]
    daily = Limits(per_ip_per_minute=100, daily=2, today=lambda: day[0])
    assert [daily.check(ip) for ip in ("a", "b", "c")] == [None, None, "daily"]
    day[0] = "2026-10-23"
    assert daily.check("c") is None

    response = client(FakeAssistant(), Limits(per_ip_per_minute=1, daily=100))
    response.post("/ask", json={"question": "διαβατηριο"})
    refused = response.post("/ask", json={"question": "διαβατηριο"})
    assert refused.status_code == 429 and refused.json() == {"error": ERRORS["rate"]}


def test_a_provider_failure_is_a_greek_message_not_a_stack_trace() -> None:
    response = client(FakeAssistant(fail=True)).post("/ask", json={"question": "διαβατηριο"})
    assert response.status_code == 503 and response.json() == {"error": ERRORS["provider"]}


def test_a_second_question_while_one_is_running_gets_busy() -> None:
    release = threading.Event()
    started = threading.Event()

    class SlowAssistant(FakeAssistant):
        def ask(self, question: str, stop: Any = lambda: False) -> Answer:
            started.set()
            release.wait(5)
            return super().ask(question, stop)

    api = client(SlowAssistant(), busy_timeout_s=0.1)
    first: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: first.update(r=api.post("/ask", json={"question": "πρώτη ερώτηση"})))
    thread.start()
    started.wait(5)
    second = api.post("/ask", json={"question": "δεύτερη ερώτηση"})
    release.set()
    thread.join(5)
    assert second.status_code == 503 and second.json() == {"error": ERRORS["busy"]}
    assert first["r"].status_code == 200


def test_the_api_never_writes_a_new_questions_vector(tmp_path: Path) -> None:
    from mitos_rag.answer import Assistant
    from mitos_rag.embed import VectorCache, text_hash

    class FakeEmbedder:
        def embed(self, texts: list[str], kind: str) -> tuple[np.ndarray, int]:
            return np.ones((len(texts), 2), dtype=np.float32), 0

    queries = VectorCache(tmp_path / "query")
    retriever = type("R", (), {"queries": queries})()
    assistant = Assistant(retriever, FakeEmbedder(), {}, FACTS, None, persist_queries=False)  # type: ignore[arg-type]
    question = "νέα ερώτηση με προσωπικά στοιχεία"
    assert assistant.query_vector(question).shape == (2,)
    assert text_hash(question) not in queries
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []



def test_the_api_checks_for_a_disconnect_and_frees_the_lock_when_the_client_left() -> None:
    staying = FakeAssistant()
    assert client(staying).post("/ask", json={"question": "διαβατηριο"}).status_code == 200
    assert staying.stops_seen == [False]  # the stop check ran and the client was still there

    leaving = FakeAssistant(leave=True)
    api = client(leaving)
    assert api.post("/ask", json={"question": "διαβατηριο"}).status_code == 499
    leaving.leave = False
    assert api.post("/ask", json={"question": "διαβατηριο"}).status_code == 200  # the lock was released

def stream_events(response: Any) -> list[dict[str, Any]]:
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines()]


def test_ask_stream_sends_found_text_then_every_ask_field() -> None:
    api = client(FakeAssistant())
    events = stream_events(api.post("/ask/stream", json={"question": "ποσο κανει το διαβατηριο"}))

    assert events[0] == {"type": "found", "titles": [PASSPORT["title"]]}
    assert [event["text"] for event in events[1:-1]] == ["Κοστίζει ", "80 € - με e-Παράβολο."]
    done = events[-1]
    assert done.pop("type") == "done"
    plain = api.post("/ask", json={"question": "ποσο κανει το διαβατηριο"}).json()
    # Each answer is recorded under its own id for a vote; everything else is the same.
    assert re.fullmatch(r"[0-9a-f]{32}", done.pop("answer_id")) and re.fullmatch(r"[0-9a-f]{32}", plain.pop("answer_id"))
    assert done == plain


def test_an_answer_can_get_one_vote_and_one_reason_never_the_question() -> None:
    store = MemoryFeedback()
    api = TestClient(create_app(FakeAssistant(), Limits(per_ip_per_minute=100, daily=100), 1.0, feedback=store))
    answer_id = api.post("/ask", json={"question": "ποσο κανει το διαβατηριο"}).json()["answer_id"]
    # Recorded: status, cited procedures, cost and date. Not the question.
    assert set(store.rows[answer_id]) == {"status", "procedure_ids", "cost_usd", "day"}
    assert store.rows[answer_id]["procedure_ids"] == ["144400"]
    assert "διαβατηριο" not in json.dumps(store.rows, ensure_ascii=False)

    def vote(**body: Any) -> Any:
        return api.post("/ask/feedback", json={"answer_id": answer_id, **body})

    assert vote(helpful=False).status_code == 204
    assert vote(helpful=True).status_code == 409  # one vote per answer
    assert vote(helpful=False, reason="missing_info").status_code == 204
    assert vote(helpful=False, reason="other").status_code == 409  # one reason
    assert store.rows[answer_id]["helpful"] is False and store.rows[answer_id]["reason"] == "missing_info"


def test_feedback_needs_a_real_answer_and_a_known_reason() -> None:
    store = MemoryFeedback()
    api = TestClient(create_app(FakeAssistant(), Limits(per_ip_per_minute=100, daily=100), 1.0, feedback=store))
    answer_id = api.post("/ask", json={"question": "διαβατηριο"}).json()["answer_id"]
    assert api.post("/ask/feedback", json={"answer_id": "0" * 32, "helpful": True}).status_code == 409
    for bad in (
        {"answer_id": "not-an-id", "helpful": True},
        {"answer_id": answer_id, "helpful": False, "reason": "free text from a visitor"},
        {"answer_id": answer_id, "helpful": True, "reason": "missing_info"},  # a reason goes with "no"
    ):
        assert api.post("/ask/feedback", json=bad).status_code == 400
    assert "helpful" not in store.rows[answer_id]


def test_ambiguous_answers_get_no_vote_and_a_failed_record_still_answers() -> None:
    options = [{"label": "Ενήλικας", "procedure_id": "144400"}, {"label": "Ανήλικος", "procedure_id": "144400"}]
    ambiguous = output(status="ambiguous", clarifying_question="Για ποιον;", options=options, facts_used=[])
    assert client(FakeAssistant(ambiguous)).post("/ask", json={"question": "διαβατηριο"}).json()["answer_id"] is None

    class BrokenStore(MemoryFeedback):
        def record(self, *args: Any) -> None:
            raise RuntimeError("DynamoDB unreachable")

    api = TestClient(create_app(FakeAssistant(), Limits(per_ip_per_minute=100, daily=100), 1.0, feedback=BrokenStore()))
    body = api.post("/ask", json={"question": "διαβατηριο"}).json()
    assert body["status"] == "answered" and body["answer_id"] is None


def test_ask_stream_refusals_are_plain_errors_and_failures_an_error_event() -> None:
    refused = client(FakeAssistant()).post("/ask/stream", json={"question": "α"})
    assert refused.status_code == 400 and refused.json() == {"error": ERRORS["length"]}

    failing = client(FakeAssistant(fail=True))
    assert stream_events(failing.post("/ask/stream", json={"question": "διαβατηριο"})) == [
        {"type": "error", "error": ERRORS["provider"]}
    ]


def test_ask_stream_frees_the_lock_after_an_answer_a_failure_or_a_client_that_left() -> None:
    assistant = FakeAssistant(fail=True)
    api = client(assistant, busy_timeout_s=0.5)
    api.post("/ask/stream", json={"question": "διαβατηριο"})
    assistant.fail, assistant.leave = False, True
    assert stream_events(api.post("/ask/stream", json={"question": "διαβατηριο"})) == []
    assistant.leave = False
    for _ in range(2):
        assert stream_events(api.post("/ask/stream", json={"question": "διαβατηριο"}))[-1]["type"] == "done"
    assert assistant.stops_seen == [False] * 4  # the disconnect check ran each time


WEB = Path(__file__).resolve().parents[1] / "web"


def test_the_ui_is_served_at_root_and_the_api_still_answers() -> None:
    api = TestClient(create_app(FakeAssistant(), Limits(per_ip_per_minute=100, daily=100), 1.0, web_dir=WEB))
    page = api.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Ρώτα το" in page.text
    for asset in ("/app.js", "/style.css", "/favicon.svg", "/apple-touch-icon.png", "/fonts/commissioner-greek.woff2"):
        assert api.get(asset).status_code == 200
    # The mount comes after the routes, so it never swallows them.
    assert api.get("/health").json() == {"status": "ok"}
    asked = api.post("/ask", json={"question": "διαβατηριο"})
    assert asked.status_code == 200 and asked.json()["status"] == "answered"


def test_without_a_web_dir_there_is_no_ui() -> None:
    assert client(FakeAssistant()).get("/").status_code == 404


def test_the_ui_makes_no_third_party_requests_and_stores_nothing() -> None:
    import re

    page = (WEB / "index.html").read_text()
    script = (WEB / "app.js").read_text()
    # Scripts and styles are local files; the only absolute link is the credit to mitos.gov.gr.
    assert re.findall(r'(?:src|href)="(https?://[^"]+)"', page) == ["https://mitos.gov.gr"]
    style = (WEB / "style.css").read_text()
    assert "@import" not in style
    assert re.findall(r'url\("?(https?:|//)', style) == []  # the font is served from this site
    # Terraform uploads every file in web/ with a content type looked up by extension.
    assert {path.suffix for path in WEB.rglob("*") if path.is_file()} <= {".html", ".js", ".css", ".svg", ".png", ".woff2", ".txt"}
    for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "innerHTML", "location.search"):
        assert forbidden not in script


def test_the_footer_keeps_the_privacy_note() -> None:
    # Each claim was checked against the code and the account (no Bedrock invocation logging,
    # eu.* profiles route only to EU regions). If one stops being true, change the note too.
    page = (WEB / "index.html").read_text()
    assert '<details class="privacy">' in page
    for claim in ("Δεν αποθηκεύουμε την ερώτησή σας", "Ευρωπαϊκή Ένωση", "(hash)", "90 ημέρες"):
        assert claim in page


def test_a_busy_reply_counts_against_no_limit() -> None:
    class HeldLock:
        def acquire(self, timeout: float = 0.0) -> bool:
            return False

        def release(self) -> None:
            raise AssertionError("never acquired")

    limits = Limits(per_ip_per_minute=1, daily=1)
    api = TestClient(create_app(FakeAssistant(), limits, 0.0, lock=HeldLock()))
    for _ in range(3):
        busy = api.post("/ask", json={"question": "διαβατηριο"})
        assert busy.status_code == 503 and busy.json() == {"error": ERRORS["busy"]}
    assert limits.check("testclient") is None  # the three busy replies used none of the quota


def test_a_refusal_or_a_limits_failure_frees_the_lock() -> None:
    class BrokenLimits:
        def check(self, ip: str) -> str | None:
            raise RuntimeError("DynamoDB unreachable")

    lock = threading.Lock()
    refused = TestClient(create_app(FakeAssistant(), Limits(per_ip_per_minute=100, daily=0), 0.1, lock=lock))
    assert refused.post("/ask", json={"question": "διαβατηριο"}).json() == {"error": ERRORS["daily"]}
    broken = TestClient(create_app(FakeAssistant(), BrokenLimits(), 0.1, lock=lock))  # type: ignore[arg-type]
    failed = broken.post("/ask/stream", json={"question": "διαβατηριο"})
    assert failed.status_code == 503 and failed.json() == {"error": ERRORS["provider"]}
    assert lock.acquire(timeout=0)  # still free


def test_behind_cloudfront_the_limit_is_per_visitor_not_per_socket() -> None:
    limits = Limits(per_ip_per_minute=1, daily=100)
    api = TestClient(create_app(FakeAssistant(), limits, 1.0, ip_header="cloudfront-viewer-address"))
    ask = lambda address: api.post(  # noqa: E731
        "/ask", json={"question": "διαβατηριο"}, headers={"CloudFront-Viewer-Address": address}
    ).status_code
    assert ask("203.0.113.7:51234") == 200
    assert ask("203.0.113.7:60001") == 429  # same visitor, another port
    assert ask("2001:db8::1:443") == 200  # IPv6: only the port is dropped
    assert ask("2001:db8::2:443") == 200


def test_the_dashboard_queries_still_read_the_log_lines(caplog: Any) -> None:
    # The CloudWatch dashboard parses these lines with Logs Insights regexes: if a line changes,
    # the dashboard goes blank without an error, so the regexes are read from the Terraform here.
    terraform = DASHBOARD.read_text(encoding="utf-8").replace("\\\\", "\\")
    questions = re.compile(re.search(r'questions_filter = "filter @message like /(.+?)/"', terraform)[1])
    status, vote = (re.compile(found.replace("(?<", "(?P<")) for found in re.findall(r"parse @message /(.+?)/ \|", terraform))
    api = TestClient(create_app(FakeAssistant(), Limits(per_ip_per_minute=100, daily=100), 1.0, feedback=MemoryFeedback()))
    with caplog.at_level(logging.INFO, logger="mitos_rag.api"):
        answer_id = api.post("/ask", json={"question": "ποσο κανει το διαβατηριο"}).json()["answer_id"]
        for vote_body in ({"helpful": False}, {"helpful": False, "reason": "missing_info"}):  # the UI's two steps
            assert api.post("/ask/feedback", json={"answer_id": answer_id, **vote_body}).status_code == 204
    lines = [record.getMessage() for record in caplog.records if record.name == "mitos_rag.api"]
    asked = [line for line in lines if questions.search(line)]
    assert len(asked) == 1
    parsed = status.search(asked[0])
    assert parsed and parsed["status"] == "answered" and float(parsed["cost"]) >= 0 and float(parsed["total"]) >= 0
    voted = [found for line in lines if (found := vote.search(line))]
    assert [(found["helpful"], found["reason"]) for found in voted] == [("False", "None"), ("False", "missing_info")]
    assert not any("διαβατηριο" in line for line in lines)  # the logs never carry the question
