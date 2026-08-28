# Architecture

Component-level design notes. `spec.md` §4 is the source of truth for the decisions below; this file is where they live once built, cross-referenced to the module that implements each one.

## Component overview

Ten components (§4.1), one direction of data flow, no cycles. The generator is a separate entry point, not a pipeline stage.

| # | Component | Job | Module |
|---|---|---|---|
| G | Generator | Emits reference / held-out / scale batches plus ground truth from a seed | `generator/` |
| 1 | Adapters | FR-08 declarative column mapping → canonical `bank_line`; loaders for the other three schemas | `pipeline/adapters/`, `pipeline/loaders.py` |
| 2 | Case assembly | Recon lines grouped by `settlement_id` → settlement-anchored cases; residual bank lines → orphan cases | `pipeline/case_assembly.py` |
| 3 | Matcher | FR-09 four-tier cascade, T+2 window, integer-paise residual | `pipeline/matcher.py` |
| 4 | Predicate evaluator | The six §3.4 evidence predicates plus the `OPERATIONAL_EXCEPTION` subtype triggers | `pipeline/predicates.py` |
| 5 | Classifier | Exception class and subtype assignment — the one component carrying a model | `pipeline/classifier.py`, `pipeline/exception_class.py` |
| 6 | Instantiator | Template → candidate JV; deterministic amount derivation, zero-leg omission, aggregation | `pipeline/instantiator.py` |
| 7 | Validator | The invariant 1.7.5 chain plus both §3.4 validation layers | `pipeline/validator.py` |
| 8 | Apply and re-reconcile | Ledger write under 1.7.4's idempotency constraint, residual recheck, terminal state | `pipeline/apply.py`, `pipeline/reconciliation.py` |
| 9 | Reporter | Metric surface, §5.2 matrices, five §1.8 artifacts, single-file HTML | `pipeline/metrics.py`, `pipeline/eval_report.py`, `pipeline/report.py` |

`pipeline/run.py`'s `run_batch` composes components 2–8 into one call, taking already-loaded records — where they came from (the committed reference dataset, a raw bank export, or a test fixture) is the caller's business, never `run_batch`'s. `pipeline/cli.py`'s `reconcile` command (FR-10) is the one caller that matters for a reviewer: it loads component 1's output from `data/reference/`, calls `run_batch` with a classifier, then components 9's `build_eval_report`/`render_report_html`.

**Components 4 and 6–8 are the invariant-bearing core.** A predicate overlap, a wrong amount derivation, a validator that passes when it shouldn't, or a non-idempotent apply are all undetectable from output alone — which is why REV-16 (predicate overlap), REV-24 (idempotency constraint granularity) and the Phase 4 checkpoint's explicit "no cut" rule all concentrate here.

## Data model

Four canonical schemas (§3.1), Pydantic v2, JSONL on disk: `Settlement`, `ReconLine`, `LedgerEntry`, `BankLine`. `GroundTruthCase` (§1.6) is a fifth, evaluation-only schema — never fed to the pipeline, only read by component 9 to grade it.

**Money is `int` paise everywhere it can affect a decision.** `decimal.Decimal` with `ROUND_HALF_UP` appears exactly once, inside the generator's fee/GST rounding, and is cast to `int` before it leaves that function (NFR-04). Nothing under `pipeline/` performs float arithmetic on a rupee value; `pipeline/money.py`'s `rupees_string_to_paise`/`paise_to_rupees_string` parse and render bank-export rupee strings with pure integer/string operations.

