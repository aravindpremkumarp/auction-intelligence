"""
tests/api/test_neo4j_http_params.py
-----------------------------------
The HTTPS Query API path (NEO4J_HTTP_API=1) must be able to send temporal
Cypher parameters. Plain-JSON encoding cannot: `json.dumps` raises on a
datetime, which crashed every dated `search_auctions` call in HTTP mode.

These tests pin the two rules the Query API enforces:
  * typed encoding is all-or-nothing — one temporal forces every parameter
    into the typed form, under the vnd.neo4j.query content type;
  * Boolean carries a real JSON bool (bool subclasses int, so branch order
    in `_encode_param` matters).
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

# tests/api/conftest.py replaces `api.neo4j_client` with an in-memory fake so
# api.main imports without live credentials. This module tests the REAL HTTP
# encoder, so load it straight from source under a private name rather than
# unwinding the shared stub for everyone else.
_REAL_PATH = Path(__file__).resolve().parents[2] / "api" / "neo4j_client.py"
_spec = importlib.util.spec_from_file_location("_real_neo4j_client", _REAL_PATH)
nc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nc)


def test_encode_bool_before_int():
    assert nc._encode_param(True) == {"$type": "Boolean", "_value": True}
    assert nc._encode_param(5) == {"$type": "Integer", "_value": "5"}


def test_encode_temporals():
    aware = datetime(2026, 8, 20, 9, 30, tzinfo=timezone.utc)
    assert nc._encode_param(aware)["$type"] == "OffsetDateTime"
    assert nc._encode_param(datetime(2026, 8, 20))["$type"] == "LocalDateTime"
    assert nc._encode_param(date(2026, 8, 20)) == {
        "$type": "Date", "_value": "2026-08-20"}
    assert nc._encode_param(time(9, 30))["$type"] == "LocalTime"


def test_encode_nested():
    out = nc._encode_param({"ids": ["1", "2"]})
    assert out["$type"] == "Map"
    assert out["_value"]["ids"]["$type"] == "List"
    assert out["_value"]["ids"]["_value"][0] == {"$type": "String", "_value": "1"}


def test_encode_rejects_unknown_type():
    with pytest.raises(TypeError):
        nc._encode_param(object())


@pytest.mark.parametrize("params,expected", [
    ({"city": "Chennai", "limit": 20}, False),
    ({"floor": datetime.now(timezone.utc)}, True),
    ({"window": [datetime.now(timezone.utc)]}, True),
    ({"nested": {"floor": date(2026, 1, 1)}}, True),
    ({}, False),
])
def test_needs_typed_params(params, expected):
    assert nc._needs_typed_params(params) is expected


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch):
    """Patch urlopen and return the list requests land in."""
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _FakeResponse({"data": {"fields": ["n"], "values": [[1]]}})

    monkeypatch.setattr(nc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(nc, "NEO4J_USERNAME", "u")
    monkeypatch.setattr(nc, "NEO4J_PASSWORD", "p")
    monkeypatch.setattr(nc, "_http_query_url", lambda: "https://x/db/neo4j/query/v2")
    return seen


def test_plain_params_keep_json_content_type(monkeypatch):
    seen = _capture(monkeypatch)
    rows = nc._http_run("RETURN 1 AS n", {"city": "Chennai"}, "READ", 5.0)

    assert rows == [{"n": 1}]
    req = seen[0]
    assert req.headers["Content-type"] == "application/json"
    assert json.loads(req.data)["parameters"] == {"city": "Chennai"}


def test_one_datetime_types_every_param(monkeypatch):
    seen = _capture(monkeypatch)
    floor = datetime(2026, 8, 20, tzinfo=timezone.utc)

    nc._http_run(
        "RETURN 1 AS n",
        {"floor": floor, "city": "Chennai", "is_reauction": True, "limit": 20},
        "READ",
        5.0,
    )

    req = seen[0]
    assert req.headers["Content-type"] == "application/vnd.neo4j.query"
    params = json.loads(req.data)["parameters"]
    # All-or-nothing: every parameter carries a type, not just the temporal.
    assert params == {
        "floor": {"$type": "OffsetDateTime", "_value": "2026-08-20T00:00:00+00:00"},
        "city": {"$type": "String", "_value": "Chennai"},
        "is_reauction": {"$type": "Boolean", "_value": True},
        "limit": {"$type": "Integer", "_value": "20"},
    }


def test_http_errors_surface(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeResponse({"errors": [{"code": "Neo.X", "message": "boom"}]})

    monkeypatch.setattr(nc.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(nc, "_http_query_url", lambda: "https://x/db/neo4j/query/v2")
    monkeypatch.setattr(nc, "NEO4J_USERNAME", "u")
    monkeypatch.setattr(nc, "NEO4J_PASSWORD", "p")

    with pytest.raises(RuntimeError, match="Neo.X"):
        nc._http_run("RETURN 1", {}, "READ", 5.0)
