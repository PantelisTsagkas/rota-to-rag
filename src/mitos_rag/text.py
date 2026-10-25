"""Tokenising Greek text for keyword search.

Three modes, so the eval can measure what each step buys (PRD 6.2):

- raw: casefold only. "Διαβατηρίου" and "διαβατηριου" stay different tokens.
  Unicode casefolding already maps final sigma ς to σ.
- normalised: casefold, then strip accents and diaeresis.
- stemmed: normalised, then the Snowball Greek stemmer, so "ταυτοτητα" and
  "ταυτοτητασ" both become "ταυτοτητ". The stemmer is rule-based and not
  perfect: "διαβατηριο" -> "διαβατηρι" but "διαβατηριου" -> "διαβατηρ".
"""

import re
import unicodedata
from typing import Literal

import Stemmer

TokenMode = Literal["raw", "normalised", "stemmed"]
TOKEN_MODES: tuple[TokenMode, ...] = ("raw", "normalised", "stemmed")

_WORD = re.compile(r"\w+")
_STEMMER = Stemmer.Stemmer("greek")


def normalise(text: str) -> str:
    """'Ταυτότητας' -> 'ταυτοτητασ', 'Ευρωπαϊκή' -> 'ευρωπαικη'."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    # casefold() has already turned ς into σ; the replace keeps that explicit.
    return unicodedata.normalize("NFC", stripped).replace("ς", "σ")


def tokenize(text: str, mode: TokenMode = "normalised") -> list[str]:
    if mode == "raw":
        return _WORD.findall(unicodedata.normalize("NFC", text.casefold()))
    tokens = _WORD.findall(normalise(text))
    if mode == "normalised":
        return tokens
    if mode == "stemmed":
        return _STEMMER.stemWords(tokens)
    raise ValueError(f"unknown token mode: {mode}")
