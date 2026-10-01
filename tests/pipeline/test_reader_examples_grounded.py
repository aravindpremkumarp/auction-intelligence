"""Every quote and anchor in the v2 few-shots is a verbatim substring of its
text, and the examples validate against the schema."""
from __future__ import annotations

from pipeline.reader.examples import EXAMPLES
from pipeline.reader.schema import NoticeRead, SegmentRead

_QUOTE_KEYS = ("quote", "name_quote", "address_quote", "adjacency_quote", "measurement_quote",
               "extent_quote", "value_quote", "bank_name_quote", "sale_terms_quote",
               "first_words", "last_words", "title_deed_holder_quote", "encumbrance_quote")


def _walk(obj, path="root"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def test_quotes_are_substrings_of_their_text():
    for text, seg, notice in EXAMPLES:
        for path, v in list(_walk(seg)) + list(_walk(notice)):
            key = path.rsplit(".", 1)[-1]
            if key in _QUOTE_KEYS and v:
                assert v in text, f"{path}: {v!r} not in example text"


def test_examples_validate_and_teach_not_stated_and_no_inference():
    for text, seg, notice in EXAMPLES:
        SegmentRead.model_validate(seg)
        NoticeRead.model_validate(notice)
    _, serial, _ = EXAMPLES[1]
    lot2 = serial["lots"][1]
    assert lot2["property_type"]["status"] == "not_stated"
    assert lot2["location"]["district"] is None            # only the Regd.Dist is named
    assert lot2["location"]["registration_district"] == "Virudhunagar"
