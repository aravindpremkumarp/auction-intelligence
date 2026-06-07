"""
evals/
------
LangSmith-style evaluation for the chat agent, built on `pydantic-evals`.

- `cases.py`       — the golden-question catalogue (dependency-free, the single
                     source of truth shared with the offline pytest shape test).
- `evaluators.py`  — custom evaluators (tool-trajectory, no-write-error) plus
                     the LLM-as-judge rubric.
- `dataset.py`     — assembles a `pydantic-evals` Dataset from the catalogue.
- `run_golden.py`  — live runner: executes each case through the real agent,
                     applies evaluators, prints a report, streams results to
                     Logfire, and exits non-zero on regression (CI gate).

Run live (needs OpenRouter + Neo4j credentials):

    python -m evals.run_golden
"""
