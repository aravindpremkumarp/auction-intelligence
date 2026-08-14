"""A/B: production LangExtract vs the ContextGem two-stage workflow, on multi-lot gold.

Both engines are graded by the SAME scorer (evals.langextract_eval.score_records), so
the only thing that varies is how the extraction is orchestrated:

  LangExtract  — one prompt (canonical scheme + 7 few-shot examples) over the whole
                 notice, window sized by pipeline.extract_routing.char_buffer_for,
                 LANGEXTRACT_PASSES passes. Model from select_extract_model().
  ContextGem   — segment lots (cheap model), then extract each lot's fields from a
                 document containing only that lot (production multi model), plus one
                 notice-level call. See evals/contextgem_pipeline.py.

Neither engine is told the gold lot count: finding it is the thing being measured.
(Production CAN inject a reviewer-confirmed count via expected_lot_count; handing it
over would erase the metric, so this run leaves it None for both.)

Reported per notice: field accuracy, extracted-vs-gold lot count, LLM calls, tokens
and wall-clock. Tokens are the cost proxy — the two models are priced differently and
neither is in litellm's price map, so this prints usage rather than inventing a dollar
figure.

Run:  python -m evals.contextgem_eval            (needs OPENROUTER_API_KEY)
      python -m evals.contextgem_eval 750348     (a subset of auction ids)
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field

os.environ.setdefault("CONTEXTGEM_DISABLE_LOGGER", "True")

from evals.langextract_eval import FIX, load_gold, score_records

_ACTIVE: "Usage | None" = None
_PATCHED = False


@dataclass
class Usage:
    """LLM usage for one engine, accumulated across notices."""
    name: str
    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    per_notice: dict = field(default_factory=dict)

    def add(self, prompt: int, cached: int, completion: int) -> None:
        self.calls += 1
        self.cached_tokens += cached
        self.input_tokens += max(prompt - cached, 0)
        self.output_tokens += completion

    def snapshot(self) -> tuple:
        return (self.calls, self.input_tokens, self.cached_tokens, self.output_tokens)

    def since(self, snap: tuple) -> tuple:
        return tuple(now - was for now, was in zip(self.snapshot(), snap))


def _record(usage_obj) -> None:
    """Attribute one API response's usage to whichever engine is running."""
    if _ACTIVE is None or usage_obj is None:
        return
    get = (usage_obj.get if isinstance(usage_obj, dict)
           else lambda k, d=0: getattr(usage_obj, k, d) or d)
    details = get("prompt_tokens_details", None)
    cached = 0
    if details is not None:
        cached = (details.get("cached_tokens", 0) if isinstance(details, dict)
                  else getattr(details, "cached_tokens", 0)) or 0
    _ACTIVE.add(get("prompt_tokens", 0) or 0, cached, get("completion_tokens", 0) or 0)


def install_tracking() -> None:
    """Patch both client paths once: openai SDK (LangExtract) and litellm (ContextGem)."""
    global _PATCHED
    if _PATCHED:
        return
    from openai.resources.chat import completions as _oai
    _orig_oai = _oai.Completions.create

    def _patched_oai(self, *a, **k):
        resp = _orig_oai(self, *a, **k)
        _record(getattr(resp, "usage", None))
        return resp

    _oai.Completions.create = _patched_oai

    import litellm
    _orig_lite = litellm.acompletion

    async def _patched_lite(*a, **k):
        resp = await _orig_lite(*a, **k)
        _record(getattr(resp, "usage", None))
        return resp

    litellm.acompletion = _patched_lite
    _PATCHED = True


def run_langextract(md: str, notice_type: str | None) -> list[dict]:
    from evals.langextract_eval import _records
    from pipeline import langextract_examples as LX
    from pipeline.extract_routing import select_extract_model
    model_id, reasoning_off = select_extract_model(notice_type)
    res = None
    for _ in range(3):  # same transient-empty retry the production eval uses
        res = LX.extract(md, model_id=model_id, reasoning_off=reasoning_off)
        if res.extractions:
            break
    return _records(res)


