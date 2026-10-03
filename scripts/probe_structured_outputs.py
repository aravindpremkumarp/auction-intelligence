"""Which structured-output tier does each extraction model reach on OpenRouter?

pipeline/reader/llm.py enforces the schema in three tiers — json_schema
(strict decoding), json_object + Pydantic, plain text + Pydantic — because
support differs by model and by the host OpenRouter picks. This probe sends
one tiny schema through each tier for each model and prints the table that
docs/reader-v2.md records. It spends a few hundred tokens per cell.

Run:  python -m scripts.probe_structured_outputs [model ...]
"""
from __future__ import annotations

import sys
import time

from pydantic import BaseModel, Field

from pipeline.config import (OPENROUTER_MODEL_EXTRACT_MULTI,
                             OPENROUTER_MODEL_EXTRACT_RETRY,
                             OPENROUTER_MODEL_EXTRACT_SINGLE)
from pipeline.reader.llm import TIERS, Truncated, Unparseable, call_structured


class _Probe(BaseModel):
    reserve_price_quote: str = Field(..., description="the reserve price, verbatim")
    unit: str | None = Field(None, description="rupees | lakh | crore")
    village: str | None = None


_TEXT = ("Sale notice. Reserve Price (In Lakhs): 57.34. EMD: 5.73. The property "
         "is situated at Kelambakkam Village, Chengalpattu Taluk.")


def main(argv: list[str] | None = None) -> int:
    models = (argv or sys.argv[1:]) or sorted({OPENROUTER_MODEL_EXTRACT_SINGLE,
                                               OPENROUTER_MODEL_EXTRACT_MULTI,
                                               OPENROUTER_MODEL_EXTRACT_RETRY})
    print(f"{'model':40} {'tier':12} {'result':10} {'secs':>5}  note")
    for m in models:
        for tier in TIERS:
            t0 = time.monotonic()
            try:
                r = call_structured(m, "Extract the fields. Quote verbatim.", _TEXT,
                                    _Probe, tier=tier, max_tokens=300)
                ok = r.parsed.reserve_price_quote and "57.34" in r.parsed.reserve_price_quote
                note = f"repaired={r.repaired} unit={r.parsed.unit!r}"
                res = "ok" if ok else "wrong"
            except Truncated as e:
                res, note = "truncated", str(e)[:60]
            except Unparseable as e:
                res, note = "unparseable", str(e)[:60]
            except Exception as e:  # noqa: BLE001 - the probe reports, never dies
                res, note = "error", f"{type(e).__name__}: {str(e)[:60]}"
            print(f"{m:40} {tier:12} {res:10} {time.monotonic() - t0:5.1f}  {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
