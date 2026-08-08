"""The complete document extraction pipeline: notice file -> structured graph.

    python -m pipeline.document_pipeline              # every stage, whole corpus
    python -m pipeline.document_pipeline --plan       # print the plan, run nothing
    python -m pipeline.document_pipeline --limit 50   # first 50 documents
    python -m pipeline.document_pipeline --from extract          # resume at a stage
    python -m pipeline.document_pipeline --only extract,promote  # just these
    python -m pipeline.document_pipeline --resume     # continue the last failed run

WHY THIS EXISTS. `pipeline/run_pipeline.py` orchestrates the *legacy* path — the
flat `{verifiable, enrichment}` blob from a text prompt (Stage 1), the
single/multi classifier (1.3), the description extractor (1.4) and the
reserve-price description matcher (1.45). The grounded path that replaced it —
span-anchored LangExtract entities, deterministic unit parsing, the :Lot/:Parcel
spine — lives in `load_extractions` -> `apply_extractions` -> `resolve_places` ->
`promote_extractions`, and every one of those had to be run by hand, in the right
order, with the right flags. Getting one wrong is silent: promote before places
and the district rollups are stale; promote a `--limit` slice without
`--skip-parcels` and phase C resolves parcels against a partial corpus, which is
exactly the wrong answer rather than a missing one.

So this module owns the ORDER and the FLAG COUPLING, and nothing else. Every
stage is the module you would have typed, run as `python -m <module>` in a
subprocess. That is deliberate:

  * a stage that dies (OOM on a 300-page notice, an OpenRouter 429 storm) takes
    its own process down, not the run;
  * stage output streams straight through, so a long OCR batch looks the same
    here as it does standalone;
  * no import-time coupling — this file is stdlib-only, so `--plan`, the stage
    table and the resume logic stay testable without Neo4j, langextract, or a
    single API key.

STAGE ORDER IS THE ARCHITECTURE (docs/extraction-pipeline-review-2026-07.md §4):
read -> triage the read -> extract once, grounded -> normalize deterministically
-> resolve geography -> promote to the graph -> embed. Two couplings are load-
bearing and are enforced in code rather than left to the operator:

  places BEFORE promote   phase A writes the canonical district/taluk/village
                          each :Lot's location is resolved against. Reversed,
                          lots land with unresolved geography and stay that way
                          until the next full promote.
  narrowed run => --skip-parcels
                          phase C ("which lots are the same physical parcel?")
                          is a corpus-wide question. Answering it from a
                          `--limit 50` slice merges parcels that only *look*
                          unique because the other 2,150 documents weren't
                          loaded. `--limit` and `--filename` therefore force
                          `--skip-parcels`; run the pipeline unnarrowed, or run
                          `promote_extractions` alone afterwards, to get parcels.

Every stage is idempotent and cached, so re-running the whole thing is cheap and
`--resume` is always safe.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT_DIR / "pipeline" / "output" / "runs"

# Credential alternatives, checked in pre-flight. Each tuple is one requirement
# satisfied by ANY of its names — the repo reads Neo4j creds from either the
# NEO4J_* names or the Aura-style CLIENT_* ones (see pipeline/config.py).
NEO4J_ENV = (("NEO4J_USERNAME", "CLIENT_ID"), ("NEO4J_PASSWORD", "CLIENT_SECRET"))
LLM_ENV = (("OPENROUTER_API_KEY",),)
EMBED_ENV = (("GOOGLE_API_KEY", "GEMINI_API_KEY"),)


@dataclass(frozen=True)
class Stage:
    """One pipeline step: a module to run, and which of our flags it understands.

    The `supports_*` fields exist because these modules were written separately
    and their CLIs genuinely differ — `load_extractions` takes `--filename`,
    `resolve_places` takes neither `--limit` nor `--filename` (it is corpus-wide
    by nature), `embed_*` take `--workers`. Rather than paper over that with a
    lowest-common-denominator interface, the orchestrator declares what each one
    accepts and drops flags the stage cannot use — quietly, because "the user
    asked to limit the run and this stage has no notion of a limit" is not an
    error, it just means the stage runs whole.
    """
    key: str
    module: str
    title: str
    why: str
    supports_limit: bool = False
    supports_filename: bool = False
    supports_force: bool = False
    supports_dry_run: bool = False
    needs_env: tuple[tuple[str, ...], ...] = ()
    extra_args: tuple[str, ...] = ()


# The pipeline, in dependency order. Keys are stable and are what --only/--from/
# --to/--skip accept; changing one is a breaking change to anyone's saved command.
STAGES: tuple[Stage, ...] = (
    Stage(
        key="ocr",
        module="scripts.ocr_with_mineru",
        title="Read",
        why="notice file (jpg/png/pdf) -> layout-aware markdown, cached on disk",
        supports_limit=True,
        needs_env=NEO4J_ENV + LLM_ENV,
    ),
    Stage(
        key="load-markdown",
        module="pipeline.load_markdowns_to_neo4j",
        title="Load markdown",
        why="markdown + layout blocks -> :Document nodes (what every later stage reads)",
        supports_limit=True,
        supports_force=True,
        needs_env=NEO4J_ENV,
    ),
    Stage(
        key="ocr-health",
        module="pipeline.ocr_health",
        title="Triage OCR",
        why="intrinsic OCR pathology score — repetition loops, truncation, script drift",
        supports_limit=True,
        supports_force=True,
        needs_env=NEO4J_ENV,
    ),
    Stage(
        key="score-markdown",
        module="pipeline.score_markdown",
        title="Score coverage",
        why="how much of the notice the read actually captured",
        supports_limit=True,
        supports_force=True,
        needs_env=NEO4J_ENV,
    ),
    Stage(
        key="extract",
        module="pipeline.load_extractions",
        title="Extract (grounded)",
        why="ONE LangExtract pass -> span-anchored entities + validator score",
        supports_limit=True,
        supports_filename=True,
        supports_force=True,
        needs_env=NEO4J_ENV + LLM_ENV,
    ),
    Stage(
        key="apply",
        module="pipeline.apply_extractions",
        title="Apply to listings",
        why="grounded entities -> AuctionProperty fields, with deterministic unit parsing",
        supports_limit=True,
        supports_dry_run=True,
        needs_env=NEO4J_ENV,
    ),
    Stage(
        key="places",
        module="pipeline.resolve_places",
        title="Resolve geography (phase A)",
        why="scraped City/Area -> canonical district/taluk/village; must precede promote",
        supports_dry_run=True,
        needs_env=NEO4J_ENV,
    ),
    Stage(
        key="promote",
        module="pipeline.promote_extractions",
        title="Promote to graph (phase B/C)",
        why=":Lot per lot with identifiers/extents/boundaries, then :Parcel across the corpus",
        supports_limit=True,
        supports_filename=True,
        supports_dry_run=True,
        needs_env=NEO4J_ENV,
    ),
    Stage(
        key="embed-markdown",
        module="pipeline.embed_markdowns",
        title="Embed notices",
        why="notice_markdown_idx — semantic search over notice text",
        supports_limit=True,
        supports_force=True,
        needs_env=NEO4J_ENV + EMBED_ENV,
    ),
    Stage(
        key="embed-description",
        module="pipeline.embed_descriptions",
        title="Embed descriptions",
        why="property_desc_idx — semantic search over per-property descriptions",
        supports_limit=True,
        supports_force=True,
        needs_env=NEO4J_ENV + EMBED_ENV,
    ),
)

STAGE_KEYS: tuple[str, ...] = tuple(s.key for s in STAGES)
BY_KEY: dict[str, Stage] = {s.key: s for s in STAGES}


# ── plan resolution (pure) ───────────────────────────────────────────────────

def _split_keys(raw: str | None) -> list[str]:
    """Parse a comma-separated stage list, tolerating spaces and trailing commas."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _check_keys(keys: list[str], flag: str) -> None:
    unknown = [k for k in keys if k not in BY_KEY]
    if unknown:
        raise ValueError(
            f"{flag}: unknown stage(s) {', '.join(unknown)}. "
            f"Known stages: {', '.join(STAGE_KEYS)}")


