# AGENT.md

## One-liner

An agent that ingests a merchant's normalized accounting-ledger export, Razorpay Settlement Reconciliation data, and the merchant's bank statement, and closes the settlement accounting loop across a batch of transactions — matching high-confidence records, detecting and correcting accounting discrepancies via a fixed template allowlist, categorizing operational exceptions, and abstaining when evidence is insufficient.

**`spec.md` is the single source of truth for this build.** Every implementation decision must be traceable to something in it. If the spec does not cover something, stop and ask — see the last rule in this document.

## Safety and audit invariants (§1.7, restated verbatim)

The following invariants apply to every automated decision the Controller makes. Any violation prevents auto-action and downgrades the case to `REVIEW_REQUIRED` or `ABSTAINED`.

1. **Monetary arithmetic uses integer paise.** All comparisons and residuals are exact; no floating-point rupees anywhere in matching or JV computation.
2. **Journal entries reaching `AUTO_CLOSED` must be instantiated from a fixed allowlist of accounting templates** (defined in Section 3). The model may classify which template applies and provide reasoning, but auto-applied accounts and amounts must be deterministically derived from source evidence — the model cannot invent accounts, amounts, or narrations on the automated path.
3. **Every automatic decision cites the source records and deterministic calculation that justify it** — no unsourced conclusions reach an auto-action state.
4. **Applied corrections are idempotent.** Each adjustment is tied to a unique `(case_id, resolution_id)` pair; reprocessing the same case cannot double-post. The synthetic ledger enforces this with a uniqueness constraint on the pair.
5. **Any failed safety validation prevents auto-action.** Validations applied to every candidate JV before it reaches `AUTO_CLOSED`:
   - `sum(debits_paise) == sum(credits_paise)`;
   - selected template is in the allowlist;
   - each account used is permitted for the selected template (per template-specific allowed accounts and posting directions);
   - all cited source records exist and are unposted for this specific correction;
   - the entry has not previously been posted for this `(case_id, resolution_id)`;
   - post-adjustment residual equals 0 paise on re-reconciliation.

## The money rule

Integer paise end to end, no floats, ever. No floating-point rupee value may enter matching, residual computation, or JV derivation (NFR-04). `decimal.Decimal` with `ROUND_HALF_UP` appears only inside the generator's fee and GST rounding and is cast to `int` immediately.

## Determinism rules

- No unseeded `random` anywhere in the pipeline or generator.
- No `datetime.now()` anywhere in the pipeline.
- The batch snapshot date is a parameter, never derived from wall-clock time.

## Invariant 1.7.2, in full

**Journal entries reaching `AUTO_CLOSED` must be instantiated from a fixed allowlist of accounting templates** (defined in Section 3). The model may classify which template applies and provide reasoning, but auto-applied accounts and amounts must be deterministically derived from source evidence — the model cannot invent accounts, amounts, or narrations on the automated path.

## Repository layout

```
AI Settlement Close Controller/
├── spec.md                 # single source of truth
├── AGENT.md                # this file
├── BUILDLOG.md             # append-only, one entry per session
├── README.md                # written incrementally, 20 min/day
├── ARCHITECTURE.md          # written incrementally, 20 min/day
├── pyproject.toml           # uv-managed
├── generator/                # synthetic data + ground truth; separate entry point
│   └── ...
├── pipeline/                 # graded path; MUST NOT import generator, ever
│   └── ...
├── tests/
│   └── test_import_guard.py  # static analysis: pipeline/ never imports generator/
└── data/                      # committed seeded reference dataset + ground truth (per §6.4)
```

`pipeline/` must never import `generator/` — §4.1 requires this to be structurally impossible rather than merely discouraged, because generator logic leaking into the graded path would invalidate every metric in §1.6. This is enforced by a pytest import-guard test that statically walks the `pipeline/` module graph.

## Command surface

- `uv run generate --seed <n>` — runs the generator, emits a batch plus ground truth.
- `uv run pytest` — runs the test suite, including the import guard.
- (Later sessions add the pipeline CLI surface per FR-10; not yet built.)

## Current phase pointer

