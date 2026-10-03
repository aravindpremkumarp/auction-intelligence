"""HTML tables in the notice text as rows × columns with char ranges.

Datalab keeps a table's HTML verbatim in the markdown (pipeline/datalab.py),
so a multi-lot notice usually arrives as ``<table><tr><td>1</td><td>Mr X
...</td><td>Rs.50,00,000</td>...``. This module turns that into cells that
know their row, their column and where they sit in the text, so that:

* segmentation can cut one lot per row (pipeline/reader/segment.py);
* a money value can be required to sit in ITS row and ITS column — lot 2's
  EMD cannot be read as lot 1's, even though both are on the page;
* a unit declared once in a header ("Reserve Price (In Lakhs)") reaches the
  value below it.

Only well-formed tables are claimed: a row layout is offered for a table
whose header names a money column and whose data rows each carry a figure in
it. Anything else (a flattened table, labels and values on separate lines,
merged cells) is left to the serial / price / whole strategies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_MONEY = re.compile(r"(?:rs\.?|₹|inr)\s*\.?\s*\d|\d[\d,]{4,}", re.I)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# Column roles from header text. Order matters: "Earnest Money Deposit" must
# not be caught by a bare "deposit"; "Reserve Price" before "price".
_COLUMN_ROLES: tuple[tuple[str, re.Pattern], ...] = (
    ("serial", re.compile(r"^\s*(?:s\.?\s*(?:r|l)?\.?\s*no|sr\.?\s*no|sl\.?\s*no|lot\s*no|item)\b", re.I)),
    ("emd", re.compile(r"\bemd\b|earnest\s*money", re.I)),
    ("reserve_price", re.compile(r"reserve|upset", re.I)),
    ("bid_increment", re.compile(r"increment", re.I)),
    ("outstanding", re.compile(r"outstanding|dues|amount\s+due|demand", re.I)),
    ("borrower", re.compile(r"borrower|guarantor|mortgagor|name\s+of\s+the", re.I)),
    ("possession", re.compile(r"possession", re.I)),
    ("date", re.compile(r"\bdate\b|\btime\b|auction\s+on", re.I)),
    ("extent", re.compile(r"extent|\barea\b|measur", re.I)),
    ("description", re.compile(r"descri|property|schedule|asset", re.I)),
)
_UNIT_IN_HEADER = re.compile(r"\(\s*(?:rs\.?\s*)?(?:in\s+)?(lakh|lakhs|lac|lacs|crore|crores|cr)\b", re.I)


@dataclass(frozen=True)
class Cell:
    start: int          # absolute offsets of the cell's CONTENT in the notice
    end: int
    text: str
    row: int
    col: int
    header: bool = False


@dataclass
class Row:
    index: int
    start: int
    end: int
    cells: list[Cell] = field(default_factory=list)

    def cell(self, col: int) -> Cell | None:
        return next((c for c in self.cells if c.col == col), None)


@dataclass
class Table:
    start: int
    end: int
    header: list[Cell] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)      # data rows only

    @property
    def columns(self) -> dict[int, tuple[str, str | None]]:
        """{col: (role, unit)} read off the header cells."""
        return column_roles(self.header)

    def column(self, role: str) -> int | None:
        return next((c for c, (r, _) in self.columns.items() if r == role), None)


class _Parser(HTMLParser):
    def __init__(self, md: str) -> None:
        super().__init__(convert_charrefs=False)
        self.md = md
        self._line_starts = [0]
        for i, ch in enumerate(md):
            if ch == "\n":
                self._line_starts.append(i + 1)
        self.tables: list[Table] = []
        self._table: Table | None = None
        self._row: Row | None = None
        self._row_i = 0
        self._col = 0
        self._cell_start: int | None = None
        self._cell_is_th = False
        self._cell_span = 1

    def _off(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs) -> None:
        if tag == "table":
            self._table = Table(self._off(), self._off())
            self._row_i = 0
        elif tag == "tr" and self._table is not None:
            self._row = Row(self._row_i, self._off(), self._off())
            self._col = 0
        elif tag in ("td", "th") and self._row is not None:
            self._cell_start = self._off() + len(self.get_starttag_text() or "")
            self._cell_is_th = tag == "th"
            span = dict(attrs).get("colspan")
            try:
                self._cell_span = max(1, int(span)) if span else 1
            except ValueError:
                self._cell_span = 1

    def handle_endtag(self, tag) -> None:
        if tag in ("td", "th") and self._row is not None and self._cell_start is not None:
            end = self._off()
            text = self.md[self._cell_start:end]
            self._row.cells.append(Cell(self._cell_start, end, text, self._row.index,
                                        self._col, self._cell_is_th))
            self._col += self._cell_span
            self._cell_start = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._row.end = self._off() + len("</tr>")
            self._table.rows.append(self._row)
            self._row = None
            self._row_i += 1
        elif tag == "table" and self._table is not None:
            self._table.end = self._off() + len("</table>")
            _split_header(self._table)
            self.tables.append(self._table)
            self._table = None


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text)


def _split_header(t: Table) -> None:
    """The first row is the header when it is all <th>, or carries no figure
    while the next row does."""
    if not t.rows:
        return
    first = t.rows[0]
    if first.cells and (all(c.header for c in first.cells)
                        or (not any(_MONEY.search(_plain(c.text)) for c in first.cells)
                            and len(t.rows) > 1
                            and any(_MONEY.search(_plain(c.text)) for c in t.rows[1].cells))):
        t.header = [Cell(c.start, c.end, c.text, c.row, c.col, True) for c in first.cells]
        t.rows = t.rows[1:]


def parse_tables(md: str) -> list[Table]:
    """Every ``<table>`` in ``md`` with its header and data rows. Malformed
    HTML yields whatever rows closed properly; nothing raises."""
    if not md or "<table" not in md.lower():
        return []
    p = _Parser(md)
    try:
        p.feed(md)
        p.close()
    except Exception:  # noqa: BLE001 - never let a bad tag break a read
        pass
    return p.tables


def column_roles(header: list[Cell]) -> dict[int, tuple[str, str | None]]:
    """{col: (role, unit)} for header cells whose text names a known column."""
    out: dict[int, tuple[str, str | None]] = {}
    for c in header:
        text = _plain(c.text)
        role = next((r for r, rx in _COLUMN_ROLES if rx.search(text)), None)
        if role is None:
            continue
        unit = None
        m = _UNIT_IN_HEADER.search(text)
        if m:
            w = m.group(1).lower()
            unit = "crore" if w.startswith("cr") else "lakh"
        out[c.col] = (role, unit)
    return out


def lot_rows(t: Table) -> list[Row] | None:
    """The data rows of ``t`` when each is plausibly one lot: the header
    names a reserve-price (or EMD) column and every row carries a figure in
    it. None when the table is not that shape."""
    col = t.column("reserve_price")
    if col is None:
        col = t.column("emd")
    if col is None or not t.rows:
        return None
    unit = t.columns[col][1]
    rows = []
    for r in t.rows:
        c = r.cell(col)
        if c is None:
            return None
        cell_text = _plain(c.text)
        # "57.34" under "Reserve Price (In Lakhs)" is money; bare "57.34"
        # under an unlabelled header is not.
        if not (_MONEY.search(cell_text) or (unit and _NUMBER.search(cell_text))):
            return None
        rows.append(r)
    return rows


def cell_for(t: Table, row: Row, role: str) -> Cell | None:
    col = t.column(role)
    return row.cell(col) if col is not None else None


__all__ = ["Cell", "Row", "Table", "parse_tables", "column_roles", "lot_rows",
           "cell_for"]
