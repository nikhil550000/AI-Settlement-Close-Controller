# FAILURES.md

What broke during the build, how each defect was found, and what stops it coming back.

Two mechanisms produced this list, and both were in place before there was any code to break.

- **`BUILDLOG.md`** — one append-only entry per session, five fixed subheads: **Built / Broke / Cut / Decided / Next.** The **Broke** field is mandatory and written whether or not anything broke. Across twenty sessions, six record "nothing broke" and say why; that is what makes the other fourteen worth reading.
- **A locked design document with its own revision log** — a defect in the *specification* is tracked the same way as a defect in the code. Sections lock; once locked, a change requires a numbered revision stating its issue, its change, and the alternatives rejected. Twenty-five of them. Two standing rules produced most: anything contradicting a locked section stops the session and becomes a revision rather than a quiet edit, and if the design does not cover something, stop and ask instead of inventing scope.

Each incident below names **how it was caught**, because that is the part that generalises. Five mechanisms account for all of them: a failing test; a session checkpoint running real code against real storage for the first time; reading a rendered artifact instead of trusting an assertion; a deliberate adversarial pass over work already declared finished; and stopping to verify a locked claim before building on it. Only the first is automatic, and it caught the fewest.

---

## 1. `AUTO_CLOSED` was unreachable for all 50 cases

**What broke.** The idempotency rule put `UNIQUE(case_id, resolution_id)` on the `ledger_entry` table — a **row**-level constraint. But that table is defined as one row **per leg**, and every one of the six correction templates has two or three legs sharing a single `resolution_id`. Against the DDL, built exactly as written:

```
leg 0 (5010 Payment Gateway Charges) inserted OK
leg 1 (5020 GST on Gateway Charges)  REJECTED: UNIQUE constraint failed: ledger_entry.case_id, ledger_entry.resolution_id
leg 2 (1020 Razorpay Clearing)       REJECTED: UNIQUE constraint failed: ledger_entry.case_id, ledger_entry.resolution_id
```

Every correcting entry truncates to its first leg, fails the `sum(debits) == sum(credits)` check, and the case is downgraded. **No case in the batch reaches `AUTO_CLOSED`** — against a planned allocation of 50 such cases and roughly 70% of the build weight on that state. The headline capability of the product was arithmetically impossible.

**How it was found.** A checkpoint partway through the build, the first time a real template was instantiated and inserted against real SQLite. The storage session's own checkpoint had *passed*: `tests/test_storage.py` asserts the constraint rejects a duplicate `(case_id, resolution_id)` insert, which it does. There were no multi-leg entries in existence yet to test it with. The contradiction lived between two locked sections, so no single session's tests could see it.

**Root cause.** Not the invariant — "reprocessing the same case cannot double-post" is the right guarantee. The **granularity of its enforcement**: a per-row constraint on a per-leg table cannot express "this multi-leg entry was posted once."

**Fix and guard.** Demonstrated first, then the session stopped rather than working around it. Logged as a numbered revision (`v0.7 → v0.8`) with three candidate shapes put to the user; the chosen one widens the constraint to `UNIQUE(case_id, resolution_id, account_code)`, verified beforehand that no template posts the same account twice within one entry. The entry-level balance check and the `resolution_id` rule are unchanged. Two tests in `tests/test_apply.py` pin both halves so neither can be lost: `test_a_three_leg_entry_posts_all_three_legs` and `test_the_widened_constraint_still_rejects_a_duplicate_leg`. The revision records the rejected alternatives, including the textbook `resolution` header table — rejected for adding a table the spec names nowhere.

---

## 2. Nine ground-truth labels no component could ever reach

**What broke.** The nine settlement-anchored `AMBIGUOUS_CASE` cases were built early in the generator work as an internally-balanced contra-revenue ledger pair whose `reference` was a fresh `rfnd_*` id existing nowhere else in the batch. Measured: 18 entries, 9 distinct references, **0 resolving to anything**. That made the pair unresolvable — the intent, since a required piece of evidence is absent — but it also made it **unattributable**. `ledger_entry.reference == recon_line.entity_id` is the only join between the ledger and a case, so a reference resolving to nothing belongs to no case at all. Nine of 150 cases carried `ABSTAINED` ground truth that no component, deterministic or model, could have reached: `state_prediction_accuracy` would have carried a silent ~6% ceiling and `abstention_rate` could only have been hit by accident.

