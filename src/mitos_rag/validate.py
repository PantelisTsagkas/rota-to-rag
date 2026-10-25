"""Code checks on a generated answer (PRD 6.5): the model is not the only reader of costs and deadlines.

Checks, against the structured facts table (`facts.jsonl`) and the retrieved candidates:

- every cited `procedure_id` was retrieved,
- every typed fact the model says it used (cost, duration, validity, online) matches
  the facts row of its procedure,
- every euro amount written in the answer text is a cost of a cited procedure,
- every duration written in the answer text (days, weeks, months, years, in numerals) is a
  deadline, estimated duration or validity of a cited procedure. Ages ("14 ετών") and
  durations written in words ("μία ημέρα") are not checked,
- the answer text contains no links (links are built by code from the procedure id).

Amounts and durations in the text have two tiers (decision 010). The facts row is the strong
check. A number that is not there but is written in a cited procedure's own text (for
example the "13 μηνών" passport in a cost description, or a "30 €" debt limit in the
requirements) is text-grounded: listed apart, with no warning. Text matching compares parsed
values, never substrings. Two things never get the second tier: typed facts, which the model
says come from <facts>, and amounts on a line about a total ("Σύνολο", "συνολικά"), because a
sum is not in the registry and a match in the text would be a coincidence.

A failed check never silently passes: it is listed in `Validation`, and the rendered
answer carries a warning (`answer.py`).
"""

import re
from dataclasses import dataclass, field
from typing import Any

from mitos_rag.text import normalise

# 80 €, 62,5 €, 5.87 €, 1.250,00 €, 105 ευρώ, and the legal style "τριάντα (30) ευρώ". Thousands
# use a dot only when followed by exactly three digits; otherwise a dot or comma before one or
# two digits is the decimal mark.
EURO = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\)?\s*(?:€|ευρώ|ευρω|EUR)",
    re.IGNORECASE,
)
# The first number of a range such as "14,50-19,50 €" has no euro sign of its own.
EURO_RANGE_START = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d{1,2})?)\s*[-–]\s*(?=\d[\d.,]*\s*(?:€|ευρώ|ευρω|EUR))", re.IGNORECASE)
# "5 ημέρες", "2-3 εργάσιμες ημέρες", "2 έως 3 ημέρες", "6 μήνες", "10 χρόνια", "10 έτη", "έξι (6) μήνες".
# The genitive "ετών" / "χρονών" is left out on purpose: it is how Greek writes ages.
DURATION = re.compile(
    r"(?<![\d.,])(\d+)\)?(?:\s*(?:[-–]|έως|εως|ως|με|και)\s*\(?(\d+)\)?)?\s*(?:εργάσιμ\w*\s+|εργασιμ\w*\s+)?"
    r"(ημέρ\w*|ημερ\w*|μέρ\w*|μερ\w*|εβδομάδ\w*|εβδομαδ\w*|μήν\w*|μην\w*|έτη|έτος|ετη|ετος|χρόνια|χρόνο|χρονια|χρονο)",
    re.IGNORECASE,
)
# Matched on accent-folded text: "Σύνολο", "συνολικά", "συνολικό κόστος".
TOTAL = re.compile(r"συνολ")
ISO_DURATION = re.compile(r"^P(\d+)([DWMY])$")
URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def euro_amounts(text: str) -> list[float]:
    """Euro amounts in Greek notation: dot for thousands, comma for decimals."""
    amounts = [round(float(m.group(1).replace(",", ".")), 2) for m in EURO_RANGE_START.finditer(text)]
    for match in EURO.finditer(text):
        number = match.group(1)
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?", number):
            number = number.replace(".", "")
        amounts.append(round(float(number.replace(",", ".")), 2))
    return amounts


def total_amounts(text: str) -> set[float]:
    """Amounts on a line about a total. They only pass when the facts row has them."""
    return {amount for line in text.splitlines() if TOTAL.search(normalise(line)) for amount in euro_amounts(line)}


def _unit(word: str) -> str:
    word = word.lower()
    if word.startswith(("μήν", "μην")):
        return "M"
    if word.startswith(("ημ", "μέρ", "μερ")):
        return "D"
    if word.startswith(("εβδ",)):
        return "W"
    return "Y"


def text_durations(text: str) -> list[tuple[int, str]]:
    """(number, unit) for every numeral duration in the text; both ends of a range count."""
    found = []
    for match in DURATION.finditer(text):
        unit = _unit(match.group(3))
        found.append((int(match.group(1)), unit))
        if match.group(2):
            found.append((int(match.group(2)), unit))
    return found


def fact_durations(facts: dict[str, Any]) -> set[tuple[int, str]]:
    """Every duration the facts row states, with weeks also as days."""
    values: set[tuple[int, str]] = set()
    for key in ("deadline", "estimated_min", "estimated_max", "validity"):
        match = ISO_DURATION.match(str(facts.get(key) or ""))
        if match:
            number, unit = int(match.group(1)), match.group(2)
            values.add((number, unit))
            if unit == "W":
                values.add((number * 7, "D"))
    return values


def cost_amounts(facts: dict[str, Any]) -> set[float]:
    """Every amount the facts row states. A null amount is unknown, never 0 (CLAUDE.md data notes)."""
    amounts: set[float] = set()
    for cost in facts.get("costs", []):
        for key in ("amount_min", "amount_max"):
            if cost.get(key) is not None:
                amounts.add(round(float(cost[key]), 2))
    return amounts


