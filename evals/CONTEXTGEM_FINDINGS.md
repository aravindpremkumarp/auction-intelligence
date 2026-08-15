# ContextGem two-stage extraction — A/B result

**Verdict: do not adopt. Keep LangExtract.**

Measured with `evals/contextgem_eval.py`, 3 runs per engine per notice, both engines
graded by the same scorer (`evals.langextract_eval.score_records`). Neither engine was
told the gold lot count.

## Numbers

| notice | lots | LangExtract | ContextGem |
| --- | --- | --- | --- |
| 749433 | 1 | 10/10, 10/10, 10/10 | 10/10, 10/10, 10/10 |
| 750348 | 6 | 32/33, 32/33, 31/33 | 32/33, 32/33, 31/33 |
| 753006 | 5 | 30/30, 30/30, 29/30 | 21/30, 21/30, 21/30 |

Per run: LangExtract 2 calls / ~378s; ContextGem 26 calls / ~289s (~1.3x faster
wall-clock, ~13x the calls, ~12x the billed input tokens).

## Why it was tried, and why that reason did not hold

`pipeline/extract_routing.char_buffer_for` documents the target failure: LangExtract
extracts each window independently, so a long multi-lot notice loses its global lot
numbering, and production compensates by inflating the window to 30k chars. The
two-stage design was meant to remove the need for that workaround by extracting each
lot from a document containing only that lot.

**Lot counts were correct in 100% of runs — both engines, every notice.** On this gold
set the problem being solved did not occur, so there was no headroom to win.

## Why ContextGem lost on 753006

Not noise: 21/30 three times, identically. The misses are an off-by-one shift.

```
lot2:emd  380000 -> [700000]    <- lot 1's EMD
lot3:emd  350000 -> [380000]    <- lot 2's EMD
lot4:emd  310000 -> [350000]    <- lot 3's EMD
```

MinerU flattens this notice into 71 short paragraphs (a table with labels and values on
separate lines), so stage 1 returns ~26 fragments rather than 5 lot blocks, and the
fragment order runs description -> RESERVE PRICE -> EMD -> property_id. `assemble_lots`
closes a lot on the reserve price, so every lot's EMD and property ID land in the
following lot.

That rule could be patched, but the shape of the result is the point: **per-lot
isolation does not remove lot-binding errors, it relocates them into the assembly
step, where they depend on document layout.** The single whole-notice prompt needs no
assembly rule at all, because the model sees the lot and its numbers together.

A second instance of the same weakness: a unit declared once in a table header
("Reserve Price (In Lakhs)") sits outside the fragment that needs it. An earlier
version of this prototype scored 14/7/4 on 753006 purely because its money rule
mishandled "Rs.70.00 Lakhs" — fixed in the prompt here, but the structural exposure
remains.

## What is worth keeping

- **Run-to-run variance is large and was previously unmeasured.** Two runs of the
  identical LangExtract config on 750348 returned 141 and 90 entities and scored 48%
  and 88%. Any future extraction A/B needs repeats; `--repeats` (default 3) exists for
  that, and the harness reports mean plus min-max spread.
- **A latent crash in `evals/langextract_eval.py`** — sorting borrowers on a raw
  `lot_index` raised `TypeError` on a mixed `"2"` / `2` list and aborted a whole eval
  run after the API spend. Fixed with a regression test.
- **Per-field justifications** (`add_justifications=True`) remain the one ContextGem
  feature with a clear use here: they would feed the extraction-review UI directly.
  That is available without adopting the workflow.

## Reproducing

```
pip install contextgem                      # prototype-only, not in requirements
python -m evals.contextgem_eval --repeats 3           # full multi-lot A/B
python -m evals.contextgem_eval 753006 --repeats 3    # the notice that fails
python -m evals.contextgem_pipeline evals/fixtures/753006.txt   # segments + lots
```