**How it was found.** A session's **Next** field flagged it as a gap; the following session checked the claim empirically before writing the predicate evaluator and found it was worse than a gap. Nothing was failing. The batch generated, the tests passed, and the number that would have been wrong was a metric nobody had computed yet.

**Root cause.** Not a design defect — the specification said nothing wrong. The generator's realization of them confused *unresolvable* with *unattributable*.

**Fix and guard.** The reference now names a real payment in the same settlement, with the amount still drawn independently of that payment's own. The case stays genuinely ambiguous — the books book a refund against a payment Razorpay's evidence says was never refunded, and `T-02` cannot fire because it requires the refund recon line that is precisely what is missing — while joining to its case like every other piece of ledger evidence. Verified after: all 9 reachable through the standard join, 0 dangling references batch-wide. Two existing tests had *pinned the defective construction* and were rewritten to pin the corrected one (`test_ambiguous_ledger_entry_is_uncorroborated_but_attributable`, `test_the_ambiguous_cases_phantom_pairs_survive_the_id_pass`), and `test_the_global_id_pass_keeps_every_cross_reference_resolvable` now asserts the dangling set is **empty** rather than tolerating it.

---

## 3. A generator defect found before the code that would have consumed it

**What broke.** `generator/orphans.py`'s `generate_noise_bank_lines` drew "unrelated NEFT" noise 50/50 credit/debit. A credit draw used the exact same `credit_narration()` / `NAMED_COUNTERPARTIES` construction as a genuine `UNMATCHED_INBOUND_CREDIT` case: same narration pool, same party pool, same amount distribution, no settlement anchor either way. Verified at seed 0 — 12 of 18 noise lines landed as credits, byte-identical in shape to the 8 real case lines, with **no field anywhere in `bank_line` that could tell them apart**.

**How it was found.** Immediately before writing case assembly, by reading the population the new component was about to consume. Case assembly is component 2 — fully deterministic, no model permitted — and its checkpoint is "150 cases assemble." That checkpoint could not have been hit honestly while the collision existed, and the failure would have presented as a component bug rather than a data bug.

**Root cause.** A generator implementation choice colliding with itself: two populations that must be separable, drawn from the same generator, with no signal in the schema to distinguish them.

**Fix and guard.** Surfaced to the user before writing anything; they chose to fix the generator rather than log a spec revision, since this was not a contradiction in spec text. "Unrelated NEFT" noise is now **always a debit** — an outbound payment to an unrelated party, which is what genuinely unrelated noise looks like on a real business bank statement, and which makes the population content-distinguishable without touching money or inventing a signal the schema doesn't carry. No population count changed. Credit-direction noise drops to exactly 6 lines — the self-matching reversal pairs' credit legs, which the reversal-pairing rule resolves on its own. **Honest caveat:** the guard here is the rule written into `generator/orphans.py`'s module docstring plus `test_noise_reversal_pairs_net_to_zero_and_share_a_utr`. No test asserts the direction rule directly, which is weaker than every other entry on this page.

---

## 4. The Fireworks 404, and the 401 that made it look like something else

**What broke.** The design's stated primary model did not exist on this project's actual account. `accounts/fireworks/models/llama-v3p3-70b-instruct`, `llama-v3p1-8b-instruct`, `llama4-scout-instruct-basic`, `qwen3-235b-a22b` and `kimi-k2-instruct` all returned an identical `404 Model not found, inaccessible, and/or not deployed`.

**How it was found.** By treating the design's own "Assumption" callout as a step rather than a caveat: *"the exact model ID MUST be re-checked against the live catalog before it is pinned."* That session smoke-tested the stated primary and four alternatives against the real key before wiring anything.

**Root cause, and the confound.** The 404 being **identical across three unrelated model families** rather than one is what turned this from a wrong-model-ID question into an account-level access question. That diagnosis was nearly derailed by a second, self-inflicted failure mode: an early debugging script parsed `.env` with a bare `key, _, value = line.partition("=")`, which does not strip surrounding quote characters. `.env`'s value is quoted, so every request from that script sent a literal `Bearer "fw_...` header and returned `401 The API key you provided is invalid` — while the *same key* through the `openai` SDK was returning 404s. Two different error codes from what looked like one cause. Root-caused by dumping the parsed value's length, prefix and suffix — never the full key — and finding the leading and trailing `"`.