def resolve_plan(only: str | None = None, from_: str | None = None,
                 to: str | None = None, skip: str | None = None) -> list[Stage]:
    """Which stages run, in pipeline order.

    `--only` is absolute (it ignores --from/--to); `--from`/`--to` window the
    full list; `--skip` subtracts from whatever survived. Order always comes
    from STAGES, never from the order the user typed the keys in — a plan that
    ran `promote` before `places` because that is how they were typed would be
    silently wrong rather than obviously wrong.
    """
    only_keys, skip_keys = _split_keys(only), _split_keys(skip)
    _check_keys(only_keys, "--only")
    _check_keys(skip_keys, "--skip")
    if from_:
        _check_keys([from_], "--from")
    if to:
        _check_keys([to], "--to")

    if only_keys:
        chosen = [s for s in STAGES if s.key in set(only_keys)]
    else:
        start = STAGE_KEYS.index(from_) if from_ else 0
        end = STAGE_KEYS.index(to) + 1 if to else len(STAGES)
        if end <= start:
            raise ValueError(f"--to {to} comes before --from {from_}; nothing would run")
        chosen = list(STAGES[start:end])

    return [s for s in chosen if s.key not in set(skip_keys)]


@dataclass
class RunOptions:
    """Everything the operator asked for, before it is mapped onto stage flags."""
    limit: int | None = None
    filename: str | None = None
    force: bool = False
    dry_run: bool = False
    http_api: bool = False
    extra: dict[str, list[str]] = field(default_factory=dict)

    @property
    def narrowed(self) -> bool:
        """True when this run sees only part of the corpus.

        Drives the --skip-parcels coupling: phase C is a whole-corpus join and
        answering it from a slice is worse than not answering it.
        """
        return self.limit is not None or bool(self.filename)