**The unit of state is the reconciliation case (§1.2), not a raw record.** `pipeline.case_assembly.Case` is built in one of two ways: settlement-anchored (`case_id == settlement.id`, every recon line grouped by `settlement_id`), or orphan (residual bank lines the matcher and case assembly's own narration/reference-token classification could not attach to a settlement). Component 3 fills in `match_tier`, `residual_paise`, `in_settlement_window` on top of assembly's output via `model_copy` — orphan cases pass through untouched, since FR-09's cascade is only meaningful against a settlement anchor.

**The chart of accounts (§3.2) lives in exactly one place, `pipeline/accounts.py`**, and is re-exported by the generator rather than duplicated — the same reasoning `pipeline/timing.py` follows for the T+2 window calculation. Two independent copies of either would eventually drift, silently invalidating every template-allowlist check that depends on them agreeing.

**The six §3.4 templates are a fixed allowlist**, `TEMPLATE_LEG_ACCOUNTS` in `pipeline/instantiator.py`: (debit accounts, credit accounts) per template, checked twice — once when a candidate is instantiated, once more independently in `pipeline/validator.py` (transcribed rather than derived from the instantiator's own table, since deriving a check from the thing it checks is decorative). `T-01` and `T-03` stay separate templates despite sharing a debit leg specifically so the *account choice* stays in classification (gradeable) rather than moving into instantiation, where invariant 1.7.2 forbids the model from making it.

## Deterministic versus LLM slots

Invariant 1.7.2 permits the model to classify which template applies. It does not have to use that permission, and this build doesn't: §3.4 already makes the six evidence predicates formal, deterministic, and (per REV-16) mutually exclusive, so template selection is arithmetic, not classification — a model layered on top of a function that already has the answer can only introduce disagreement.

The model earns its place in exactly two slots, both bounded by invariant 1.7.2's line: it may classify and it may write prose; it may never originate an account, an amount, or a narration on the automated path.

**Slot A — exception subtype classification, non-`AUTO_CLOSED` cases. LLM. Graded.** `pipeline/classifier.py`. The one place in the system where the correct answer is not derivable from arithmetic: `UNMATCHED_INBOUND_CREDIT` versus `AMBIGUOUS_CASE` turns entirely on whether free-text narration identifies a counterparty, which no residual computation decides. The model receives a structured `EvidenceBundle` — case kind, fired triggers, whether any template hit, bank-line narrations, match tier — and returns one value from an eight-value enum (`SubtypeLabel`: the seven `OPERATIONAL_EXCEPTION` subtypes plus `AMBIGUOUS_CASE`) under constrained decoding. It never sees or emits an account, an amount, or a postable narration. Graded by `exception_subtype_precision`/`recall`, per subtype and macro (§5.2). A deterministic keyword baseline (`classify_batch_baseline`) is built first and doubles as the §5.4 ablation arm — falling behind degrades to a disclosed baseline, never to nothing.

**Slot B — resolution text, abstention rationale, per-case reasoning prose. LLM. Ungraded, off the money path.** `pipeline/narration.py`. Free text over facts the deterministic path has already fixed, restricted to the two states that need it (`EXTERNAL_ACTION_REQUIRED`, `ABSTAINED`). `CaseNarration.model_generated` is a fixed `Literal[True]` field carried as data, and `pipeline/report.py` renders it as a visible `model-generated` badge — FR-11's labelling obligation held as a schema fact rather than a convention a future session could forget.

**Slot C — FR-06 policy-exclusion detection. Deterministic in v1.** `pipeline/policy.py`. The predicate proved sufficient against the reference batch; the LLM alternative §4.2 named as a fallback was never needed.

**Everything else is deterministic**: adapters, case assembly, the full FR-09 cascade, all six evidence predicates, template instantiation, amount derivation, the validator chain, ledger apply, re-reconciliation, and every §1.6 metric.

## Determinism and reproducibility

Three layers make NFR-01 literally true rather than approximately true (§4.3):

1. **Constrained decoding to Slot A's eight-value enum** (`_SLOT_A_JSON_SCHEMA`, Fireworks' `response_format: json_schema`) bounds nondeterminism to a closed output space even under model drift.
2. **A SHA-256-keyed prompt/response cache, committed to the repository** (`pipeline/llm_cache.py`'s `PromptCache`, `data/llm_cache.json`). The key is the hash of the exact prompt string — dumb by design, so it knows nothing about `SubtypeLabel` or `CaseNarration` and survives a prompt revision without a schema migration. `CacheMode.STRICT` (the eval path's mode, and `pipeline/cli.py`'s default) makes a cache miss a hard `CacheMissError` rather than a fallthrough to the network; `CacheMode.REFRESH` is the only mode that ever constructs a `FireworksClient` or reaches the internet.
3. **The cache is committed alongside the pinned run (FR-13), and its hit rate is reported in the metrics JSON** (`RunProvenance.cache_hit_rate`).

The determinism claim is not "the API is reproducible" — no inference provider guarantees bitwise reproducibility across batching and kernel scheduling — it is "the response, once cached, is never asked for again." `tests/test_cli.py::test_two_runs_against_the_committed_data_produce_byte_identical_metrics` checks this in-process; `tests/test_reproduce.py` checks it across a genuine second checkout (see below).

**Nothing under `pipeline/` reads a clock or constructs unseeded randomness.** The batch snapshot date is a parameter everywhere it is used (the matcher's T+2 window, the posting date of every correcting entry, `pipeline/report.py`'s own docstring: "there is no 'generated at' timestamp"). `PerformanceMetrics` (throughput, latency) is deliberately a separate model from `MetricsReport`, specifically so the committed `metrics.json` — which NFR-06 requires reproduce byte-identically — never has to carry a wall-clock figure that by definition cannot.

**The FR-13 pin** — generator seed, git SHA, Fireworks model ID, the metrics JSON itself — is `RunProvenance` (`pipeline/metrics.py`), every field caller-supplied and `None` by default. Nothing in `pipeline/` ever reads `git rev-parse` itself: a SHA read from the working tree at metric time would name a different commit from the one the committed run actually reflects, so it travels in as a CLI flag (`reconcile --git-sha`), supplied by whoever is doing the pinning, not derived.

**`tests/test_reproduce.py` is the literal Phase 7 checkpoint**: a real `git clone --local` of this repository into a temp directory, a real `uv sync` there from the committed `uv.lock`, a real `uv run reconcile` subprocess with `FIREWORKS_API_KEY` stripped from its environment, diffed byte-for-byte against the committed `data/metrics.json`. Every other NFR-05 checkpoint in this codebase discharges its claim against a real artifact rather than a stub; this is that same discipline applied to "clean clone," which two runs in the same process cannot stand in for.

## Storage

**SQLite** for the synthetic ledger (`pipeline/storage.py`), raw `sqlite3`, no ORM — chosen specifically because `UNIQUE(case_id, resolution_id, account_code)` turns invariant 1.7.4's idempotency guarantee into a schema-level constraint rather than an application check. `(case_id, resolution_id)` alone (the original spec text) is a *row*-level constraint on a table that is one row per *leg*; since no §3.4 template posts the same account twice within one entry, widening to include `account_code` (REV-24) makes every leg unique by construction while still rejecting a second attempt to post the identical correction. SQLite's NULL-distinctness means `manual`/`erp_import` ledger rows (`case_id = resolution_id = NULL`) never collide with each other under this constraint — it only ever fires on a real, already-posted `(case_id, resolution_id, account_code)` triple.

The database is opened `:memory:` for every run `pipeline/cli.py` drives — there is no persistent ledger file to gitignore beyond the pattern already in `.gitignore` (`*.sqlite`, `ledger.db`) for anyone who points `connect()` at a real path. Everything the report needs after a run is read back once via `fetch_ledger_entries`, in insertion order.

## Evaluation harness

`pipeline/metrics.py` computes the full §1.6 surface against ground truth. Every metric is a `Rate` (numerator, denominator, and the ratio of the two as a property) — never a bare float — and a zero denominator produces `value=None`, not `0.0`, because "no case was predicted `DUPLICATE_CREDIT`" and "every case predicted `DUPLICATE_CREDIT` was wrong" are different findings that a bare zero would collapse into one.

**`align_ground_truth` is the join a reader is most likely to need and least likely to guess.** The generator mints orphan case IDs as `orphan_<hex>`; case assembly, having never seen the generator (§4.1's import guard forbids it), independently synthesizes `case_orphan_<lowest line_id>` from the bank lines it groups. A settlement-anchored case joins trivially on `case_id`; an orphan case has no ID both sides can derive independently, so the join goes through `expected_linked_source_records` instead — the bank-line IDs both sides observe — and is strict on every ambiguity (an unresolvable line, a line resolving into the wrong population, two ground-truth cases claiming one assembled case).

`pipeline/eval_report.py` builds on top: `ConfusionMatrix` (rows ground truth, columns predicted, everywhere in this codebase) for §5.2's two matrices — outcome state and exception class — the per-subtype precision/recall table, the §5.5 provisional threshold review (`PROVISIONAL_THRESHOLDS`, transcribed from the spec table, changing one is a Section 8 revision, not an edit here), and the §5.1 development-versus-held-out comparison (`compare_reports`/`BatchComparison`), which prints the gap as a finding rather than explaining it away.

`pipeline/report.py` renders all of it — plus the case log, the audit-trail drill-down, the exception report, and the reconciled-ledger diff — into the one self-contained HTML file FR-11 requires: inlined CSS, an embedded JSON blob, vanilla JS that only toggles row visibility (all rendering is server-side Jinja2, so the checkpoint — "opens from `file://` with networking off" — can be asserted directly against the file's bytes with no browser in the loop).

`pipeline/cli.py`'s `reconcile` command is components 2–9 wired end to end against a batch on disk, the thing FR-10 calls "the product": one command, a console summary (component 9's plain-text rendering), and both `report.html` and `metrics.json` written to `--out-dir`.