**Fix and guard.** `gpt-oss-120b`, named by the user after checking the Fireworks dashboard directly, returned 200 immediately and is now `pipeline.llm_client.FIREWORKS_MODEL_ID`, verified end to end through the real `FireworksClient.complete` path including constrained decoding. The quote bug was in a throwaway debugging script, not in any committed module — `pipeline/llm_client.py` reads `os.environ` directly and never had it — so it is recorded here rather than "fixed" in code that was innocent. The substitution is visible in the artifact, not only in prose: the pinned model ID travels in `metrics.json`'s `provenance` block. The automated suite never touches `FireworksClient`; the real-account verification lives in a scratch driver, because a checkpoint that only passes with a live key would make the offline mode a lie.

---

## 5. The report rendered its own HTML as visible text

**What broke.** Jinja2's `autoescape=True` — on deliberately, so a model-generated narration containing `</td>` cannot break the table it sits in — escapes the string result of *every* `{{ expression }}`, including one calling a Python helper that returns raw HTML. `render_matrix` / `render_entries` / `render_validations` build the confusion matrices, audit-trail legs and validation checklists as literal `<table>...</table>` strings. Unwrapped, the confusion-matrix section of the shipped report rendered as `&lt;table&gt;&lt;thead&gt;...` — visible source text where the rendered artifact should have been. The same pass caught the second half: five `{{ ... else "&mdash;" }}` fallbacks round-tripping into visible `&amp;mdash;`.

**How it was found.** By opening the rendered file. **All eleven tests passed throughout**, because every one asserted that expected substrings were *present somewhere in the file* and none asserted that the matrix *rendered as a table*. The clearest example here of a green suite over a wrong artifact.

**Root cause.** A correct security default applied to content that is not untrusted, with no test distinguishing "the string is in the file" from "the browser draws a table."

**Fix and guard.** The three template globals are wrapped in `markupsafe.Markup(...)` at registration. What they build is entirely the module's own — chart-of-accounts names, enum values, generator-minted IDs, validator check text — never a narration or any other model-generated string, so marking it trusted does not reopen the hole autoescaping exists to close. `test_no_html_is_double_escaped` pins both halves: the matrix renders as a real `<table>`, and neither `&lt;table&gt;` nor `&amp;mdash;` appears anywhere.

---

## 6. Six defects found by an adversarial pass over finished work

Commits `2ae71c1` and `181b1da`. The repository was feature-complete, the suite green at 543 tests, the reproducibility run pinned, the README written. A deliberate pass reading the shipped artifacts as an unsympathetic reader found six. Four are one story: **the repository was shipping, pinning and documenting the arm that measures worst.**

**a. The audit trail showed one of six safety validations, and the least meaningful one.** Every `AUTO_CLOSED` case in the shipped `report.html` read `PASS not_previously_posted: already posted identically by a previous run; replayed, not re-posted`. There had been no previous run. `run_batch` applies twice when a classifier is given; idempotency makes the second pass *replay*, and `apply_case`'s replay short-circuit emits a `ValidationReport` carrying only `NOT_PREVIOUSLY_POSTED` without re-running `validate_candidate`. So `entry_balanced`, `post_adjustment_residual_zero`, `account_direction_permitted`, `template_allowlisted` and `cited_records_exist` had **zero occurrences in the shipped HTML** — the artifact required to show "the specific safety validations passed" was showing one check, and the one least able to support the claim. Found by reading the rendered report. Fixed by `_carry_forward_validations` in `pipeline/run.py`, which restores the checks from the pass that actually posted wherever the second pass produced nothing but the replay marker, leaving every other field untouched. All six now render.

**b. The CLI defaulted to the arm that loses.** `--classifier` defaulted to `llm`. At seed 0: `baseline` 150/150 state accuracy and 1.0000 on both macros; `llm` 143/150 and 0.8012 / 0.8405. Shipping the losing arm as the default contradicted the one thing this build claims as its position. Now defaults to `baseline`, with the criterion named in the CLI's own help text.

