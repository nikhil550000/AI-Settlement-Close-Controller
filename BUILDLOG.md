# BUILDLOG.md

Append-only. One entry per session, five fixed subheads: **Built / Broke / Cut / Decided / Next.**

Per spec.md §6.2, this log feeds the incremental README/ARCHITECTURE writing, supplies the raw material for the video's "what broke and what I did about it" segment, and its **Next** field is the handoff between stateless implementation sessions (§6.3). The **Next** field is mandatory and must be specific — not "continue Phase X."

---

## Session 1.1 — 2026-08-27 — Skeleton, uv, package layout, import guard, AGENT.md

**Built**

- Discovered the project directory was nested inside a git repo rooted at the user's home directory (`C:\Users\Nikhil Sai`), which would have made every home-directory file (including `.ssh/`, credentials, and unrelated personal files) part of this project's history. Confirmed with the user and initialized a fresh, independent `git init` in the project folder instead, per §6.4's public-repo requirement.
- `AGENT.md` written first, per session instructions, following the §6.1 content list exactly (invariants 1.7.1–1.7.5 restated verbatim, the money rule, determinism rules, invariant 1.7.2 in full, repo layout, command surface, current-phase pointer, §2.0 cut order, do-not-invent-scope instruction).
- `uv init` for a Python 3.11+ project (`.python-version` pins 3.11). Removed the default `main.py` scaffold uv init generates — not part of the spec'd layout.
- Dependencies added per §4.5, Phase-1 subset only: `pydantic`, `typer` as runtime deps; `pytest` as a dev dep. `pandas`, `openpyxl`, `jinja2`, and the `openai` SDK deliberately not added — they belong to the sessions that use them (adapters, report, Slot A).
- Package layout: `generator/` and `pipeline/` as sibling packages, each currently just an `__init__.py` — no schema, model, or generator code this session, per instructions.
- `tests/test_import_guard.py`: walks every `.py` file under `pipeline/` with `ast.parse` (static analysis, no execution) and fails if any file's `import`/`from ... import` statements name `generator` as a top-level module. Because every file in the transitive chain lives under `pipeline/` and is scanned directly, a transitive violation (`pipeline.a` imports `pipeline.b` imports `generator`) is caught by the same per-file scan — no separate graph-traversal step was needed.
- `.gitignore` created now (Session 1.1), before any code exists to need it, so `.env` and the future Fireworks API key have a safe home from day one — per §6.4's explicit reasoning that this must not be a Day 5 afterthought. Covers `.env`, `.venv/`, `__pycache__/`, the SQLite ledger, and run scratch output.
- `.env.example` stub created (empty template) since §6.4 lists it as committed.
- `README.md` and `ARCHITECTURE.md` created with headings only, to be filled incrementally per §6 ("Documentation is written incrementally, not on Day 7").

**Broke**

- Nothing broke. No schema or logic code was written this session to break.

**Cut**

- Nothing cut. Session scope was skeleton-only per instructions.

**Decided**

- Used `ast`-based static parsing for the import guard rather than `importlib`/runtime introspection, per the explicit instruction that this must be "static analysis of the import graph, not a runtime check." Relative imports (`from . import x`) are skipped in the scan since they cannot resolve to `generator` by construction.
- Git repo re-rooted into the project folder rather than continuing to use the pre-existing home-directory repo (see Built above). Logged here since it is a decision the spec didn't anticipate needing.

**Next**

Session 1.2 builds the four §3.1 canonical schemas (`recon_line`, `settlement`, `bank_line`, `ledger_entry`) as Pydantic v2 models, the `Paise = NewType('Paise', int)` alias, and the SQLite DDL with `UNIQUE(case_id, resolution_id)`. Nothing schema-related exists yet — `generator/__init__.py` and `pipeline/__init__.py` are empty. The checkpoint for 1.2 is: each schema round-trips (construct → serialize → parse back equal), and the unique constraint rejects a duplicate `(case_id, resolution_id)` insert.
