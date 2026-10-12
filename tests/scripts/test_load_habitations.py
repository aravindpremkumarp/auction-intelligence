"""scripts/load_habitations: TNRD hamlets flattened to a lookup file."""
from pipeline.place_resolution import habitation_rows
from scripts.load_habitations import build


def _row(gp, code, name):
    return {"gp_code": gp, "code": code, "name": name}


def test_hamlets_group_by_panchayat_code_without_duplicates_or_blanks():
    out = build([_row("226332", "1", "Alagapuram"), _row("226332", "2", "Colony"),
                 _row("226332", "2", "Colony"), _row("226333", "1", "")])
    assert out == {"226332": ["Alagapuram", "Colony"]}


def test_hamlets_join_the_panchayats_villages_on_the_code():
    rows = habitation_rows({"226332": ["Colony"]},
                           [("226332", "Andimadam", "Kuvagam"),
                            ("226332", "Andimadam", "Vallam"),
                            ("999999", "Andimadam", "Elsewhere")])
    assert rows == [("Colony", "226332", "Andimadam", "Kuvagam"),
                    ("Colony", "226332", "Andimadam", "Vallam")]


def test_the_converter_writes_no_graph_nodes():
    import inspect

    import scripts.load_habitations as m
    assert "MERGE" not in inspect.getsource(m) and "nq(" not in inspect.getsource(m)