**c. `metrics.json` contained a number that contradicted itself.** The two macro averages were typed as `Rate`, which carries a numerator and denominator, producing a committed artifact reading `{"numerator": 7, "denominator": 7, "value": 0.80}` — where 7/7 is 1.00. A mean of ratios is not a ratio. `MacroRate` replaces `Rate`; the counts travel as `subtypes_averaged` / `subtypes_eligible`, which is what they always meant, and the visible-denominator rule is still met by the per-subtype table.

**d. The one documented command died on any batch it had not seen.** `uv run reconcile` against any batch outside the committed cache raised an unhandled `CacheMissError` from Slot B — a path the README itself invites, one line above, via `uv run generate --seed <n>`. See *Engineered fallbacks*.

**e. Two README claims were inverted, not imprecise.** It said Slot A "recovers seven more `UNMATCHED_INBOUND_CREDIT` cases the baseline misses" and framed the ablation as a recall/precision trade. Both arms have `UNMATCHED_INBOUND_CREDIT` recall of exactly **8/8**; Slot A recovers zero true positives. The seven are ground-truth `ABSTAINED` cases it wrongly promotes to confident exceptions — the same seven the eval report had been printing all along as `7 × ground truth ABSTAINED → predicted EXTERNAL_ACTION_REQUIRED`. The report was right and the README describing it was backwards. Replaced with the three-arm table and the mechanism: the evidence bundle, correctly, carries no UTR, no amount and no dispute flag, so the model cannot *check* six of the eight definitions it must choose between. Also corrected `--llm-cache` to `--cache-mode`, a flag that never existed under that name.

**f. Two capability-bearing tools were gitignored.** `build_adversarial_set.py` and `measure_performance.py` sat under `/scratch/` while producing claims the repository makes. Moved to `tools/`, because nothing capability-bearing belongs outside the repository.

Fixing (b) forced a re-pin of the reproducibility run (`181b1da`), where the four stories converge into visible numbers: `state_prediction_accuracy` 143/150 → 150/150, `abstention_rate` 0.0667 → 0.1133 (into the 8–18% target band), `predicted_state_counts` `EXTERNAL_ACTION_REQUIRED` 43 → 36 and `ABSTAINED` 10 → 17 — those seven cases again. `tests/test_reproduce.py` re-verified green: real `git clone --local`, real `uv sync`, real `uv run reconcile` with `FIREWORKS_API_KEY` stripped, `metrics.json` byte-identical.

---

## 7. The benchmark is saturated, and the system cannot survive a vocabulary change

The least flattering item here, and the most important.

**What broke.** The deterministic arm scores **1.0000** on state accuracy and both macro subtype metrics at seeds 0, 1, 2, 5, 7 and 11, and 10/10 on the hand-authored adversarial set. A perfect score across six seeds is not a result; it is a symptom.

**How it was found.** An adversarial pass asked why, and found the mechanism: five of the pipeline's decision boundaries are keyword lists whose coverage of the generator's own string pools is 100% hit / 0% miss **by construction** — `_RAZORPAY_MARKER`, `_REVERSAL_KEYWORDS`, `_BANK_CHARGE_KEYWORDS`, `_BANKING_BOILERPLATE_WORDS`, `_TAX_POSITION_MARKERS`, against `SETTLEMENT_PARTIES`, `REVERSAL_TEMPLATES`, `_BANK_CHARGE_NARRATIONS`, `OPAQUE_CREDIT_NARRATIONS`, `TAX_SIGNATURES`. The import guard cannot see this — a data coupling is not an import. A seed sweep cannot see it — every pool is a module constant a seed does not vary. **No test in the suite feeds the pipeline a string the generator could not have written**, which is exactly why 543 of them pass over it.

**Then it was measured, not asserted.** `tools/heldout_vocabulary.py` builds a disjoint surface vocabulary — real Indian bank-statement and settlement-adjustment wording, chosen so every replacement stays decidable by a competent reader while sharing no literal with those five lists (`RZRPAY SOFTWARE PVT LTD`, `SUSPENSE-CR`, `SUNDRY RECEIPT`, `CR CANCELLED-{ref}`). `tools/build_heldout_vocab_batch.py` rewrites a batch onto it **by template, not by search-and-replace**: each narration is matched against the generator's own `ALL_TEMPLATES` in the same precedence `narration_template` uses, its slots extracted, and the held-out template at the same index re-rendered with `{ref}` verbatim so the tier cascade sees the identical token. `ground_truth.jsonl` and `settlements.jsonl` are copied byte-for-byte, so nothing in the apparatus can move a label. Structure identical, answer key identical, surface changed. Committed at `data/heldout_vocab/`.

