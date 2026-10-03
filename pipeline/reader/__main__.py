"""python -m pipeline.reader <markdown.txt> [expected_lot_count]: one live read."""
from __future__ import annotations

import json
import sys

from pipeline.reader import read_notice


def main() -> int:
    text = open(sys.argv[1], encoding="utf-8").read()
    exp = int(sys.argv[2]) if len(sys.argv) > 2 else None
    r = read_notice(text, expected_lot_count=exp)
    lots = {(e["attrs"] or {}).get("lot_index") for e in r.entities} - {None}
    ung = sum(1 for e in r.entities if e.get("start") is None)
    print(f"{len(lots)} lot(s), {len(r.entities)} entities, {ung} ungrounded, "
          f"{len(r.dropped)} dropped, strategy={r.segmentation.get('strategy')}")
    print(json.dumps(r.telemetry, indent=1)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
