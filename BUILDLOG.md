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

---

## Session 1.2 — 2026-08-27 — Four §3.1 schemas, `Paise` alias, SQLite DDL

**Built**

- `pipeline/money.py`: `Paise = NewType('Paise', int)` per §4.5, plus `NonNegPaise = Annotated[Paise, Field(ge=0)]` used on every money field across the four schemas — every amount in §3.1 is a magnitude (a debit leg, a credit leg, a fee, a settlement total), never a signed net figure, so non-negativity is boundary validation, not an invented constraint.
- `pipeline/schemas.py`: the four §3.1 canonical schemas as frozen (immutable) Pydantic v2 `BaseModel`s — `ReconLine`, `Settlement`, `BankLine`, `LedgerEntry` — plus their four enums (`RazorpayEntityType`, `SettlementStatus`, `BankProfile`, `LedgerSource`) as `enum.StrEnum`. Field lists transcribed literally from the current (REV-14-corrected) §3.1 text, including the documented-but-unused `posted_at` and `on_hold` fields and their "MUST NOT be read as evidence" / "no consumer" callouts, preserved as docstring notes so a later session doesn't accidentally wire them in.
- `LedgerEntry` carries a `model_validator` enforcing the §3.1 rule stated in prose — `resolution_id` and `case_id` are set if and only if `source == controller_adjustment` — since this is a real cross-field rule the spec states explicitly, not an invented one.
- `pipeline/storage.py`: raw `sqlite3` DDL for the `ledger_entry` table with `UNIQUE(case_id, resolution_id)`, plus a minimal `connect()` / `insert_ledger_entry()` pair — enough to make the constraint testable, nothing beyond that (no idempotency pre-check logic, no query helpers; that is Phase 4's `apply.py`).
- `tests/test_schemas.py`: one round-trip test per schema (construct → `model_dump_json` → `model_validate_json` → equality), plus two tests for the `LedgerEntry` cross-field validator (rejects `resolution_id`/`case_id` set without `controller_adjustment`, and rejects `controller_adjustment` without them).
- `tests/test_storage.py`: insert-and-read-back, a duplicate `(case_id, resolution_id)` insert raising `sqlite3.IntegrityError`, and a check that two `manual`/`erp_import` entries (both `case_id = NULL, resolution_id = NULL`) do *not* collide — documenting that SQLite treats NULLs as distinct under `UNIQUE`, which is the behavior the idempotency invariant actually needs.
- Full suite green: 11 passed (import guard + 7 schema tests + 3 storage tests). Verified no `float`, `datetime.now()`, or unseeded `random` anywhere under `pipeline/`.

**Broke**

- Nothing broke. Schemas and DDL are new, additive code with no prior behavior to regress.

**Cut**

- Nothing cut. Session scope was the four schemas, `Paise`, and the DDL only — no adapter, generator, or validator-chain code, per the session-decomposition table.

**Decided**

- Schemas live under `pipeline/`, not a new top-level `schemas/` package. §4.1 says the generator is a separate entry point the pipeline never imports, but does not forbid the reverse; since these are described as "canonical *input* schemas" to the pipeline, and data flows generator → pipeline, having the generator import `pipeline.schemas` in a later session is the correct direction and keeps the import guard's one-way rule intact (confirmed: the guard only scans `pipeline/`, so this is unaffected either way).
- All four schemas are `frozen=True`. Only `recon_line` is explicitly called "never mutated" in the spec text, but none of the other three are working state either — they are raw external evidence, an adapted bank line, or a posted journal fact — so immutability was extended to all four for consistency rather than left as an inconsistent special case.
- Did **not** restrict `LedgerEntry.account_code` to an enum of the 7 §3.2 chart-of-accounts codes, even though the COA is locked and small. §3.1's schema block states `account_code` as a plain string with an FK comment; enforcing "account permitted for this template" is explicitly Phase 4 validator-chain territory (invariant 1.7.5, session 4.3). Encoding it here would pre-empt that check with a cruder one and risk conflicting with how the validator chain is actually specified to work.
- Did **not** enforce the `entity_id` prefix convention (`pay_*`, `rfnd_*`, `trf_*`, `adj_*`) as a regex/pattern constraint — that comment in §3.1 reads as illustrative, not a stated requirement, and inventing it risks rejecting a legitimately-shaped real payload.

**Next**

Session 1.3 builds the generator core: record shape, money and rounding (`decimal.Decimal` with `ROUND_HALF_UP`, cast to `int`/`Paise` immediately), seeded RNG threaded through (no unseeded `random`), and the clean-case path only (no anomaly injection yet — that's Phase 2). Nothing under `generator/` exists yet beyond an empty `generator/__init__.py`; it will need to import `pipeline.schemas` (`ReconLine`, `Settlement`, `BankLine`, `LedgerEntry`) and `pipeline.money` (`Paise`, `NonNegPaise`) to construct its records — both now exist and are stable. The checkpoint for 1.3 is the three Phase 1 assertions from spec.md §6 line ~903: `generate --seed 1` runs end to end; `settlement.amount == sum(credits) − sum(debits) − fees − tax` holds on every generated settlement; and the generated ledger balances globally (`Σ debits == Σ credits` in integer paise). The `uv run generate` CLI entry point referenced in `AGENT.md`'s command surface does not exist yet and needs to be wired up (likely via `typer`, already a dependency) as part of this session.