**The result.** The deterministic arm does not degrade on that batch. **It cannot complete a run at all.** `_RAZORPAY_MARKER` no longer separates the gateway from a merchant, case assembly mis-splits the 125/25 populations, and the run terminates with

```
MetricsError: ground-truth case 'orphan_53fdba0a' matches no assembled case
```

Failing loudly is the correct behaviour and is to `align_ground_truth`'s credit. Scoring 1.0000 on the batch where the same code scores nothing is the finding.

**Reported, not buried.** The README states it in the ablation section — "the benchmark is saturated, and that is a finding about the batch, not a result" — and it is the first entry under Known limitations. Against the same 23 held-out strings, a live constrained-decoding call framed as *extraction* rather than 8-way classification ("name the counterparty, or null") scores 23/23: null for all four re-spaced gateway names, null for all six suspense/clearing strings, null for all five charge lines, and the exact name for all eight merchants. That is the generalisation the README used to claim for Slot A without evidence, measured for the first time on strings the generator could not have written.

---

## 8. Rs 12,693.20 of bank credit went missing, and no test could see it

**What broke.** `data/contested/` shipped with four bank credits — **Rs 12,693.20** — attached to nothing. Not mis-attached: absent. They appeared in no case, no metric and no line of the report.

**Why every individual decision was correct.** The credits narrate the gateway, so `assemble_orphan_cases` correctly declined to treat them as orphans — a credit from Razorpay is presumptively spoken for by a settlement. Tier-2 demotion then correctly dropped them from every settlement that had claimed them, because a tie is not a match. Each rule did exactly what it should. The money fell into the gap between two correct decisions.

**Why the suite was blind to it.** Every graded metric is denominated in **cases**. A bank line that reaches no case is invisible to all of them, in both directions: it cannot lower a rate, and it cannot raise one. 602 tests, six seeds, four committed batches, and not one assertion was denominated in the unit the loss occurred in. It was found by *reading* — an adversarial pass over the finished contested-credit work — and written into the README as a known limitation, which is where it sat.

**Why it is the most dangerous class of defect here.** A wrong journal entry is loud: `false_match_rate` moves, `auto_close_precision` moves, a confusion matrix goes off-diagonal. Money that silently leaves the denominator moves nothing at all. In a finance product that is the failure mode that survives to production, because every dashboard still reads green.

**The fix is a unit, not a rule.** `pipeline/bank_accounting.py` partitions every bank line into exactly one disposition — settlement evidence, orphan evidence, contested-unawarded, bank charge, self-matched reversal, outbound noise — with `unaccounted` as a seventh bucket that must always be empty. The contested credits now land in a named bucket with their rupee value, and `Case.contested_bank_lines` carries the line back onto each settlement that claimed and lost it: visible to the report, invisible to everything that prices a case, so no downstream reader gains evidence the matcher just ruled inadmissible.

It changes no graded metric. `data/metrics.json` gained one field and not a single existing figure moved.

**The guard.** `tests/test_bank_accounting.py::test_the_partition_is_total_on_every_committed_batch` asserts, over all four committed batches, that the disposition counts sum to the bank-line count and `unaccounted` is empty. A future change that makes a line reachable by no rule turns that red instead of quietly removing money from the batch. `test_noise_dispositions_are_read_from_case_assemblys_own_rules` pins that each noise bucket actually fires, so a partition that classified everything as one disposition cannot pass.

**What it also bought.** The contested ablation now has a second denominator. The grounded model read returns **Rs 7,323.00** of bank credit to a settlement that the keyword arm leaves unattached, and the credits it still cannot place are a strict subset of the keyword arm's. Two cases was a thin number; the same result in money is not.

---

## The rest

