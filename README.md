# AI Settlement Close Controller

Razorpay AI Buildathon 2026, Track 4 (AI Finance Controller). Solo build. `spec.md` is the single source of truth — every claim below traces to a section of it, and every number is read off a committed artifact, not typed from memory.

## What this is

An agent that ingests a merchant's normalized accounting-ledger export, Razorpay Settlement Reconciliation data, and the merchant's bank statement, and closes the settlement accounting loop across a batch of transactions:

1. Matches high-confidence records across the three sources (§1.1.1, FR-09's four-tier cascade).
2. Detects accounting discrepancies — unposted refunds, fee/GST mismatches, chargeback lifecycle events, settlement holds, timing differences — and derives a correcting journal entry from a **fixed allowlist of six templates** (§3.4), never an invented account or amount (invariant 1.7.2).
3. Validates every candidate against the full §1.7.5 chain (balance, allowlist membership, evidence, idempotency, zero post-adjustment residual) before it is allowed to post.
4. Applies validated corrections to a synthetic ledger and re-reconciles to confirm the discrepancy actually closed.
5. Categorizes operational exceptions it cannot resolve itself and names the external action required.
6. Abstains, explicitly, when evidence does not justify an automated decision — abstention is a designed outcome (§1.3), not a failure.

Every case terminates in exactly one of five states: `AUTO_MATCHED`, `AUTO_CLOSED`, `EXTERNAL_ACTION_REQUIRED`, `REVIEW_REQUIRED`, `ABSTAINED` (§1.3).

**Where the model is allowed to act, exactly.** Invariant 1.7.2: the model may *classify* which of the six templates applies and may write prose describing what it found; it may never originate an account, an amount, or a narration on the automated path. Concretely, this build has exactly two model-touching slots (§4.2): **Slot A**, a constrained eight-value classification over evidence already computed deterministically (graded — §5.2's confusion matrices and per-subtype precision/recall are Slot A's scorecard); and **Slot B**, free-text prose for a human reviewer, explicitly labelled model-generated in the report and never itself evidence. Everything that reaches the ledger — which account, which amount, which template — is computed by deterministic code the model never touches.

## Non-goals

Explicitly out of scope (§2.10 and FR-10's own framing):

- Not a general reconciliation or rules-based bookkeeping product — Razorpay already ships those (Settlement Reconciliation API, Razorpay Recon, the Bookkeeping Agent). This build is the exception-resolution layer above predefined rules: the cases where merchant ledger, settlement data, and bank statement disagree in ways a rule can't resolve.
- No web server, SPA, database UI, authentication, or live dashboard. The CLI is the product (FR-10); the report is one static HTML file (FR-11).
- No real merchant data, anywhere. Every record — settlements, recon lines, ledger entries, bank lines, ground truth — comes from one seeded synthetic generator (`generator/`), which the graded pipeline (`pipeline/`) is structurally forbidden from importing (§4.1's import guard, `tests/test_import_guard.py`).
- No FR-05 (recognition-entry proposals on `EXTERNAL_ACTION_REQUIRED` cases) in v1 — §2.4's stated fallback, unbuilt by design.

## Required disclosures

Two facts the spec requires stated everywhere the numbers below are shown — here, in the FR-11 report header, and in the pitch video — verbatim in substance (§3.5, §5.3):

> **Synthetic evaluation.** Ground-truth labels and the records being graded come from one generator. The evaluation measures whether the pipeline recovers the injected intent; it does not establish that the injected intent resembles a real merchant's books.

> **Anomaly enrichment.** `match_rate` on this batch is not comparable to any industry figure. The batch is deliberately anomaly-enriched for metric legibility — only 30 of 150 cases (20%) require no action at all, against a real-world break rate closer to low single digits, roughly an order of magnitude enrichment. `EXTERNAL_ACTION_REQUIRED` runs high because orphan cases are unresolvable by construction (§3.6) and are the majority of that state's population: 36 of 150 cases (24.0%) are **ground-truth** `EXTERNAL_ACTION_REQUIRED`; the system's own predicted count varies by classifier arm (36/150 deterministic, 43/150 Slot A). The seven extra cases on the Slot A arm are **not** recoveries: both arms have `UNMATCHED_INBOUND_CREDIT` recall of exactly 8/8, so Slot A finds no true positive the deterministic arm misses. The seven are ground-truth `ABSTAINED` cases Slot A wrongly promotes to a confident exception — the same seven the eval report prints as `7 × ground truth ABSTAINED → predicted EXTERNAL_ACTION_REQUIRED`. See the arm comparison below.

## Quickstart / reproduce path

```bash
uv sync
uv run reconcile
```

That single command (FR-10) reads the committed reference batch at `data/reference/` (seed 0, snapshot date 2026-08-28), runs the full pipeline — matching, evidence, template instantiation, validation, ledger apply, Slot A classification, Slot B narration, metrics — and writes both a console summary and `.run/report.html` / `.run/metrics.json`. Defaults: `--classifier baseline` (the arm that measures best — see the arm comparison below), `--cache-mode strict` (no network call is ever made; `--cache-mode refresh` is the only mode that calls Fireworks, and needs `FIREWORKS_API_KEY`). `--classifier hybrid` and `--classifier llm` select the other two arms; all three run offline against the committed cache.

To reproduce the **exact pinned run** (FR-13) in place, overwriting nothing but the two files already committed at `data/`:

```bash
uv run reconcile --seed 0 --git-sha <sha from data/metrics.json> --out-dir data
```

`tests/test_reproduce.py` runs this for real — a genuine `git clone --local` into a temp directory, a genuine `uv sync`, a genuine `uv run reconcile` with `FIREWORKS_API_KEY` stripped from the subprocess environment — and asserts the resulting `metrics.json` is byte-identical to the one committed at `data/metrics.json` (NFR-01, NFR-06).

To regenerate the reference batch itself from scratch (not required to reproduce the pinned run — the batch is already committed per FR-12):

```bash
uv run generate --seed 0 --out-dir data/reference
```

To run the test suite, including the import guard and the clean-clone reproduce test:

```bash
uv run pytest
```

## Batch and reference dataset

The committed reference batch (`data/reference/*.jsonl`, seed 0, snapshot date 2026-08-28) is 150 reconciliation cases: 125 settlement-anchored, 25 orphan. Every population's size is fixed by §3.5/§3.6's case-allocation tables and asserted exactly in `tests/test_generator_batch.py` — not eyeballed. 1,392 recon lines, 5,418 ledger entries, 176 bank lines, 150 ground-truth cases.

Ground truth is emitted from the injection plan when each case is planted, never re-derived by inspecting the generated records (§3.5) — re-deriving would embed the pipeline's own matching logic into its answer key.

A second, hand-authored adversarial set (`data/adversarial/`, ten cases, no RNG, no generator import) targets four specific evidence boundaries the reference batch's random population doesn't stress-test on demand: `T-01` vs `T-03` template selection (REV-16), the family-4 timing triangle (core / date-error / no-op), duplicate-credit vs reversal (REV-18), and the ambiguous-vs-unmatched-inbound-credit split (§4.2's own stated Slot A boundary). Reported separately (`tests/test_adversarial.py`), never merged into any other metrics report — measured 10/10 state and exception-class agreement, first construction, nothing tuned.

A third, disjoint held-out batch (seed 2) is generated and run for §5.1's development-versus-held-out comparison; it is never inspected case by case and nothing is tuned against it (§5.1's rule, with teeth).

## Metrics and evaluation

The full §1.6 metric surface is computed against ground truth in `pipeline/metrics.py`; every rate carries its own integer numerator and denominator (never a bare float), and an undefined metric (zero denominator) reports as `undefined`, never as `0.0` — collapsing the two would flatter the system. Two §5.2 confusion matrices (outcome state, exception class), a per-subtype precision/recall breakdown over Slot A's seven graded subtypes, and the §5.5 provisional threshold review are in `pipeline/eval_report.py`; all five §1.8 report artifacts render into one self-contained HTML file (`pipeline/report.py`, FR-11).

**Measured at seed 0, the shipped `baseline` arm, nothing tuned in response to it.** (The same table on the `llm` arm is in the arm comparison below; it is worse on every line that differs.)

| Metric | Value | §5.5 target |
|---|---|---|
| `false_match_rate` | 0/150 = 0.0000 | 0 |
| `auto_close_precision` | 50/50 = 1.0000 | ≥ 0.98 |
| `auto_match_precision` | 30/30 = 1.0000 | ≥ 0.95 |
| `auto_close_recall` | 50/50 = 1.0000 | 0.80 – 0.95 |
| `auto_match_recall` | 30/30 = 1.0000 | 0.85 – 0.95 |
| `state_prediction_accuracy` | 150/150 = 1.0000 | 0.80 – 0.90 |
| `exception_subtype_recall` (macro, 7 subtypes) | 1.0000 | 0.70 – 0.85 |
| `exception_subtype_precision` (macro, 7 subtypes) | 1.0000 | 0.75 – 0.90 |
| `abstention_rate` | 17/150 = 0.1133 | 8 – 18% |
| `declined_by_policy_rate` | 17/150 = 0.1133 | ≈ 11.3% |
| `value_coverage` | 0.6246 | reported, no target |

**Read the perfect scores as a statement about the batch, not the system.** Every 1.0000 above is real and reproducible, and none of it is evidence the Controller is good — see the saturation note under the ablation below. The two numbers worth reading as results are `false_match_rate` (0, on a batch built to tempt false matches) and the fact that **80 of 150 cases close fully automatically — 30 `AUTO_MATCHED` + 50 `AUTO_CLOSED` — which is 100% of the population ground truth marks automatable, with zero false matches.** `match_rate` (30/150 = 0.20) counts only the no-adjustment half of that and is the §1.6 name for it, so it is reported under that name and not as a headline.

`false_match_rate` and `auto_close_precision` are the two primary safety metrics — both read exactly where §5.5 asks (0, and 1.00). `abstention_rate` reads below the 8–18% operating range on the Slot A arm (session 6.2's finding, reported rather than tuned — see the `MEASURED, NOT TUNED` note below). `auto_close_recall`/`auto_match_recall` reading ABOVE their bands means every ground-truth `AUTO_MATCHED`/`AUTO_CLOSED` case was actually caught, which is a stronger result than the band anticipated, not a defect.

**Development-versus-held-out gap (§5.1, seed 1 vs seed 2), Slot A arm:** the largest gap on a §5.5-targeted metric is `state_prediction_accuracy`, −0.0133 (two cases in 150), and every targeted gap is negative or zero — the held-out batch never scores better than the batch the prompt was written against. `false_match_rate` and `auto_close_precision` stay perfect on both batches; the model's errors are confined entirely to the non-money classification path. On the deterministic baseline, every §5.5-targeted metric has a gap of exactly zero, as §5.1 predicts for a path with no learned parameters. Full figures: `BUILDLOG.md`, session 6.2.

**§5.4 ablation — three arms, `exception_subtype` macro, seed 0.** The arm the CLI defaults to is the one that measures best, and on this batch that is the deterministic one:

| arm | macro precision | macro recall | state accuracy | LLM calls |
|---|---|---|---|---|
| `baseline` — triggers + keyword read | **1.0000** | **1.0000** | **150/150** | 0 |
| `hybrid` — triggers win; Slot A decides only the untriggered orphan split | 0.9333 | 1.0000 | 143/150 | 16 |
| `llm` — Slot A over all eight labels | 0.8012 | 0.8405 | 143/150 | 70 |

Held-out (seed 2) reproduces the ordering: `llm` −0.2044 precision / −0.1143 recall against `baseline`; `hybrid` −0.0612 / ±0.0000.

**Why Slot A loses, stated plainly.** Six of the seven graded subtypes have a deterministic §3.3 trigger, and `EvidenceBundle` — correctly, per invariant 1.7.2 — carries no UTR, no amount and no dispute flag. So the model cannot *check* six of the eight definitions it is asked to choose between; it pattern-matches narration text instead. The cost lands as false positives on cases whose ground truth is `ABSTAINED`: confident operational exceptions manufactured out of genuine ambiguity, which is the error direction §1.3 ranks worst. `hybrid` recovers all of the lost recall and most of the precision by scoping the model to the one split §4.2 actually reserves for it.

**The benchmark is saturated, and that is a finding about the batch, not a result.** The deterministic arm scores 1.0000 on state accuracy and on both macro subtype metrics at seeds 0, 1, 2, 5, 7 and 11, and the hand-authored adversarial set is 10/10. That is not evidence the system is good; it is evidence this generator plants *structurally unambiguous* anomalies — each family produces a distinct arithmetic signature, and REV-16 made the six template predicates mutually exclusive by construction, so evidence → label is a bijection with no irreducible ambiguity for judgment to resolve. A perfect score on a task where perfection is arithmetic proves as little as one cherry-picked match. Closing this is the first entry under Known limitations.

**MEASURED, NOT TUNED.** Every §5.5 threshold in this repository is stated as provisional in the spec itself, "set properly after the first real run against the development batch." No prompt, threshold, predicate, or classifier behaviour has been changed in response to any figure in this README, `BUILDLOG.md`, or the committed reports — §5.1's rule applies with teeth to whoever *acts* on a number, and nobody has.

**Performance (FR-02, NFR-02, NFR-03), measured on `Windows AMD64, Intel64 Family 6 Model 154 Stepping 4, GenuineIntel, Python 3.11.15`:** reference batch (150 cases) 605–656 cases/s; scale batch (seed 3, 362 cases) 299–310 cases/s.

## Repository layout

```
AI Settlement Close Controller/
├── spec.md                 # single source of truth
├── AGENT.md                 # standing brief every coding session opens with
├── BUILDLOG.md               # append-only, one entry per session (Built/Broke/Cut/Decided/Next)
├── README.md                 # this file
├── ARCHITECTURE.md           # component-level design notes
├── pyproject.toml            # uv-managed; `generate` and `reconcile` entry points
├── generator/                 # synthetic data + ground truth generator — separate entry point
├── pipeline/                  # the graded path — MUST NOT import generator/, ever
│   ├── cli.py                  # `uv run reconcile` — FR-10's single command
│   ├── run.py                   # components 2-8, composed end to end
│   ├── metrics.py / eval_report.py / report.py   # component 9 (§1.6, §5.2, FR-11)
│   └── ...                      # matcher, predicates, instantiator, validator, apply, classifier, narration
├── tests/
│   ├── test_import_guard.py    # static analysis: pipeline/ never imports generator/
│   ├── test_reproduce.py       # NFR-01/NFR-06: real clean-clone, real uv sync, byte-identical metrics
│   └── ...                      # one file per component/session
└── data/
    ├── reference/               # committed seed-0 reference batch (FR-12)
    ├── adversarial/             # hand-authored ten-case boundary set (§5.3)
    ├── llm_cache.json           # SHA-256-keyed Slot A / Slot B prompt-response cache (§4.3)
    ├── metrics.json             # the pinned run's MetricsReport (FR-13)
    └── report.html              # a sample FR-11 report from a real run (FR-12)
```

## Known limitations

- **The reference batch does not contain irreducible ambiguity, so it cannot discriminate between arms fairly.** A keyword matcher scores 1.0000 across six seeds; the LLM arm can therefore only lose. Until the generator plants cases whose correct label is genuinely undecidable from structure alone, the ablation measures the batch, not the arms.
- **Slot A is net-negative on every measured metric and is not the default.** It stays in the repository as the §5.4 comparator and as the honest record of a measured negative result — see the arm comparison above.
- **No FR-05.** `EXTERNAL_ACTION_REQUIRED` cases carry a categorized exception and a recommended next step (Slot B), but never a proposed recognition entry — §2.4's stated v1 fallback.
- **Orphan cases are unresolvable by construction (§3.6)** and cannot reach `AUTO_MATCHED` or `AUTO_CLOSED`; they are the majority of `EXTERNAL_ACTION_REQUIRED`'s population, which is why that state runs at roughly a quarter of the batch rather than reading as a defect.
- **The reconciled-ledger diff artifact shows only `CONTROLLER_ADJUSTMENT` rows**, not the full ~5,800-row ledger — a deliberate reading of FR-11's "diff," not a truncation.
- **`abstention_rate` on the Slot A arm reads below §5.5's 8–18% operating band** (session 6.2, reported and not tuned — see Metrics above).
