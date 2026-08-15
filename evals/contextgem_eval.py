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

REPEATS, and why they are not optional
--------------------------------------
Both engines are noisy on the same input. Two runs of the identical LangExtract config
(deepseek-v4-pro, 2 passes) over notice 750348 returned 141 and 90 entities and scored
48% and 88% — the difference between "rewrite the pipeline" and "leave it alone" is
inside one engine's run-to-run spread. So every notice is run REPEATS times per engine
and the summary reports the spread, not a single number. Treat any comparison run with
--repeats 1 as an anecdote.

Run:  python -m evals.contextgem_eval                    (needs OPENROUTER_API_KEY)
      python -m evals.contextgem_eval 750348             (a subset of auction ids)
      python -m evals.contextgem_eval --repeats 5        (tighter spread, 5x the cost)
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


def _pct(runs: list[float]) -> str:
    """mean, with the spread when repeats disagree — the spread is the point."""
    if not runs:
        return "n/a"
    mean = sum(runs) / len(runs)
    if len(runs) == 1:
        return f"{mean * 100:.1f}%"
    return f"{mean * 100:.1f}% [{min(runs) * 100:.0f}-{max(runs) * 100:.0f}]"


def _parse_args(argv: list[str]) -> tuple[set[str], int]:
    aids, repeats = set(), 3
    it = iter(argv)
    for arg in it:
        if arg == "--repeats":
            repeats = int(next(it))
        elif arg.startswith("--repeats="):
            repeats = int(arg.split("=", 1)[1])
        else:
            aids.add(arg)
    return aids, max(1, repeats)


def main(argv: list[str]) -> int:
    global _ACTIVE
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY not set", file=sys.stderr)
        return 2
    install_tracking()

    wanted, repeats = _parse_args(argv)
    gold = [g for g in load_gold() if g.get("lots")]
    if wanted:
        gold = [g for g in gold if g["aid"] in wanted]
    if not gold:
        print("no multi-lot gold entries selected", file=sys.stderr)
        return 2

    from evals.contextgem_pipeline import build_llm_group
    group = build_llm_group()
    usages = {"langextract": Usage("langextract"), "contextgem": Usage("contextgem")}
    # engine -> list of per-run accuracy fractions / lot-count hits, across all notices
    accs: dict[str, list[float]] = {k: [] for k in usages}
    lot_hits: dict[str, list[float]] = {k: [] for k in usages}
    misses: list[tuple] = []

    print(f"multi-lot A/B — {len(gold)} notices x {repeats} run(s) per engine, "
          f"passes={os.environ.get('LANGEXTRACT_PASSES', '2')}\n", flush=True)
    header = (f"{'aid':8} {'engine':12} {'run':>3} {'fields':>9}  {'lots':>7}  "
              f"{'calls':>5} {'tok in':>9} {'tok out':>8} {'sec':>6}")
    print(header)
    print("-" * len(header), flush=True)

    for g in gold:
        md = (FIX / f"{g['aid']}.txt").read_text(encoding="utf-8")
        for name, runner in (
            ("langextract", lambda: run_langextract(md, g.get("notice_type"))),
            ("contextgem", lambda: run_contextgem(md, group)),
        ):
            usage = usages[name]
            for run_i in range(1, repeats + 1):
                _ACTIVE = usage
                snap, t0 = usage.snapshot(), time.time()
                try:
                    records = runner()
                except Exception as e:  # keep every other cell usable
                    print(f"{g['aid']:8} {name:12} {run_i:3} FAILED: "
                          f"{type(e).__name__}: {e}", flush=True)
                    continue
                finally:
                    _ACTIVE = None
                elapsed = time.time() - t0
                usage.seconds += elapsed
                correct, total, (rows, lot_stats) = _score(g, records)
                n_gold, n_got, count_ok = lot_stats
                accs[name].append(correct / total if total else 0.0)
                lot_hits[name].append(1.0 if count_ok else 0.0)
                calls, tin, _cached, tout = usage.since(snap)
                print(f"{g['aid']:8} {name:12} {run_i:3} {correct:4}/{total:<4} "
                      f"{n_got:3}/{n_gold:<3}{'' if count_ok else '!'} "
                      f"{calls:5} {tin:9,} {tout:8,} {elapsed:6.1f}", flush=True)
                misses += [(g["aid"], name, run_i, k, gd, got)
                           for k, gd, got, ok in rows if not ok]

    print()
    for name, usage in usages.items():
        runs = accs[name]
        n = len(runs) or 1
        print(f"{name:12} accuracy {_pct(runs):>18}   "
              f"lot-count {_pct(lot_hits[name]):>18}   "
              f"per run: calls={usage.calls / n:.0f} "
              f"in={usage.input_tokens / n:,.0f} "
              f"(+{usage.cached_tokens / n:,.0f} cached) "
              f"out={usage.output_tokens / n:,.0f} {usage.seconds / n:.0f}s")

    if misses:
        print("\nMISSES (aid  engine  run  field  gold -> got):")
        for aid, name, run_i, key, gd, got in misses:
            print(f"  {aid}  {name:12} {run_i}  {key:26} {gd!r:22} -> {got!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
