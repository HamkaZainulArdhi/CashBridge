"""Guardrails between the LLM and the numbers.

Principle: the code computes, the LLM explains. This module:
  * wraps untrusted data (uploaded descriptions) so it is treated as data,
  * validates the LLM's structured explanation against the tool outputs,
  * enforces numeric consistency: every figure the LLM states must exist in
    the set of computed values (it may never invent or fudge amounts).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from pydantic import BaseModel, Field


class KeyFigure(BaseModel):
    label: str
    value: float
    source: str = Field(description="Tool name or tool-call id that produced this value")


class Explanation(BaseModel):
    summary: str = Field(description="One or two sentences: what is happening")
    why: str = Field(description="Why it is happening, grounded in the computed drivers")
    next_steps: list[str] = Field(default_factory=list,
                                  description="Recommended actions, non-loan options first")
    key_figures: list[KeyFigure] = Field(default_factory=list,
                                         description="Every figure cited must come from tool output")
    confidence: str = Field(default="medium", description="low | medium | high")


_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[,.]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)([MmKk]?)")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LETTER_DIGIT_RE = re.compile(r"([A-Za-z])(\d)")
_DOTTED_THOUSANDS_RE = re.compile(r"\d{1,3}(?:\.\d{3})+$")


def _text_numbers(text: str) -> list:
    """Numbers in prose, with ISO dates removed first."""
    return extract_numbers(_DATE_RE.sub(" ", text or ""))


def extract_numbers(text: str) -> list:
    """Pull candidate monetary/quantity figures out of free text."""
    found = []
    # "IDR12,373,412" -> "IDR 12,373,412" so the full figure matches instead
    # of a partial tail ("373,412") that would fail grounding.
    spaced = _LETTER_DIGIT_RE.sub(r"\1 \2", text or "")
    for match in _NUMBER_RE.finditer(spaced):
        raw, suffix = match.group(1), match.group(2)
        cleaned = raw.replace(",", "")
        if _DOTTED_THOUSANDS_RE.fullmatch(cleaned):   # 12.373.412 style
            cleaned = cleaned.replace(".", "")
        elif "." in cleaned:
            head, _, tail = cleaned.rpartition(".")
            if len(tail) == 3 and head.isdigit():   # 1.250.000 style
                cleaned = head + tail
        try:
            value = float(cleaned)
        except ValueError:
            continue
        if suffix in ("M", "m"):
            value *= 1_000_000
        elif suffix in ("K", "k"):
            value *= 1_000
        found.append(value)
    return found


def match_allowed(value: float, allowed: Iterable[float], rel_tolerance: float = 0.02) -> bool:
    for candidate in allowed:
        if candidate == 0:
            if abs(value) < 1:
                return True
            continue
        if abs(value - candidate) / abs(candidate) <= rel_tolerance:
            return True
    return False


def validate_explanation(explanation: Explanation, allowed_values: Iterable[float],
                         known_sources: Optional[Iterable[str]] = None) -> dict:
    """Return {"ok": bool, "issues": [str, ...]}.

    Checks:
      1. key_figures values must come from computed tool outputs,
      2. numbers in the free text must also be matched (or be small counts),
      3. cited sources must be tools that actually ran.
    """
    allowed = [float(v) for v in allowed_values]
    known = set(known_sources or [])
    issues = []

    for figure in explanation.key_figures:
        if not match_allowed(figure.value, allowed):
            issues.append(f"key figure '{figure.label}' = {figure.value:,.0f} "
                          f"does not match any computed value")
        if known and figure.source not in known:
            issues.append(f"key figure '{figure.label}' cites unknown source '{figure.source}'")

    for field_name in ("summary", "why"):
        for number in _text_numbers(getattr(explanation, field_name)):
            if number < 100:            # small counts (days, months) are fine
                continue
            if not match_allowed(number, allowed):
                issues.append(f"number {number:,.0f} in '{field_name}' is not grounded "
                              f"in computed outputs")

    for step in explanation.next_steps:
        for number in _text_numbers(step):
            if number < 100:
                continue
            if not match_allowed(number, allowed):
                issues.append(f"number {number:,.0f} in a next step is not grounded "
                              f"in computed outputs")

    return {"ok": not issues, "issues": issues}


def wrap_untrusted(label: str, text: str, max_len: int = 400) -> str:
    """Wrap data that came from uploads so the LLM treats it as data, not orders."""
    clipped = (text or "")[:max_len]
    return f"<untrusted_data source=\"{label}\">\n{clipped}\n</untrusted_data>"