def durations(facts: dict[str, Any], kind: str) -> set[str]:
    keys = ("validity",) if kind == "validity" else ("deadline", "estimated_min", "estimated_max")
    return {facts[key] for key in keys if facts.get(key)}


@dataclass
class Validation:
    invalid_procedure_ids: list[str] = field(default_factory=list)
    unsupported_facts: list[dict[str, Any]] = field(default_factory=list)
    unsupported_amounts: list[float] = field(default_factory=list)
    unsupported_durations: list[tuple[int, str]] = field(default_factory=list)
    # Not in the facts row, but written in a cited procedure's text: no warning.
    text_grounded_amounts: list[float] = field(default_factory=list)
    text_grounded_durations: list[tuple[int, str]] = field(default_factory=list)
    supported_facts: list[dict[str, Any]] = field(default_factory=list)
    had_links: bool = False
    status_overridden: str | None = None

    @property
    def ok(self) -> bool:
        return not (
            self.invalid_procedure_ids
            or self.unsupported_facts
            or self.unsupported_amounts
            or self.unsupported_durations
            or self.had_links
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "invalid_procedure_ids": self.invalid_procedure_ids,
            "unsupported_facts": self.unsupported_facts,
            "unsupported_amounts": self.unsupported_amounts,
            "unsupported_durations": [f"{n}{unit}" for n, unit in self.unsupported_durations],
            "text_grounded_amounts": self.text_grounded_amounts,
            "text_grounded_durations": [f"{n}{unit}" for n, unit in self.text_grounded_durations],
            "supported_facts": self.supported_facts,
            "had_links": self.had_links,
            "status_overridden": self.status_overridden,
        }


def fact_supported(fact: dict[str, Any], facts: dict[str, Any] | None) -> bool:
    if facts is None:
        return False
    kind, value = fact["type"], str(fact["value"]).strip()
    if kind == "cost":
        try:
            amount = round(float(value.replace(",", ".")), 2)
        except ValueError:
            return False
        # "free" is a fact only when Mitos says so; a missing amount is unknown, never 0
        return amount in cost_amounts(facts) or (amount == 0 and facts.get("is_free") is True)
    if kind in ("duration", "validity"):
        return value.upper() in durations(facts, kind)
    if kind == "online":
        if value.lower() not in ("yes", "no"):
            return False
        return facts.get("can_apply_online") is (value.lower() == "yes")
    return True  # "other" facts (documents, steps) have no structured field to check against


def validate(
    output: dict[str, Any],
    candidates: list[str],
    facts: dict[str, dict[str, Any]],
    texts: dict[str, str] | None = None,
) -> tuple[dict[str, Any], Validation]:
    """Return a cleaned copy of the model output and what failed. Never trusts the model's ids.

    `texts` maps a procedure id to its rendered text, for the second tier. Without it, every
    amount and duration must be in the facts row.
    """
    result = Validation()
    cleaned = dict(output)
    allowed = set(candidates)

    cited = list(dict.fromkeys(output.get("procedure_ids", [])))
    result.invalid_procedure_ids = [pid for pid in cited if pid not in allowed]
    cleaned["procedure_ids"] = [pid for pid in cited if pid in allowed]
    cleaned["options"] = [opt for opt in output.get("options", []) if opt.get("procedure_id") in allowed]
    result.invalid_procedure_ids += [
        opt.get("procedure_id", "") for opt in output.get("options", []) if opt.get("procedure_id") not in allowed
    ]

    for fact in output.get("facts_used", []):
        pid = fact.get("procedure_id")
        if pid in allowed and fact_supported(fact, facts.get(pid)):
            result.supported_facts.append(fact)
        else:
            result.unsupported_facts.append(fact)

    text = output.get("answer_text", "")
    cited_ids = cleaned["procedure_ids"]
    known = set().union(*(cost_amounts(facts[pid]) for pid in cited_ids if pid in facts))
    known_durations = set().union(*(fact_durations(facts[pid]) for pid in cited_ids if pid in facts))
    cited_texts = [texts[pid] for pid in cited_ids if texts and pid in texts]
    in_text = set().union(*(euro_amounts(doc) for doc in cited_texts))
    in_text_durations = set().union(*(text_durations(doc) for doc in cited_texts))
    totals = total_amounts(text)
    for amount in euro_amounts(text):
        if amount in known:
            continue
        if amount in in_text and amount not in totals:
            result.text_grounded_amounts.append(amount)
        else:
            result.unsupported_amounts.append(amount)
    for duration in text_durations(text):
        if duration in known_durations:
            continue
        if duration in in_text_durations:
            result.text_grounded_durations.append(duration)
        else:
            result.unsupported_durations.append(duration)

    if URL.search(text):
        result.had_links = True
        cleaned["answer_text"] = URL.sub("", text)

    if cleaned.get("status") == "answered" and not cleaned["procedure_ids"]:
        cleaned["status"] = "not_found"
        result.status_overridden = "answered without a retrieved procedure"
    if cleaned.get("status") == "ambiguous" and len(cleaned["options"]) < 2:
        result.status_overridden = "ambiguous with fewer than 2 valid options"
        cleaned["status"] = "answered" if cleaned["procedure_ids"] else "not_found"
    return cleaned, result