def build_argv(stage: Stage, opts: RunOptions, python: str | None = None) -> list[str]:
    """The exact command line for one stage — the unit the subprocess runs.

    Kept separate from execution so `--plan` shows the operator precisely what
    would run (copy-pasteable, to re-run one stage by hand) and so the flag
    coupling is unit-testable without a database.
    """
    argv = [python or sys.executable, "-m", stage.module]

    if opts.limit is not None and stage.supports_limit:
        argv += ["--limit", str(opts.limit)]
    if opts.filename and stage.supports_filename:
        argv += ["--filename", opts.filename]
    if opts.force and stage.supports_force:
        argv.append("--force")
    if opts.dry_run and stage.supports_dry_run:
        argv.append("--dry-run")

    # Phase C (parcel resolution) needs every identifier in the corpus to exist
    # before it can tell which lots share a parcel. On a narrowed run those
    # identifiers are missing, so resolving would MERGE parcels that are only
    # unique within the slice. Skip it and say so, rather than storing a wrong
    # answer that looks like a real one.
    if stage.key == "promote" and opts.narrowed:
        argv.append("--skip-parcels")

    argv += list(stage.extra_args)
    argv += opts.extra.get(stage.key, [])
    return argv


# ── pre-flight ───────────────────────────────────────────────────────────────

def ocr_env_requirement(engine: str | None = None) -> tuple[str, ...]:
    """Which API key the read stage needs, given the configured OCR engine.

    `DESCRIPTION_OCR_ENGINE` picks the backend (datalab by default, mineru
    historically), and they take different keys — checking for the wrong one
    would either block a valid run or wave through a run that dies on the first
    file. Unknown engine values accept either key, since the engine string is
    also read by scripts that may have added a backend since.
    """
    engine = (engine if engine is not None
              else os.environ.get("DESCRIPTION_OCR_ENGINE", "datalab")).strip().lower()
    if engine == "mineru":
        return ("MINERU_API_KEY",)
    if engine == "datalab":
        return ("DATALAB_API_KEY",)
    return ("DATALAB_API_KEY", "MINERU_API_KEY")


def missing_env(stages: list[Stage], environ: dict[str, str] | None = None) -> list[str]:
    """Env-var requirements the plan cannot satisfy, as human-readable strings.

    Checked for the WHOLE plan before anything runs. The alternative — discover
    the missing embedding key after a two-hour OCR batch — is how the weekly run
    used to fail.
    """
    env = environ if environ is not None else os.environ
    requirements: list[tuple[str, ...]] = []
    for stage in stages:
        requirements.extend(stage.needs_env)
        if stage.key == "ocr":
            requirements.append(ocr_env_requirement(env.get("DESCRIPTION_OCR_ENGINE")))

    missing, seen = [], set()
    for req in requirements:
        if req in seen:
            continue
        seen.add(req)
        if not any(env.get(name) for name in req):
            missing.append(" or ".join(req))
    return missing


