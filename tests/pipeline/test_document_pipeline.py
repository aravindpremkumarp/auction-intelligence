"""Unit tests for pipeline/document_pipeline.py (plan resolution + flag coupling).

No subprocesses and no Neo4j: the orchestrator's whole job is deciding WHICH
stages run, in what order, with which flags — and that decision layer is pure.
The one place we do exercise execution (run_plan) stubs subprocess.run, because
what matters there is the manifest and the stop/continue policy, not the child.
"""
from __future__ import annotations

import json

import pytest

import pipeline.document_pipeline as D


def keys(stages):
    return [s.key for s in stages]


# ── plan resolution ──────────────────────────────────────────────────────────

def test_default_plan_is_every_stage_in_order():
    assert keys(D.resolve_plan()) == list(D.STAGE_KEYS)


def test_places_runs_before_promote():
    plan = keys(D.resolve_plan())
    assert plan.index("places") < plan.index("promote")


def test_extract_runs_after_the_markdown_it_reads():
    plan = keys(D.resolve_plan())
    assert plan.index("load-markdown") < plan.index("extract")


def test_from_starts_at_the_named_stage():
    assert keys(D.resolve_plan(from_="extract"))[0] == "extract"


def test_to_stops_after_the_named_stage():
    assert keys(D.resolve_plan(to="apply"))[-1] == "apply"


def test_from_and_to_window_the_plan():
    assert keys(D.resolve_plan(from_="extract", to="promote")) == [
        "extract", "apply", "places", "promote"]


def test_only_ignores_from_and_to():
    assert keys(D.resolve_plan(only="extract,promote", from_="ocr", to="apply")) == [
        "extract", "promote"]


def test_only_uses_pipeline_order_not_the_typed_order():
    # Typing promote first must not run promote before places — order is the
    # architecture, not a user preference.
    assert keys(D.resolve_plan(only="promote,places")) == ["places", "promote"]


def test_skip_subtracts_from_the_plan():
    assert "ocr" not in keys(D.resolve_plan(skip="ocr"))


def test_skip_applies_to_only_as_well():
    assert keys(D.resolve_plan(only="extract,promote", skip="promote")) == ["extract"]


def test_stage_lists_tolerate_spaces_and_trailing_commas():
    assert keys(D.resolve_plan(only=" extract , promote , ")) == ["extract", "promote"]


def test_unknown_stage_is_rejected_with_the_known_keys():
    with pytest.raises(ValueError) as exc:
        D.resolve_plan(only="nope")
    assert "nope" in str(exc.value)
    assert "extract" in str(exc.value)


def test_unknown_from_stage_is_rejected():
    with pytest.raises(ValueError):
        D.resolve_plan(from_="nope")


def test_to_before_from_is_rejected_rather_than_running_nothing():
    with pytest.raises(ValueError):
        D.resolve_plan(from_="promote", to="extract")


def test_skipping_everything_yields_an_empty_plan():
    assert D.resolve_plan(skip=",".join(D.STAGE_KEYS)) == []


# ── flag coupling ────────────────────────────────────────────────────────────

def test_limit_is_passed_to_stages_that_accept_it():
    argv = D.build_argv(D.BY_KEY["extract"], D.RunOptions(limit=50), python="py")
    assert argv[:3] == ["py", "-m", "pipeline.load_extractions"]
    assert "--limit" in argv and "50" in argv


def test_limit_is_dropped_for_corpus_wide_stages():
    # resolve_places has no --limit; passing one would abort the stage on an
    # unrecognized argument.
    argv = D.build_argv(D.BY_KEY["places"], D.RunOptions(limit=50), python="py")
    assert "--limit" not in argv


def test_filename_only_goes_to_stages_that_accept_it():
    opts = D.RunOptions(filename="notices/x/n.jpg")
    assert "--filename" in D.build_argv(D.BY_KEY["extract"], opts)
    assert "--filename" not in D.build_argv(D.BY_KEY["apply"], opts)


def test_force_is_dropped_where_unsupported():
    opts = D.RunOptions(force=True)
    assert "--force" in D.build_argv(D.BY_KEY["extract"], opts)
    assert "--force" not in D.build_argv(D.BY_KEY["promote"], opts)


def test_dry_run_is_dropped_where_unsupported():
    opts = D.RunOptions(dry_run=True)
    assert "--dry-run" in D.build_argv(D.BY_KEY["promote"], opts)
    assert "--dry-run" not in D.build_argv(D.BY_KEY["extract"], opts)


# The load-bearing coupling: phase C is a corpus-wide join.

