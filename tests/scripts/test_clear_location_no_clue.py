import json

from scripts.clear_location_no_clue import plan_doc


def test_only_automatic_no_clue_location_marks_are_cleared():
    corr = {
        "absent:1:location": {"by": "auto", "rule": "no_clue"},
        "absent:12:location": {"by": "auto", "rule": "no_clue"},
        "absent:2:location": {"by": "auto", "rule": "not_found"},
        "absent:3:location": {"by": "reviewer@x"},
        "absent:1:extent": {"by": "auto", "rule": "no_clue"},
        "unfound:4:location": {"by": "auto", "rule": "no_clue"},
    }
    kept, cleared = plan_doc(json.dumps(corr))
    assert cleared == ["1", "12"]
    assert set(kept) == set(corr) - {"absent:1:location", "absent:12:location"}


def test_bad_json_clears_nothing():
    assert plan_doc("not json") == ({}, [])
    assert plan_doc(None) == ({}, [])
