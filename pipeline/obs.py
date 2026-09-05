"""
pipeline/obs.py
---------------
Shared observability for pipeline runs: structured logging and LLM usage
metering.

Logging: `get_logger(__name__)` returns a logger writing to stderr with a
greppable `pipeline.obs <level> <name> <message>` shape. Level comes from
PIPELINE_LOG_LEVEL (default INFO). The interactive print()/tqdm output of the
stage scripts is untouched — loggers carry the *operational* record (skipped
records, failed writes, merge conflicts, usage summaries) that print lines
were losing.

Usage metering: the OpenRouter batch stages (e.g. classify_document) feed each
response's `usage` block into a module-level
`USAGE` meter. Stages log a summary line when they finish, and an optional
token budget (PIPELINE_LLM_TOKEN_BUDGET) aborts a runaway batch run before it
becomes an expensive surprise.
"""
from __future__ import annotations

import logging
import os
import sys
import threading

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "pipeline.obs %(levelname)s %(name)s %(message)s"))
        root = logging.getLogger("pipeline")
        root.addHandler(handler)
        root.setLevel(os.environ.get("PIPELINE_LOG_LEVEL", "INFO").upper())
        root.propagate = False
        _configured = True
    if not name.startswith("pipeline"):
        name = f"pipeline.{name}"
    return logging.getLogger(name)


class BudgetExceeded(RuntimeError):
    """Raised when a batch run blows through PIPELINE_LLM_TOKEN_BUDGET."""


class UsageMeter:
    """Accumulate OpenRouter `usage` blocks across a batch run.

    Thread-safe; async callers share the one event-loop thread but MinerU
    helpers run in workers, so a lock is cheap insurance.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def record(self, usage: dict | None) -> None:
        """Feed one response's `usage` block; enforce the optional budget."""
        with self._lock:
            self.calls += 1
            if usage:
                self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                self.completion_tokens += int(usage.get("completion_tokens") or 0)
            total = self.prompt_tokens + self.completion_tokens
        budget = os.environ.get("PIPELINE_LLM_TOKEN_BUDGET", "").strip()
        if budget and total > int(budget):
            raise BudgetExceeded(
                f"LLM token budget exceeded: {total} > PIPELINE_LLM_TOKEN_BUDGET={budget}"
            )

    def summary(self, stage: str) -> str:
        total = self.prompt_tokens + self.completion_tokens
        return (f"stage={stage} llm_calls={self.calls} "
                f"prompt_tokens={self.prompt_tokens} "
                f"completion_tokens={self.completion_tokens} total_tokens={total}")


# One shared meter per process — stages log .summary(<stage>) when they finish,
# so a full run_pipeline pass shows cumulative spend stage by stage.
USAGE = UsageMeter()