| What broke | How it was found | Fix and guard |
|---|---|---|
| **`match_rate` was defined as recall** over the auto-matchable population but named as the classic finance batch-level rate. Reporting recall under a name finance reviewers read as match rate overstates the headline. | Reading the locked metric definitions against their own stated meaning, before any metric code existed. | Redefined with total cases as denominator; the original formula kept under the accurate name `auto_match_recall`. Both reported. A later revision caught the identical mistake in `auto_close_rate`. |
| **The macro-average denominator disagreed with itself.** It was stated as **six** subtypes in the population table and its supporting clause, and **seven** elsewhere. Six cannot divide the 36 cases the allocation table assigns across seven; excluding `DISPUTE_PENDING` leaves 31, so the sentence was false under either count. Moves the headline for the one graded LLM slot by up to a seventh. | Hand-checking every metric denominator against the population table's population table before computing anything. | Seven is normative; the two descriptive restatements corrected. `test_macro_average_denominator_is_seven_and_numerator_is_the_defined_subtypes`, `test_macro_average_never_includes_ambiguous_case`. |
| **`T-01` and `T-03` both fired** on all 10 family-3 cases. Selection is deterministic, so a double fire had no defined resolution; if `T-01` won, the wrong entry still *balances*, so the debit-equals-credit check passes it. | Reading the template definitions in full before implementation. | `T-01` gains a positive conjunct making the two mutually exclusive per record, plus a rule that the pipeline assert at instantiation that at most one predicate fires per `(case_id, entity_id)` — a double fire is a hard error, not a precedence question. |
| **`largest_gap` was decided by float noise.** Two metrics move by the same two cases in 150, so their gaps are equal — but as raw floats they differ in the sixteenth decimal (`-0.013333333333333308` vs `-0.013333333333333329`) and a plain `max` reported whichever way the subtraction rounded. A headline figure flipping on a rounding artefact. | A checkpoint test expecting `state_prediction_accuracy` failed when `max` picked `abstention_rate`. | `largest_gap_among` compares at 6 dp — finer than one case at 0.0067, coarser than the noise — and breaks ties toward the metric surface's stated order. `test_a_tie_between_two_gaps_is_broken_by_order_not_by_float_noise` pins both halves, including that the raw floats really are unequal. |
| **The orphan case-ID join.** `compute_metrics` failed on its first real run with 25 unscored and 25 unlabelled cases: ground truth says `orphan_1f617519`, the assembled case says `case_orphan_bank_0442bb20`. | The error's own output. Second time this seam bit — an earlier session records the same two ID schemes defeating a test helper. | `align_ground_truth` joins through each case's record IDs. The first fix lived in one test file; this one is production code on the graded path. Two new tests that had reproduced the bug by hand were corrected, not the code. |
| **The committed cache did not cover the pinned arm.** Earlier work moved seven `ABSTAINED` cases into `EXTERNAL_ACTION_REQUIRED`, so a strict run of the pinned arm asked Slot B for narrations the cache had never seen — `CacheMissError`. | By actually trying to build the CLI's default pinned run **offline**, rather than assuming it from the arithmetic. | A real `--cache-mode refresh` pass, verified by two consecutive offline strict passes. |
| **`UnicodeEncodeError` on the one documented command.** `pipeline/cli.py`'s summary printed characters outside Latin-1 (a section sign, a multiplication sign, an em-dash) which this machine's default console codepage (cp1252) cannot encode. | The first time *committed* code printed those to a real console. Sessions 6.3 and 7.1 had hit the identical trap in gitignored scratch scripts and correctly shrugged it off — that shrug expired the moment committed code did it. | A guarded module-level `sys.stdout/stderr.reconfigure(encoding="utf-8")` in `pipeline/cli.py`. "The CLI is the product" means it works out of the box, not after a judge sets an environment variable. |
| **The ledger-entry estimate (~3,540) could not be reconciled** with the locked four-leg posting. | The generator's first actual full generator run measured it: 1,449 payment-type recon lines produced 5,774 entries, 3.98 legs/payment. Caught only then because no earlier session generated the full 125-settlement population at once. | Corrected to ~5,800 entries, ~7,600 raw records. Surfaced to the user, who chose a formal revision over a loose estimate. No case count, template or invariant affected. |
| **Fingerprint z-scores, and the obvious explanation being wrong.** The ledger-date ordering check failed at z ≈ 16; the first reading blamed the family-4 date-error population, but excluding it moved z only from 15.9 to 15.1. | A statistical checkpoint failing, then refusing the first plausible cause. | Real cause: a payment's four legs share its capture date, so the check was measuring how tightly a *case* clusters — a true fact about settlement bookkeeping, not leakage. Replaced with earliest posting date **per case**: z ≤ 2.8 across forty seeds. Separately `case_id` ordering hit z = 4.59 on seed 5 because `orphan_*` sorts below `setl_*`; split into one check per namespace, worst z afterwards 2.47 / 2.24. |
| **Six test assertions were wrong and the code was right** (spread across four sessions, two of them fixture bugs). | Failing tests whose failures were diagnosed rather than silenced. | Recorded every time, because the alternative — editing code until the test passes — is what produces a green suite over a broken system. |
| **`uv run generate` failed: `error: Failed to spawn: generate — program not found`.** | First attempt to run the CLI. | No `[build-system]` table, so `uv sync` never installed the project and `[project.scripts]` was silently inert. Added `hatchling` pointed at both packages. |
| **`Settlement.amount` went negative** (`amount=-603920`) because a family-5 debit adjustment was drawn from the same distribution as payments. | `test_family_batch_holds_exactly_ten_cases[family_5]` failed immediately. | Capped at half the settlement's net-of-payments total — a real deduction cannot exceed the settlement it comes out of. The same class was caught pre-emptively in 2.2, where `SETTLEMENT_AMOUNT_MISMATCH`'s randomly-signed delta became always-positive. |

