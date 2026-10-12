"""scripts/load_habitations: joining TNRD hamlets to held panchayats."""
from scripts.load_habitations import _WRITE, plan


def _row(gp, code, name):
    return {"gp_code": gp, "code": code, "name": name}


def test_rows_join_on_the_panchayat_code_and_unknown_codes_are_skipped():
    todo, stats = plan([_row("226332", "1", "Alagapuram"), _row("999999", "1", "X")],
                       {"226332"})
    assert [r["name"] for r in todo] == ["Alagapuram"]
    assert stats["panchayat not in graph"] == 1


def test_duplicates_and_blank_names_are_dropped():
    todo, stats = plan([_row("1", "1", "A"), _row("1", "1", "A"), _row("1", "2", "")], {"1"})
    assert len(todo) == 1 and stats["duplicate row"] == 1 and stats["blank row"] == 1


def test_the_writer_never_creates_a_panchayat():
    assert "MATCH (g:GramPanchayat {code: row.gp_code})" in _WRITE
    assert "MERGE (g:GramPanchayat" not in _WRITE
