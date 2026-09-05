"""
scripts/run_weekly_pipeline.py
-------------------------------
Single-command weekly orchestrator for the auction data pipeline.

Runs, in order:
  1. scrapers/phase1_harvest_urls.py   (plain script — bare "import utils")
  2. scrapers/phase2_scrape_details.py (plain script — bare "import utils")
  3. python -m scripts.prepare_tn_data
  4. python -m scripts.load_tn_to_neo4j
  5. python -m scripts.upload_downloads_to_r2
  6. python -m pipeline.run_pipeline
  7. python -m pipeline.embed_descriptions

NOTE: pipeline.run_pipeline already calls scripts.link_reauctions internally
as its final "STAGE 5" (see pipeline/run_pipeline.py), so this orchestrator
does NOT invoke scripts.link_reauctions separately — doing so would just
redo the same (cheap but pointless) full-corpus re-link twice.

Stages 1-2 are the Selenium scrapers. They run with a VISIBLE Chrome window
(headless=False, their existing default) because Cloudflare's challenge page
sometimes requires a human to manually solve a CAPTCHA. This script does not
attempt to make that unattended — it just pauses (the scrapers themselves
poll and wait) until a human clears it, then continues automatically.

Usage:
    C:\\Python314\\python.exe scripts\\run_weekly_pipeline.py
    C:\\Python314\\python.exe scripts\\run_weekly_pipeline.py --skip-scrape

--skip-scrape starts from stage 3 (prepare_tn_data), using whatever is
already sitting in data/live_eauction_data.jsonl (e.g. because the user
already ran the scrape manually earlier in the week).
"""

import argparse
import datetime
import os
import subprocess
import sys
import time

# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = r"C:\Python314\python.exe"
LOGS_DIR = os.path.join(REPO_ROOT, "logs")
ENV_FILE = os.path.join(REPO_ROOT, ".env")

# ── Env var requirements per stage ──────────────────────────────────────────
# (stage_name -> list of requirements; each requirement is either a single env
# var name, or a tuple of alternative names where at least one must be set —
# mirrors pipeline/config.py's own NEO4J_*/CLIENT_* and GOOGLE_API_KEY/
# GEMINI_API_KEY fallback aliases, so this preflight can't false-negative on a
# .env that uses the alias form instead of the primary name.)
REQUIRED_ENV_VARS = {
    "upload_downloads_to_r2": [
        "R2_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "R2_PUBLIC_BASE_URL",
    ],
    "pipeline.run_pipeline": [
        "OPENROUTER_API_KEY",
        # NEO4J_URI and NEO4J_DATABASE are NOT required here — both are
        # auto-derived by pipeline/config.py from NEO4J_USERNAME when unset.
        # MINERU_API_KEY is NOT required — pipeline.run_pipeline never calls
        # an OCR API; it reads the markdown / extraction_json already on the
        # graph's :Document nodes.
        ("NEO4J_USERNAME", "CLIENT_ID"),
        ("NEO4J_PASSWORD", "CLIENT_SECRET"),
    ],
    "pipeline.embed_descriptions": [
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        ("NEO4J_USERNAME", "CLIENT_ID"),
        ("NEO4J_PASSWORD", "CLIENT_SECRET"),
    ],
}