**Phase 3, Session 3.3 complete** (of 19 implementation sessions across 7 phases — see spec.md §6.3). Session 3.1 built the FR-08 bank adapter: `pipeline/adapters/profiles/{hdfc,icici,axis}.yaml` (the three declarative column maps per §2.6) loaded by `pipeline/adapters/profiles.py`, `pipeline/adapters/bank_adapter.py`'s parser (locates the header row and the table's end by content, not by a configured junk-row count, so it handles any junk-header/summary-block shape), and `generator/bank_export.py`'s mirror-image writer for round-trip testing. Both halves load the same YAML so they cannot drift apart. `pipeline/money.py` gained `rupees_string_to_paise`/`paise_to_rupees_string` (pure integer/string arithmetic, no float or `Decimal`) for the comma-grouped rupee strings a bank export prints. `tests/test_bank_adapter.py` is the session checkpoint: all three profiles, CSV and XLSX, parse a shared record set to an identical canonical `bank_line`, verified both on hand-built fixtures and on the full 176-line reference batch. Session 3.2 built case assembly (component 2, §4.1): `pipeline/loaders.py` (bare JSONL loaders for the four §3.1 schemas plus ground truth) and `pipeline/case_assembly.py`'s `assemble_cases` — settlement-anchored cases (one per `Settlement`, `case_id == settlement.id`) plus orphan cases from residual bank lines, classified deterministically by narration content and reference-token pairing (no settlement/UTR matching, which is session 3.3's job). Before writing case-assembly code, the session found and fixed a real bug in `generator/orphans.py`: "unrelated NEFT" noise was drawn 50/50 credit/debit, and the credit-direction draw was byte-identical in shape to a genuine `UNMATCHED_INBOUND_CREDIT` case — fixed (with user sign-off via `AskUserQuestion`) by making that noise population always debit. `tests/test_case_assembly.py` is the session checkpoint: 150 cases assemble (125 settlement-anchored + 25 orphan), orphan granularity matches REV-18 exactly (22 one-line cases + 3 two-line duplicate-credit cases = 28 bank lines). Session 3.3 built the matcher (component 3, §4.1): `pipeline/timing.py` (relocated from `generator/timing.py`, which now imports it back — the import guard forbids the reverse) and `pipeline/matcher.py`'s FR-09 four-tier cascade (`match_settlement_anchored_case`, `match_cases`, `match_tier_distribution`). `pipeline/case_assembly.py`'s `Case` gained three fields — `match_tier`, `residual_paise`, `in_settlement_window` — that the matcher fills in via `model_copy`; orphan cases pass through untouched. Tier 0 (whitespace-delimited UTR-or-`bank_ref_no` equality) versus tier 1 (`[A-Z0-9]{8,}` alphanumeric-run prefix match) is resolved by tokenizing on whitespace only at tier 0, which is what makes `CLEAN`-shaped credits hit tier 0 and `EMBEDDED`/`TRUNCATED`-shaped ones fall through to tier 1 — see the module docstring for the full reasoning. §3.3's timing-residual rule is applied inside the matcher itself: a tier-3 (no-match) case still inside the T+2-plus-slack window reports `residual_paise = 0`, not the full settlement amount. `tests/test_matcher.py` is the session checkpoint: one hand-built unit test per tier plus the tie/window edge cases, and `test_matches_at_more_than_one_tier_and_thirty_cases_reach_auto_matched` — against the full reference batch, tiers 0/1/2 are all populated (49/39/10 matches respectively at seed 0, 27 tier-3 no-matches) and all 30 ground-truth `AUTO_MATCHED` cases (18 fully-clean + 12 family-4 no-op) get `residual_paise == 0` from the matcher alone, checked at seed 0 and repeated across three more seeds.

Check `BUILDLOG.md`'s most recent entry's **Next** field for the actual current state before starting any session — this pointer is a coarse anchor, the **Next** field is the real handoff.

## §2.0 cut order

If the build falls behind, cuts fire in this fixed order, mechanical rather than improvised:

1. **Drop family 5** (settlement adjustment unposted) — removes 10 cases and templates `T-05`/`T-06`.
2. **Drop the third bank format profile** (Axis-shape).
3. **Reduce the reference batch to 100 cases** (last resort — cheapest to cut, most expensive to lose, since every metric denominator shrinks with it).

## Do not invent scope

**One instruction outranks the rest: do not invent scope. If the spec does not cover it, stop and ask.**
