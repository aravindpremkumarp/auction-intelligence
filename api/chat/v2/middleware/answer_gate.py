"""
api/chat/v2/middleware/answer_gate.py
-------------------------------------
Checks the drafted answer against the turn's tool results before it ships.

**Why this is still needed with a graph database underneath.** Neo4j
guarantees the *tool result* is correct. It cannot guarantee the model
*transcribed* it correctly — and transcription is where a chat agent
fabricates: a price that drifts by a digit, a count taken from a 10-row
sample instead of `total_count`, an auction_id that never appeared. The gate
checks the transcription, not the database.

**Phase 1 is report-only.** It computes a verdict and logs it; it never
rewrites an answer or spends a repair call. The fire rate is unknown, and the
design rule that keeps this cheap is that the expensive path must be the
exception path — so the rate gets measured before anything is built on top of
it. Enforcement is a Phase 3 decision with data behind it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 6-digit auction ids, the real format. Bounded so a year or a price fragment
# doesn't read as an id.
_ID_RE = re.compile(r"\b\d{6}\b")

# Rupee amounts the answer asserts: "Rs 35,00,000", "₹3.5 crore", "4000000".
_MONEY_RE = re.compile(
    r"(?:₹|\bRs\.?\s*|\bINR\s*)([\d,]+(?:\.\d+)?)\s*"
    r"(lakhs?|lakh|crores?|crore|cr|l)?",
    re.I,
)

_MULTIPLIER = {"lakh": 100_000, "lakhs": 100_000, "l": 100_000,
               "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000}

# Small integers are ordinals, ranks and list positions far more often than
# they are claims about the data ("the top 3", "2 of them"). Checking them
# produces noise, not signal.
_MIN_CHECKED_COUNT = 4


@dataclass
class GateVerdict:
    ok: bool = True
    unsupported_ids: list[str] = field(default_factory=list)
    unsupported_amounts: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        parts = []
        if self.unsupported_ids:
            parts.append(f"auction_ids not in results: {self.unsupported_ids}")
        if self.unsupported_amounts:
            parts.append(f"amounts not in results: {self.unsupported_amounts}")
        return "; ".join(parts)


def check_answer(answer: str, tool_results: Any, *,
                 recommendation: Any = None,
                 extra_numbers: list[float] | None = None,
                 extra_ids: list[str] | None = None) -> GateVerdict:
    """Verify every id and rupee amount in the answer appears in the results.

    Deliberately one-directional: it catches values the answer asserts that
    the data never produced. It does not require the answer to mention
    everything the data returned — summarising is the job.
    """
    verdict = GateVerdict()
    if not answer:
        return verdict

    haystack = _flatten(tool_results)
    text = answer
    if recommendation is not None:
        text += " " + _flatten(recommendation)

    known_ids = set(_ID_RE.findall(haystack)) | {str(i) for i in (extra_ids or [])}
    for candidate in set(_ID_RE.findall(text)):
        if candidate not in known_ids:
            verdict.unsupported_ids.append(candidate)

    known_numbers = _numbers_in(haystack) | {float(n) for n in (extra_numbers or [])}
    for raw, value in _amounts_in(text):
        if value is None:
            continue
        if not any(_close(value, known) for known in known_numbers):
            verdict.unsupported_amounts.append(raw)

    verdict.ok = not (verdict.unsupported_ids or verdict.unsupported_amounts)
    if not verdict.ok:
        # Report-only in Phase 1: the fire rate is what decides whether
        # enforcement is affordable.
        logger.warning("chat v2 answer gate: %s", verdict.reason)
    return verdict


def _flatten(value: Any) -> str:
    """Render any value as searchable text.

    Pydantic models get `model_dump_json`, not `json.dumps(..., default=str)`:
    the latter falls back to `str(model)`, which is a repr — so the
    Recommendation half of the gate would quietly check nothing at all.
    """
    import json

    if isinstance(value, str):
        return value
    dump = getattr(value, "model_dump_json", None)
    if callable(dump):
        try:
            return dump()
        except (TypeError, ValueError):
            pass
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values()) + " " + \
               " ".join(str(k) for k in value)
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _flatten(vars(value))
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _numbers_in(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in re.findall(r"\d[\d,]*\.?\d*", text)
            if m.strip(",.")}


def _amounts_in(text: str) -> list[tuple[str, float | None]]:
    out: list[tuple[str, float | None]] = []
    for match in _MONEY_RE.finditer(text):
        raw = match.group(0).strip()
        try:
            value = float(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            out.append((raw, None))
            continue
        unit = (match.group(2) or "").lower()
        out.append((raw, value * _MULTIPLIER.get(unit, 1)))
    return out


def _close(claimed: float, known: float, tolerance: float = 0.01) -> bool:
    """Rounding in prose is honest ("about Rs 35L" for 3,499,000); a different
    number is not. One percent separates the two."""
    if claimed == known:
        return True
    if known == 0:
        return False
    return abs(claimed - known) / abs(known) <= tolerance