def run_contextgem(md: str, group) -> list[dict]:
    from evals.contextgem_pipeline import assemble_lots, to_records, _extract_async
    import asyncio
    _, segments, notice = asyncio.run(_extract_async(md, group))
    return to_records(assemble_lots(segments), notice)


def _score(g: dict, records: list[dict]) -> tuple[int, int, tuple]:
    rows, lot_stats = score_records(g, records)
    return sum(1 for *_, ok in rows if ok), len(rows), (rows, lot_stats)


def main(argv: list[str]) -> int:
    global _ACTIVE
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY not set", file=sys.stderr)
        return 2
    install_tracking()

    wanted = set(argv)
    gold = [g for g in load_gold() if g.get("lots")]
    if wanted:
        gold = [g for g in gold if g["aid"] in wanted]
    if not gold:
        print("no multi-lot gold entries selected", file=sys.stderr)
        return 2

    from evals.contextgem_pipeline import build_llm_group
    group = build_llm_group()
    lx_usage, cg_usage = Usage("langextract"), Usage("contextgem")
    totals = {"langextract": [0, 0], "contextgem": [0, 0]}
    lot_ok = {"langextract": 0, "contextgem": 0}
    misses: list[tuple] = []

    print(f"multi-lot A/B — {len(gold)} notices, "
          f"passes={os.environ.get('LANGEXTRACT_PASSES', '2')}\n")
    header = f"{'aid':8} {'engine':12} {'fields':>9}  {'lots':>7}  {'calls':>5} {'tok in':>9} {'tok out':>8} {'sec':>6}"
    print(header)
    print("-" * len(header))

    for g in gold:
        md = (FIX / f"{g['aid']}.txt").read_text(encoding="utf-8")
        for name, usage, runner in (
            ("langextract", lx_usage, lambda: run_langextract(md, g.get("notice_type"))),
            ("contextgem", cg_usage, lambda: run_contextgem(md, group)),
        ):
            _ACTIVE = usage
            snap, t0 = usage.snapshot(), time.time()
            try:
                records = runner()
            except Exception as e:  # keep the other engine's numbers usable
                print(f"{g['aid']:8} {name:12} FAILED: {type(e).__name__}: {e}")
                continue
            finally:
                _ACTIVE = None
            elapsed = time.time() - t0
            usage.seconds += elapsed
            correct, total, (rows, lot_stats) = _score(g, records)
            totals[name][0] += correct
            totals[name][1] += total
            n_gold, n_got, count_ok = lot_stats
            lot_ok[name] += bool(count_ok)
            calls, tin, _cached, tout = usage.since(snap)
            print(f"{g['aid']:8} {name:12} {correct:4}/{total:<4} "
                  f"{n_got:3}/{n_gold:<3}{'' if count_ok else '⚠'} "
                  f"{calls:5} {tin:9,} {tout:8,} {elapsed:6.1f}")
            misses += [(g["aid"], name, k, gd, got) for k, gd, got, ok in rows if not ok]

    print()
    for name, usage in (("langextract", lx_usage), ("contextgem", cg_usage)):
        c, t = totals[name]
        acc = f"{c / t * 100:.1f}%" if t else "n/a"
        print(f"{name:12} accuracy {c:4}/{t:<4} = {acc:>6}   "
              f"lot-count {lot_ok[name]}/{len(gold)}   "
              f"calls={usage.calls} in={usage.input_tokens:,} "
              f"(+{usage.cached_tokens:,} cached) out={usage.output_tokens:,} "
              f"{usage.seconds:.0f}s")

    if misses:
        print("\nMISSES (aid  engine  field  gold -> got):")
        for aid, name, key, gd, got in misses:
            print(f"  {aid}  {name:12} {key:26} {gd!r:22} -> {got!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