def load_env_file(path):
    """Minimal .env loader (no external dependency) — merges into os.environ
    without overwriting anything already set in the real environment."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def check_required_env(stage_name, log):
    """Fail fast if a stage's required env vars aren't set/non-empty.

    Each requirement is either a plain var name (must be set) or a tuple of
    alternative names (at least one must be set).
    """
    missing = []
    for req in REQUIRED_ENV_VARS.get(stage_name, []):
        names = (req,) if isinstance(req, str) else req
        if not any(os.environ.get(n) for n in names):
            missing.append(" or ".join(names))
    if missing:
        log(f"[ENV CHECK FAILED] Stage '{stage_name}' is missing required env vars: {', '.join(missing)}")
        log("Fix your .env (or machine environment) and re-run. Aborting before running any stage.")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Run the weekly auction data pipeline end-to-end.")
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip stages 1-2 (phase1/phase2 scrapers) and start from prepare_tn_data, "
             "using whatever is already in data/live_eauction_data.jsonl.",
    )
    args = parser.parse_args()

    # Load .env into os.environ up front so the pre-flight checks below see
    # the same values the subprocesses (via pipeline/config.py's load_dotenv())
    # will see.
    load_env_file(ENV_FILE)

    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = os.path.join(LOGS_DIR, f"pipeline_run_{timestamp}.log")
    log_file = open(log_path, "a", encoding="utf-8")

    def log(msg):
        line = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        log_file.write(line + "\n")
        log_file.flush()

    log(f"=== Weekly auction pipeline run starting (log: {log_path}) ===")
    log(f"Repo root: {REPO_ROOT}")
    log(f"Python interpreter: {PYTHON}")
    if args.skip_scrape:
        log("--skip-scrape passed: starting from stage 3 (prepare_tn_data)")

    # ── Stage definitions: (name, command list, cwd, env-check key) ─────────
    # Stages 1-2 are plain-script invocations (bare "import utils" requires
    # running them as a script, not "-m"), everything else is a package
    # invoked with "-m" per each module's own documented convention.
    stages = []

    if not args.skip_scrape:
        stages.append((
            "phase1_harvest_urls",
            [PYTHON, os.path.join(REPO_ROOT, "scrapers", "phase1_harvest_urls.py")],
            REPO_ROOT,
            None,
        ))
        stages.append((
            "phase2_scrape_details",
            [PYTHON, "-u", os.path.join(REPO_ROOT, "scrapers", "phase2_scrape_details.py")],
            REPO_ROOT,
            None,
        ))

    stages.append(("prepare_tn_data", [PYTHON, "-m", "scripts.prepare_tn_data"], REPO_ROOT, None))
    stages.append(("load_tn_to_neo4j", [PYTHON, "-m", "scripts.load_tn_to_neo4j"], REPO_ROOT, None))
    stages.append(("upload_downloads_to_r2", [PYTHON, "-m", "scripts.upload_downloads_to_r2"], REPO_ROOT, "upload_downloads_to_r2"))
    stages.append(("pipeline.run_pipeline", [PYTHON, "-m", "pipeline.run_pipeline"], REPO_ROOT, "pipeline.run_pipeline"))
    stages.append(("pipeline.embed_descriptions", [PYTHON, "-m", "pipeline.embed_descriptions"], REPO_ROOT, "pipeline.embed_descriptions"))

    # ── Pre-flight: verify all required env vars BEFORE running anything ────
    # (fail fast, before wasting time on earlier stages that would otherwise
    # succeed only for a later stage to blow up on a missing key)
    log("Running pre-flight env var checks for all stages that need them...")
    all_ok = True
    for name, _cmd, _cwd, env_key in stages:
        if env_key and not check_required_env(env_key, log):
            all_ok = False
    if not all_ok:
        log("=== ABORTED: one or more stages are missing required env vars. No stages were run. ===")
        log_file.close()
        sys.exit(1)
    log("Pre-flight env var checks passed.")

    # ── Run stages in sequence ───────────────────────────────────────────────
    results = []  # (name, status, elapsed_seconds)
    pipeline_start = time.time()

    for name, cmd, cwd, _env_key in stages:
        log(f"--- STAGE START: {name} ---")
        log(f"Command: {' '.join(cmd)}")
        stage_start = time.time()
        tail_buffer = []  # keep last 30 lines in memory for the failure report
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            # Stream output live line-by-line as the subprocess produces it —
            # important for stages 1-2, which are long-running and may need a
            # human watching the console/Chrome window in real time (Cloudflare).
            for line in proc.stdout:
                line = line.rstrip("\n")
                log(f"    | {line}")
                tail_buffer.append(line)
                if len(tail_buffer) > 30:
                    tail_buffer.pop(0)
            proc.wait()
        except Exception as exc:
            elapsed = time.time() - stage_start
            log(f"--- STAGE CRASHED (could not launch): {name} ({elapsed:.1f}s) ---")
            log(f"Exception: {exc}")
            results.append((name, "CRASHED", elapsed))
            break

        elapsed = time.time() - stage_start

        if proc.returncode != 0:
            log(f"--- STAGE FAILED: {name} (exit code {proc.returncode}, {elapsed:.1f}s) ---")
            log(f"Last {len(tail_buffer)} line(s) of output for '{name}':")
            for line in tail_buffer:
                log(f"    | {line}")
            results.append((name, "FAILED", elapsed))
            log(f"Stopping pipeline immediately — downstream stages depend on '{name}' having succeeded.")
            break

        log(f"--- STAGE OK: {name} ({elapsed:.1f}s) ---")
        results.append((name, "OK", elapsed))

    total_elapsed = time.time() - pipeline_start

    # ── Final summary (always printed, pass or fail) ─────────────────────────
    log("=== RUN SUMMARY ===")
    for name, status, elapsed in results:
        log(f"  {status:8s} {name:32s} {elapsed:8.1f}s")
    ran_names = [r[0] for r in results]
    skipped = [s[0] for s in stages if s[0] not in ran_names]
    for name in skipped:
        log(f"  {'SKIPPED':8s} {name:32s} {0.0:8.1f}s")
    overall_ok = all(status == "OK" for _n, status, _e in results) and not skipped
    log(f"TOTAL TIME: {total_elapsed:.1f}s | OVERALL: {'SUCCESS' if overall_ok else 'FAILURE'}")

    summary_line = " -> ".join(f"{n}:{s}" for n, s, _e in results) + (
        " -> " + " -> ".join(f"{n}:SKIPPED" for n in skipped) if skipped else ""
    )
    log(f"ONE-LINE SUMMARY: {summary_line}")

    log_file.close()
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