def test_limited_run_skips_parcels():
    argv = D.build_argv(D.BY_KEY["promote"], D.RunOptions(limit=10))
    assert "--skip-parcels" in argv


def test_single_filename_run_skips_parcels():
    argv = D.build_argv(D.BY_KEY["promote"], D.RunOptions(filename="notices/x/n.jpg"))
    assert "--skip-parcels" in argv


def test_full_run_resolves_parcels():
    argv = D.build_argv(D.BY_KEY["promote"], D.RunOptions())
    assert "--skip-parcels" not in argv


def test_skip_parcels_does_not_leak_onto_other_stages():
    for key in D.STAGE_KEYS:
        if key == "promote":
            continue
        assert "--skip-parcels" not in D.build_argv(D.BY_KEY[key], D.RunOptions(limit=10))


def test_narrowed_is_false_for_a_whole_corpus_run():
    assert D.RunOptions().narrowed is False
    assert D.RunOptions(force=True, dry_run=True).narrowed is False


# ── pre-flight ───────────────────────────────────────────────────────────────

def test_ocr_engine_selects_its_own_api_key():
    assert D.ocr_env_requirement("mineru") == ("MINERU_API_KEY",)
    assert D.ocr_env_requirement("datalab") == ("DATALAB_API_KEY",)


def test_unknown_ocr_engine_accepts_either_key():
    assert set(D.ocr_env_requirement("something-new")) == {"DATALAB_API_KEY", "MINERU_API_KEY"}


def test_ocr_engine_matching_ignores_case_and_padding():
    assert D.ocr_env_requirement("  MinerU ") == ("MINERU_API_KEY",)


def test_missing_env_reports_alternatives_as_one_requirement():
    missing = D.missing_env([D.BY_KEY["apply"]], environ={})
    assert "NEO4J_USERNAME or CLIENT_ID" in missing


def test_either_credential_name_satisfies_the_requirement():
    env = {"CLIENT_ID": "x", "CLIENT_SECRET": "y"}
    assert D.missing_env([D.BY_KEY["apply"]], environ=env) == []


def test_embedding_stage_requires_a_google_key():
    env = {"CLIENT_ID": "x", "CLIENT_SECRET": "y"}
    assert D.missing_env([D.BY_KEY["embed-markdown"]], environ=env) == [
        "GOOGLE_API_KEY or GEMINI_API_KEY"]


def test_requirements_are_not_reported_twice_across_stages():
    stages = [D.BY_KEY["apply"], D.BY_KEY["places"], D.BY_KEY["promote"]]
    missing = D.missing_env(stages, environ={})
    assert len(missing) == len(set(missing)) == 2


def test_preflight_checks_the_ocr_key_only_when_ocr_is_in_the_plan():
    env = {"CLIENT_ID": "x", "CLIENT_SECRET": "y", "OPENROUTER_API_KEY": "k",
           "DESCRIPTION_OCR_ENGINE": "datalab"}
    assert D.missing_env([D.BY_KEY["extract"]], environ=env) == []
    assert D.missing_env([D.BY_KEY["ocr"]], environ=env) == ["DATALAB_API_KEY"]


# ── resume ───────────────────────────────────────────────────────────────────

def test_resume_key_points_at_the_first_unfinished_stage():
    manifest = {"stages": [{"key": "ocr", "status": "ok"},
                           {"key": "extract", "status": "failed"},
                           {"key": "apply", "status": "pending"}]}
    assert D.resume_key(manifest) == "extract"


def test_resume_key_reruns_an_interrupted_stage_rather_than_skipping_it():
    manifest = {"stages": [{"key": "ocr", "status": "ok"},
                           {"key": "extract", "status": "interrupted"}]}
    assert D.resume_key(manifest) == "extract"


def test_resume_key_is_none_when_the_last_run_completed():
    manifest = {"stages": [{"key": k, "status": "ok"} for k in D.STAGE_KEYS]}
    assert D.resume_key(manifest) is None


def test_resume_key_is_none_without_a_manifest():
    assert D.resume_key(None) is None


def test_resume_key_ignores_a_stage_key_that_no_longer_exists():
    # A manifest written before a stage was renamed must not produce a --from
    # value that resolve_plan would reject.
    assert D.resume_key({"stages": [{"key": "retired-stage", "status": "failed"}]}) is None


def test_latest_manifest_picks_the_newest_run(tmp_path):
    (tmp_path / "run_20260101_000000.json").write_text('{"run_id": "old"}')
    (tmp_path / "run_20260808_120000.json").write_text('{"run_id": "new"}')
    assert D.latest_manifest(tmp_path)["run_id"] == "new"


