"""The orchestrator's stage ORDER is a correctness property, not a detail.

Every stage is stubbed; only the sequence of calls and the arguments the
orchestrator threads through are asserted.
"""
from __future__ import annotations

import sys
import types

import pytest


def _run(monkeypatch, argv):
    """Run pipeline.run_pipeline.main() with every stage replaced by a
    recorder. Returns the ordered list of (stage, kwargs) calls."""
    calls: list[tuple[str, dict]] = []

    def rec(name):
        def f(*a, **kw):
            calls.append((name, kw))
            return 0
        return f

    # Stage modules are imported lazily inside main(), so install fakes into
    # sys.modules before it runs. Each fake exposes exactly the attribute the
    # orchestrator imports from it.
    def fake(modname, **attrs):
        m = types.ModuleType(modname)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, modname, m)

    fake("pipeline.classify_notice", run=rec("classify"))
    fake("pipeline.promote_extractions", run=rec("promote"))
    fake("pipeline.apply_extractions", run=rec("apply"))
    fake("scripts.link_reauctions", run=rec("link_reauctions"),
         run_events=rec("link_reauction_events"))
    fake("scripts.link_listings", run=rec("link_listings"))
    fake("scripts.build_spine", run=rec("build_spine"))
    fake("api.tools.cypher_tools", describe_schema=rec("schema_cache"))
    import pipeline.run_pipeline as RP

    monkeypatch.setattr(sys, "argv", ["run_pipeline"] + argv)
    RP.main()
    return calls


def _order(calls):
    return [name for name, _ in calls]


def test_entities_are_promoted_into_the_graph_before_they_are_applied(monkeypatch):
    """apply_extractions' area comparer reads each lot's headline extent off
    the graph, so promote must have written it first."""
    order = _order(_run(monkeypatch, []))
    assert order == ["classify", "promote", "apply", "link_reauctions",
                     "link_listings", "build_spine", "link_reauction_events", "schema_cache"]


def test_the_spine_is_built_after_the_bridge_and_before_the_event_chain(monkeypatch):
    """link_listings writes the SAME_LISTING_AS edges build_spine clusters
    on; the event chain needs the events to exist. Any other order links
    nothing, silently."""
    order = _order(_run(monkeypatch, []))
    assert order.index("link_reauctions") < order.index("link_listings") < order.index("build_spine") < order.index("link_reauction_events")


def test_a_limited_run_skips_the_whole_corpus_parcel_phase(monkeypatch):
    """Parcels group lots across the entire corpus; on a partial promotion the
    grouping would be wrong rather than merely incomplete."""
    calls = dict(_run(monkeypatch, ["--limit", "5"]))
    assert calls["promote"]["limit"] == 5
    assert calls["promote"]["skip_parcels"] is True
    assert calls["apply"]["limit"] == 5


def test_a_full_run_builds_parcels(monkeypatch):
    calls = dict(_run(monkeypatch, []))
    assert calls["promote"]["limit"] is None
    assert calls["promote"]["skip_parcels"] is False
    assert calls["promote"]["dry_run"] is False


def test_skip_classify_drops_only_the_classification_stage(monkeypatch):
    order = _order(_run(monkeypatch, ["--skip-classify"]))
    assert "classify" not in order
    assert order.index("promote") < order.index("apply")


@pytest.mark.parametrize("flag", ["--skip-ocr", "--verify-only", "--legacy",
                                  "--skip-descriptions"])
def test_retired_path_a_flags_are_rejected(monkeypatch, flag):
    """The flat vision-LLM blob path (Stage 1 / 1.5 / 4 and the lexical
    legacy chain) is gone; its flags must fail loudly, not silently no-op."""
    with pytest.raises(SystemExit):
        _run(monkeypatch, [flag])
