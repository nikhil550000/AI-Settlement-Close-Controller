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

## Session 1.3 — 2026-08-27 — Generator core, clean-case path, `generate` CLI

**Built**

- `generator/rounding.py`: `percentage_of_paise(base: Paise, percent: Decimal) -> Paise` — the one place `decimal.Decimal` with `ROUND_HALF_UP` is permitted (§4.5), cast to `Paise` immediately. Used for both the 2% MDR fee and the 18% GST-on-fee derivation.
- `generator/clean.py`: `generate_clean_batch(rng, snapshot_date, n_settlements=18) -> CleanBatch`. Generates the §3.5 "Fully clean" population (n=18 by default, matching the case-allocation table) — settlements, their payment `recon_line`s, and the correct accrual-basis ledger posting for each payment: `Dr Razorpay Clearing (net), Dr Payment Gateway Charges (fee), Dr GST on Gateway Charges (tax) / Cr Sales Revenue (gross)`, the same entry worked out in §3.2's family-3 example. Every case is `NONE`/`AUTO_MATCHED` by construction — nothing omitted, mis-posted, or mis-timed.
- Payments-per-settlement uses the exact §3.5 distribution (`lognormvariate(ln(10), 0.5)`, clipped `[3, 25]`). Payment amount uses a lognormal in the stated `₹100-₹50,000` range, median `~₹1,500`, with `sigma` chosen as generator config since the spec states the shape and range but not `sigma` (see Decided).
- A single `random.Random(seed)` instance is created once (in `generator/cli.py`) and threaded through every generation call — no second RNG instance, no unseeded `random.*` call anywhere under `generator/`. Verified by grep: the only `random` references outside the one `random.Random(seed)` construction are the `import random` statements and a docstring mention.
- `generator/cli.py`: the `generate` command (`typer`), wired as a real console-script entry point (`uv run generate --seed 1`) — required adding `[build-system]` (`hatchling`) and `[tool.hatch.build.targets.wheel] packages = ["generator", "pipeline"]` to `pyproject.toml`, since a bare `uv init` project has no build backend and `[project.scripts]` is silently inert without one. Snapshot date defaults to the literal constant `2026-08-28` (Phase 1's own day), never `datetime.now()`. Writes `settlements.jsonl`, `recon_lines.jsonl`, `ledger_entries.jsonl` to `scratch/generated/` (gitignored — this is a dev run, not yet the committed reference batch, which doesn't exist until Phase 2's anomaly populations complete it).
- `tests/test_generator_clean.py`, five tests covering the three Phase 1 checkpoint assertions plus two supporting ones: the CLI runs end to end via `typer.testing.CliRunner` (`generate --seed 1`); `settlement.amount == sum(credits) − sum(debits) − fees − tax` holds on all 18 generated settlements (recomputed independently from each settlement's own `recon_line`s, not just re-reading the field the generator itself set); the ledger balances globally (`Σ debits == Σ credits`, both nonzero); the same seed reproduces byte-identical `model_dump()` output on a second run; every money field on every generated record is a plain `int` (no float anywhere).
- Full suite green: 16 passed (import guard, 7 schema tests, 3 storage tests, 5 new generator tests). Manually confirmed via `uv run generate --seed 1` twice into separate output directories and `diff`ing the JSONL — byte-identical.

**Broke**

- First `uv run generate --seed 1` failed with `error: Failed to spawn: generate — program not found`. Cause: the project had no `[build-system]` table, so `uv sync` never built/installed the project itself and `[project.scripts]` had nothing to attach to. Fixed by adding `hatchling` as the build backend and pointing it at both `generator/` and `pipeline/` as wheel packages.

**Cut**

- Refund generation, adjustment generation, and bank-statement-line generation are explicitly **not** in this session, even though §3.5's "record shape" section states refund/adjustment volumes as part of the batch's overall composition. Reason: a "correctly booked, non-anomalous refund" has no defined journal entry anywhere in spec.md — only family 2's *anomalous* (missing) refund posting is specified (§3.2) — so building one now would invent an accounting treatment ahead of the spec rather than transcribe one. Refunds and adjustments arrive with the family injections in session 2.1. `bank_line` generation is Phase 2/3 territory (§4.6's UTR-variety obligation, the three-profile adapters) and isn't needed by any of the three Phase 1 checkpoint assertions.
- No `Ambiguous`, `FR-06 tax`, or any other §3.5 population beyond "Fully clean" — those all require anomaly-injection machinery that doesn't exist until Phase 2.

**Decided**

- `sigma = 0.8` for the payment-amount lognormal (mu = ln(1500)), clipped to `[₹100, ₹50,000]`. Spec states the distribution family, range, and target median explicitly but leaves the spread unstated; §3.5 itself frames these draws as "generator config," so this is logged as a config choice rather than treated as an ambiguity requiring a stop-and-ask.
- Ledger entries are booked with `source = erp_import`, not `manual` — these represent the merchant's own bulk-imported bookkeeping, which is the closer fit of the two non-`controller_adjustment` enum values (§3.1) since nothing here is a hand-keyed single entry.
- Each ledger leg gets its own `journal_entry_id`; multiple legs of the same economic transaction share a `reference` (the `recon_line.entity_id`) instead. Read `journal_entry_id string, unique` (§3.1) literally as a per-row key rather than a per-transaction voucher number — the schema has no separate "voucher ID" field, and `reference`'s stated purpose ("external ref: payment ID, invoice ID") is exactly the transaction-grouping key a flat ledger export needs.
- IDs (`pay_*`, `setl_*`, `je_*`, UTRs) are drawn from the RNG as random hex/base36, not sequential counters — §3.5's "Fingerprint control" (global shuffle, shared narration pool) is explicitly session 2.3's job, but there's no reason to hand that session a sequential-ID population to un-fingerprint when random IDs cost nothing now.
- Output goes to `scratch/generated/` (already gitignored as "run scratch output" per Session 1.1), not `data/`. `data/` is reserved for the committed seeded reference dataset (§6.4), which only exists once Phase 2's anomaly populations make the batch real; this session's output is a dev run proving the generator core works, not that artifact.

**Next**

Session 1.3 completes Phase 1 (session table, spec.md §6.3) — tag `phase-1` after this commit. Session 2.1 builds the five FR-04 family injections (families 1-5, §3.2/§3.4/§3.5) on top of the clean-case generator in `generator/clean.py`. Nothing anomaly-related exists yet: no refund/adjustment recon lines, no missing-ledger-entry injection, no `bank_line` generation, no orphan cases. The checkpoint for 2.1 is per-family counts asserting to exactly 10 each (§3.5's case-allocation table). `generator/clean.py`'s `_generate_payment`/`_generate_settlement` helpers and `generator/rounding.py`'s `percentage_of_paise` are the reusable pieces — family 1's template (`Dr Payment Gateway Charges, Dr GST on Gateway Charges / Cr Razorpay Clearing`) is literally "omit the fee/GST ledger legs this session already generates," so the cleanest path is parameterizing `_generate_payment` to optionally skip posting specific legs rather than duplicating the payment-generation logic. `pipeline/storage.py`'s `insert_ledger_entry` is untouched and unused by the generator (by design — generator output is JSONL, not the SQLite ledger; the SQLite ledger is what the pipeline's `apply` step writes to in Phase 4).

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