---

## Engineered fallbacks

The graceful-degradation paths that exist by design. Several were added *because* of the incidents above.

**Slot A degrades to the deterministic baseline.** `classify_case_llm(..., on_cache_miss="fallback")` returns `classify_case_baseline(bundle)` instead of raising, and the result carries `source = KEYWORD_BASELINE` or `DETERMINISTIC_TRIGGER` rather than `LLM_SLOT_A`. The audit trail records *how* a label was reached, so a degraded run is visibly degraded rather than silently different.

**Slot B degrades to `deterministic_narration`** — prose assembled from facts the deterministic path has already fixed, carrying `model_generated=False`. That field widened from `Literal[True]` to a **real bool** in `2ae71c1`: it had been a type that could not express the thing it was named for, which made the per-string labelling requirement unfalsifiable. It now distinguishes the two cases in the rendered report.

**The strict contract is unchanged; only the CLI opts out of it.** `CacheMode.STRICT` never constructs a network path, and a cache miss is a `CacheMissError`: a hard error rather than a fallthrough to the API. That keeps the network off the eval path and makes the offline mode real rather than nominal, and it remains the default for library callers (`on_cache_miss="raise"`), pinned by `test_strict_on_a_miss_raises_cache_miss_error_not_a_network_call` in both slots' test files and `test_strict_run_with_an_incomplete_cache_fails_loudly`. The CLI passes `"fallback"`, because that rule exists to protect the *measurement* and was never a reason for the product's one documented command to be brittle.

**Abstention is a designed terminal state, not a failure.** `ABSTAINED` is one of the five terminal states, with its own ground-truth population (17 of 150), its own metric, and its own operating band (8–18%). Declining a case is a correct outcome, graded as such. This is what caught defect 6(b) from the other side: the Slot A arm reads 0.0667, *below* the band, because its eight-value output space has no "insufficient evidence in the fields provided" option, so a forced choice becomes a confident one. The band is where the metric surface caught the model converting ambiguity into confident exceptions — the error direction this build ranks worst.

**Policy refusal is separated from low confidence.** A revision split `declined_by_policy_rate` from `declined_by_confidence_rate` so that deliberate scope discipline (tax positions; date-only reclassification across a period boundary) is not reported as weak detection. At the pinned run: 17 policy, 0 confidence.

**A pre-committed cut order.** The standing brief fixed what gets dropped if the build falls behind, mechanically rather than improvised: (1) drop family 5, removing 10 cases and templates `T-05`/`T-06`; (2) drop the third bank format profile; (3) reduce the reference batch to 100 cases — last resort, cheapest to cut and most expensive to lose, since every metric denominator shrinks with it. Tags `phase-1` … `phase-7` are the recovery points that order assumes. Nothing was cut in the end, which is the only reason this reads as unused rather than as the reason the build shipped.

**The stop-and-ask rule.** *Do not invent scope; if the spec does not cover it, stop and ask.* Incidents 1, 2, 3 and the ledger-estimate revision all reached the user as a question with options rather than as a unilateral workaround. Twenty-five numbered revisions is the cost of that rule; `AUTO_CLOSED` being reachable is what it bought.
