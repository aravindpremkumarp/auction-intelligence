# Archived modes (inactive)

These mode specs are **not wired into the app**. They are kept here so they
can be brought back if needed, without cluttering the active `modes/` set.

- `scan.md` — Broad search of opportunities.
- `shortlist.md` — Narrow to candidates.
- `evaluate.md` — Deep score one property.
- `track.md` — Move items through the 8-state pipeline.
- `refresh.md` — Re-score after new data.

## Why they're inert

The agent only ever loads:

1. `modes/_shared.md` — always, into the system prompt (`api/agent.py`).
2. A single requested overlay `modes/<id>.md` — only when its `id` is in the
   `_AVAILABLE_MODES` whitelist in `api/chat/router.py`.

`_load_mode_file()` reads `modes/<name>.md`, not subdirectories, so nothing in
this folder is ever read. Files here cost **zero** input tokens.

## To re-activate one

1. `git mv modes/_archive/<name>.md modes/<name>.md`
2. Add an entry to `_AVAILABLE_MODES` in `api/chat/router.py` (and to
   `_GATED_MODES` if it should be login-gated).
3. Re-add it to the mode lists in `README.md` and
   `config/CODEBASE_OVERVIEW.txt`.