# ── manifests / resume ───────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latest_manifest(runs_dir: Path | None = None) -> dict | None:
    """The newest run manifest, or None. Manifest names sort lexicographically
    by timestamp, so `max()` on the name is the newest run without stat calls."""
    runs = runs_dir or RUNS_DIR
    if not runs.is_dir():
        return None
    files = sorted(runs.glob("run_*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def resume_key(manifest: dict | None) -> str | None:
    """Stage to restart at: the first one in the last run that did not succeed.

    A stage that failed is re-run rather than skipped (it may have written
    partial output, and every stage is idempotent, so re-running is the safe
    default). Returns None when the last run completed — the caller then runs
    the full plan rather than silently doing nothing.
    """
    if not manifest:
        return None
    for entry in manifest.get("stages", []):
        if entry.get("status") != "ok":
            key = entry.get("key")
            return key if key in BY_KEY else None
    return None


# ── execution ────────────────────────────────────────────────────────────────

def _child_env(opts: RunOptions) -> dict[str, str]:
    env = dict(os.environ)
    if opts.http_api:
        # Bolt (7687) is blocked wherever egress is HTTP-only — Claude Code on
        # the web, most CI sandboxes. The Neo4j client routes through Aura's
        # HTTPS Query API when this is set, so the whole plan inherits it rather
        # than the operator remembering to prefix each stage.
        env["NEO4J_HTTP_API"] = "1"
    return env


def run_plan(stages: list[Stage], opts: RunOptions, *,
             continue_on_error: bool = False,
             runs_dir: Path | None = None) -> int:
    """Run each stage as a subprocess, recording a manifest as we go.

    The manifest is written after EVERY stage, not at the end, so a run killed
    mid-flight (Ctrl-C, a laptop closing on a two-hour OCR batch) still leaves
    `--resume` something to read.
    """
    runs = runs_dir or RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    manifest_path = runs / f"{run_id}.json"

    manifest = {
        "run_id": run_id,
        "started_at": _now(),
        "options": {
            "limit": opts.limit, "filename": opts.filename, "force": opts.force,
            "dry_run": opts.dry_run, "http_api": opts.http_api,
            "continue_on_error": continue_on_error,
        },
        "plan": [s.key for s in stages],
        "stages": [{"key": s.key, "status": "pending"} for s in stages],
        "status": "running",
    }

    def save() -> None:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    save()
    env = _child_env(opts)
    failed = 0
    t_run = time.time()

    for i, stage in enumerate(stages):
        argv = build_argv(stage, opts)
        entry = manifest["stages"][i]
        entry.update({"argv": argv, "started_at": _now(), "status": "running"})
        save()

        print("\n" + "=" * 72)
        print(f"[{i + 1}/{len(stages)}] {stage.key} — {stage.title}")
        print(f"  {stage.why}")
        print(f"  $ {' '.join(argv)}")
        print("=" * 72, flush=True)

        t0 = time.time()
        try:
            completed = subprocess.run(argv, cwd=ROOT_DIR, env=env, check=False)
            code = completed.returncode
        except KeyboardInterrupt:
            entry.update({"status": "interrupted", "seconds": round(time.time() - t0, 1),
                          "ended_at": _now()})
            manifest["status"] = "interrupted"
            save()
            print(f"\ninterrupted during {stage.key} — resume with "
                  f"`python -m pipeline.document_pipeline --resume`")
            return 130
        except OSError as exc:  # module missing, interpreter gone — not a stage bug
            code = 127
            entry["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  could not launch {stage.module}: {exc}")

        seconds = round(time.time() - t0, 1)
        entry.update({"returncode": code, "seconds": seconds, "ended_at": _now(),
                      "status": "ok" if code == 0 else "failed"})
        save()
        print(f"  -> {entry['status']} in {seconds}s")

        if code != 0:
            failed += 1
            if not continue_on_error:
                manifest["status"] = "failed"
                manifest["ended_at"] = _now()
                save()
                print(f"\nstage {stage.key} failed (exit {code}). "
                      f"Fix it and re-run with `--resume`, or pass --continue-on-error.")
                print(f"manifest: {manifest_path}")
                return 1

    manifest["status"] = "failed" if failed else "ok"
    manifest["ended_at"] = _now()
    manifest["seconds"] = round(time.time() - t_run, 1)
    save()

    print("\n" + "=" * 72)
    print(f"pipeline {manifest['status']} — {len(stages)} stage(s), "
          f"{failed} failed, {manifest['seconds']}s")
    print(f"manifest: {manifest_path}")
    if opts.narrowed and any(s.key == "promote" for s in stages):
        print("NOTE: parcels (phase C) were skipped — this run was narrowed. "
              "Run `python -m pipeline.promote_extractions` unnarrowed for parcels.")
    print("=" * 72)
    return 1 if failed else 0


def format_plan(stages: list[Stage], opts: RunOptions) -> str:
    """Human-readable plan — what `--plan` prints, and the run header."""
    lines = []
    for i, stage in enumerate(stages, 1):
        argv = build_argv(stage, opts)
        lines.append(f"{i:>2}. {stage.key:<18} {stage.title}")
        lines.append(f"    {stage.why}")
        lines.append(f"    $ {' '.join(argv)}")
    return "\n".join(lines)


def format_stage_table() -> str:
    """What `--list` prints: every stage, whether it is in the default plan."""
    rows = [f"{'STAGE':<18} {'TITLE':<28} WHAT IT DOES"]
    for stage in STAGES:
        rows.append(f"{stage.key:<18} {stage.title:<28} {stage.why}")
    return "\n".join(rows)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.document_pipeline",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list every stage and exit")
    ap.add_argument("--plan", action="store_true",
                    help="print the resolved plan (with exact commands) and exit")
    ap.add_argument("--only", help="comma-separated stages to run (ignores --from/--to)")
    ap.add_argument("--from", dest="from_", help="start at this stage")
    ap.add_argument("--to", help="stop after this stage")
    ap.add_argument("--skip", help="comma-separated stages to drop from the plan")
    ap.add_argument("--resume", action="store_true",
                    help="start at the first stage the last run did not complete")
    ap.add_argument("--limit", type=int, help="cap documents per stage (forces --skip-parcels)")
    ap.add_argument("--filename", help="single Document.filename (forces --skip-parcels)")
    ap.add_argument("--force", action="store_true", help="re-do cached work where supported")
    ap.add_argument("--dry-run", action="store_true",
                    help="pass --dry-run to stages that support it (no writes)")
    ap.add_argument("--http-api", action="store_true",
                    help="set NEO4J_HTTP_API=1 for every stage (Bolt-blocked environments)")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="keep going after a stage fails (default: stop)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="do not check env vars before running")
    args = ap.parse_args(argv)

    if args.list:
        print(format_stage_table())
        return 0

    from_ = args.from_
    if args.resume:
        if from_ or args.only:
            print("--resume cannot be combined with --from/--only", file=sys.stderr)
            return 2
        key = resume_key(latest_manifest())
        if key:
            from_ = key
            print(f"resuming at stage: {key}")
        else:
            print("no incomplete run found — running the full plan")

    try:
        stages = resolve_plan(args.only, from_, args.to, args.skip)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not stages:
        print("plan is empty — nothing to run", file=sys.stderr)
        return 2

    opts = RunOptions(limit=args.limit, filename=args.filename, force=args.force,
                      dry_run=args.dry_run, http_api=args.http_api)

    if args.plan:
        print(format_plan(stages, opts))
        return 0

    if not args.skip_preflight:
        missing = missing_env(stages)
        if missing:
            print("missing env var(s) required by this plan:", file=sys.stderr)
            for name in missing:
                print(f"  - {name}", file=sys.stderr)
            print("Set them in .env (see .env.example) or pass --skip-preflight.",
                  file=sys.stderr)
            return 2

    print(f"plan ({len(stages)} stage(s)):")
    print(format_plan(stages, opts))
    return run_plan(stages, opts, continue_on_error=args.continue_on_error)


if __name__ == "__main__":
    raise SystemExit(main())
