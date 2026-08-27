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

**Phase 2, Session 2.3 complete — Phase 2 done** (of 19 implementation sessions across 7 phases — see spec.md §6.3). Session 2.3 added the global pass that runs once over the assembled batch (`generator/finalize.py`): the shuffled ID re-mint, §4.6's 50/25/15/10 UTR narration split, and the shuffle of emission order, plus one shared narration pool (`generator/narration.py`) behind every free-text string the generator writes. `tests/test_generator_fingerprint.py` is the §3.5 fingerprint checkpoint; `pipeline/fingerprint.py` holds the statistic it and (per §5.3) the Phase-6 metrics JSON are built on.

Check `BUILDLOG.md`'s most recent entry's **Next** field for the actual current state before starting any session — this pointer is a coarse anchor, the **Next** field is the real handoff.

## §2.0 cut order

If the build falls behind, cuts fire in this fixed order, mechanical rather than improvised:

1. **Drop family 5** (settlement adjustment unposted) — removes 10 cases and templates `T-05`/`T-06`.
2. **Drop the third bank format profile** (Axis-shape).
3. **Reduce the reference batch to 100 cases** (last resort — cheapest to cut, most expensive to lose, since every metric denominator shrinks with it).

## Do not invent scope

**One instruction outranks the rest: do not invent scope. If the spec does not cover it, stop and ask.**
