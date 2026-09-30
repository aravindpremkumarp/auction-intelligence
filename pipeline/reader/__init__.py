"""Reader v2: a lot-first, schema-locked, code-grounded read of a sale notice.

Why this exists
---------------
The first-generation reader (pipeline/langextract_examples) asks one model
call to read a whole notice and copy long verbatim spans. It is unstable from
run to run, composes text that is not on the page, and loses lots on long
notices; a repair layer (gap_fill, absence, lot_windows, reground, ...) grew
around it to patch the output afterwards. This package moves every one of
those guarantees INTO the read:

  segment.py     lots are cut by code (table rows, serials, prices); a model is
                 asked only for boundary anchor phrases, and only as a fallback
  schema.py      the model fills a fixed form (Pydantic) with verbatim quotes;
                 it may answer ``not_stated`` and is told never to infer
  llm.py         structured-output client: temperature 0, seed, max_tokens,
                 finish_reason checked, three tiers of schema enforcement
  ground.py      every quote is located by code inside its own lot's text;
                 what cannot be located is dropped, never stored
  tables.py      HTML tables become rows × columns with char ranges, so a
                 money value must sit in its own row and column
  normalize.py   money / date / area / possession parsed by code; mangled
                 digits become ILLEGIBLE, never a guessed number
  provenance.py  char offset -> page + block, from Document.blocks
  migrate.py     forward migrations of stored entities across schema versions

Later PRs add consistency.py (cross-field rules), stability.py (targeted
re-read + key-facts vote), convert.py (-> extraction_json) and
``read_notice`` here. Until then ``pipeline.reader.read_notice`` does not
exist and the eval's ``--reader v2`` says so.
"""
from __future__ import annotations

from pipeline.reader.schema import SCHEMA_VERSION  # noqa: F401

READER_VERSION = "v2"