def test_latest_manifest_is_none_when_no_runs_exist(tmp_path):
    assert D.latest_manifest(tmp_path) is None


def test_latest_manifest_survives_a_truncated_file(tmp_path):
    # A run killed mid-write leaves invalid JSON; --resume must degrade to
    # "run everything", not crash.
    (tmp_path / "run_20260808_120000.json").write_text('{"run_id": ')
    assert D.latest_manifest(tmp_path) is None


# ── execution policy ─────────────────────────────────────────────────────────

class _Result:
    def __init__(self, returncode):
        self.returncode = returncode


def _stub_runner(codes, calls):
    """subprocess.run stand-in returning the given exit codes in order."""
    def fake(argv, **kwargs):
        calls.append(argv)
        return _Result(codes[len(calls) - 1])
    return fake


def test_run_plan_stops_at_the_first_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(D.subprocess, "run", _stub_runner([0, 1, 0], calls))
    plan = D.resolve_plan(only="extract,apply,places")
    rc = D.run_plan(plan, D.RunOptions(), runs_dir=tmp_path)
    assert rc == 1
    assert len(calls) == 2  # third stage never launched


def test_continue_on_error_runs_every_stage_and_still_fails(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(D.subprocess, "run", _stub_runner([0, 1, 0], calls))
    plan = D.resolve_plan(only="extract,apply,places")
    rc = D.run_plan(plan, D.RunOptions(), continue_on_error=True, runs_dir=tmp_path)
    assert rc == 1
    assert len(calls) == 3


def test_successful_run_returns_zero_and_writes_an_ok_manifest(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(D.subprocess, "run", _stub_runner([0, 0], calls))
    plan = D.resolve_plan(only="extract,apply")
    assert D.run_plan(plan, D.RunOptions(), runs_dir=tmp_path) == 0

    manifest = json.loads(next(tmp_path.glob("run_*.json")).read_text())
    assert manifest["status"] == "ok"
    assert manifest["plan"] == ["extract", "apply"]
    assert [s["status"] for s in manifest["stages"]] == ["ok", "ok"]


def test_manifest_of_a_failed_run_is_resumable_at_the_failed_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(D.subprocess, "run", _stub_runner([0, 1], []))
    D.run_plan(D.resolve_plan(only="extract,apply"), D.RunOptions(), runs_dir=tmp_path)
    assert D.resume_key(D.latest_manifest(tmp_path)) == "apply"


def test_manifest_records_the_exact_argv_of_each_stage(tmp_path, monkeypatch):
    monkeypatch.setattr(D.subprocess, "run", _stub_runner([0], []))
    D.run_plan(D.resolve_plan(only="promote"), D.RunOptions(limit=5), runs_dir=tmp_path)
    manifest = D.latest_manifest(tmp_path)
    assert "--skip-parcels" in manifest["stages"][0]["argv"]


def test_http_api_flag_reaches_every_child_process(monkeypatch):
    monkeypatch.delenv("NEO4J_HTTP_API", raising=False)
    assert D._child_env(D.RunOptions(http_api=True))["NEO4J_HTTP_API"] == "1"
    assert "NEO4J_HTTP_API" not in D._child_env(D.RunOptions())


# ── CLI ──────────────────────────────────────────────────────────────────────

def test_list_prints_every_stage_and_exits(capsys):
    assert D.main(["--list"]) == 0
    out = capsys.readouterr().out
    for key in D.STAGE_KEYS:
        assert key in out


def test_plan_prints_commands_without_running_anything(capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("--plan must not launch a stage")
    monkeypatch.setattr(D.subprocess, "run", boom)
    assert D.main(["--plan", "--only", "extract"]) == 0
    assert "pipeline.load_extractions" in capsys.readouterr().out


def test_bad_stage_name_exits_two(capsys):
    assert D.main(["--plan", "--only", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_resume_cannot_be_combined_with_from():
    assert D.main(["--resume", "--from", "extract"]) == 2


def test_empty_plan_exits_two():
    assert D.main(["--plan", "--skip", ",".join(D.STAGE_KEYS)]) == 2


def test_preflight_blocks_a_run_missing_credentials(capsys, monkeypatch):
    monkeypatch.setattr(D.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no run")))
    for name in ("NEO4J_USERNAME", "CLIENT_ID", "NEO4J_PASSWORD", "CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert D.main(["--only", "apply"]) == 2
    assert "NEO4J_USERNAME or CLIENT_ID" in capsys.readouterr().err
