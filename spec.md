# AI Settlement Close Controller — Specification

**Project:** Razorpay AI Buildathon 2026, Track 4 (AI Finance Controller)
**Build type:** Solo
**Submission deadline:** September 5, 2026 — confirmed from the Buildathon page itself ("You have from today till 5 September"). **Internal ship target: September 3, 2026**, with Sept 4–5 held as contingency only. No work is planned into the contingency window.
**Today:** August 27, 2026 — **7 days to internal ship target (Sept 3), 9 days to confirmed deadline (Sept 5)**
**Spec version:** v0.7
**Spec status:** Sections 1–6 locked. Sections 4 (architecture and tooling), 5 (evaluation methodology) and 6 (phase plan) written and locked in v0.5; §6.3 (session decomposition) and §6.4 (version control protocol) added in v0.6; §2.2's ledger-entry and raw-record-total estimates corrected in v0.7 (REV-23). Section 7 remains pending; §2.10 holds the non-goals list. Eight revisions to locked sections logged as REV-16 → REV-23. **Build starts Aug 28.**

---

## 0. How to read this document

This spec is the single source of truth for the build. Every implementation decision must be traceable to something in here. Sections are written in order (1 → 7) and each section is discussed, revised, and locked before moving to the next.

**Conventions:**

- `> Assumption:` marks a decision made without explicit user input, standing until challenged.
- `FR-XX` and `NFR-XX` are used for functional and non-functional requirements starting in Section 2.
- Money is always in integer paise unless explicitly annotated with a rupee unit.
- Once a section is locked, changes require an explicit revision note logged in Section 8.

**Version history:**

- **v0.1** *(Aug 24, 2026)* — Section 1 locked. Sub-direction: AI Settlement Close Controller.
- **v0.2** *(Aug 27, 2026)* — Section 2 (MVP scope) locked. Section 8 created. Three revisions to locked Section 1 logged as REV-01 → REV-03. Deadline corrected against primary source.
- **v0.3** *(Aug 27, 2026)* — Section 3 started (Step 3). Sub-topics 3.1 (canonical input schemas) and 3.2 (chart of accounts) locked, verified against live Razorpay API documentation. REV-04 logged: FR-01a's recon-line field list corrected against primary source. Sub-topics 3.3–3.6 continue in the next chat.
- **v0.4** *(Aug 27, 2026)* — Section 3 completed and locked: 3.3 (close-pattern taxonomy), 3.4 (templates and posting-direction rules), 3.5 (generation strategy), 3.6 (orphan cases). Preceded by a full verification pass over v0.3, which found fifteen defects across locked sections; REV-05 → REV-15 log the eleven that required changes. Reference batch rescaled 120 → 150 cases. Build budget corrected 48h → 42h.
- **v0.5** *(Aug 27, 2026)* — Sections 4, 5 and 6 written and locked in a single time-boxed pass, deliberately shallower than Section 3 because architecture is reversible, thresholds are meaningless before a real run, and a solo phase plan is a short list. REV-16 → REV-19 log four defects found while reading v0.4 for Step 4: an overlap between the `T-01` and `T-03` evidence predicates, a wrong bank-line decomposition in §2.2, a dead granularity exception in §3.6, and a subtype label that contradicted its own definition in §3.3. REV-20 closes the `exception_classification_accuracy` gap §2.11 deferred to Step 5.
- **v0.6** *(Aug 27, 2026)* — §6.3 added: the day-level phase plan is subdivided into nineteen implementation sessions with per-session artifacts, checkpoints and model assignments, plus a session boundary rule and a stateless-handoff protocol. Logged as REV-21. §6.4 adds the version control protocol — commit cadence, phase tagging, secrets, and the committed-versus-ignored split — logged as REV-22. No other change; Sections 1–5 are untouched.

---

## 1. Sub-direction

### 1.1 Statement

An agent that ingests a merchant's normalized accounting-ledger export, Razorpay Settlement Reconciliation data, and the merchant's bank statement, and closes the settlement accounting loop across a batch of transactions.

For each reconciliation case, the Controller:

1. Matches high-confidence records across the merchant ledger, Razorpay settlement data, and bank statement.
2. Detects missing, incorrect, or inconsistent accounting treatment — including unposted refunds, fee/GST discrepancies, chargeback lifecycle events, settlement holds and releases, and timing differences.
3. Derives journal adjustments from a fixed allowlist of accounting templates, with source-record evidence, and validates them using deterministic accounting and financial constraints.
4. Applies validated high-confidence adjustments to the synthetic ledger and re-runs reconciliation to verify the discrepancy is closed (post-adjustment residual = 0 paise).
5. Categorizes operational exceptions and identifies the external action required for resolution.
6. Explicitly abstains when evidence is insufficient rather than forcing a match or an accounting decision.

### 1.2 Unit of state: reconciliation case

The state machine operates on **reconciliation cases**, not raw input records.

- A **reconciliation case** is anchored to either (a) a Razorpay settlement, or (b) an orphan bank credit / ledger entry for which no settlement anchor could be identified.
- A single case can encompass many underlying records: multiple payments and refunds rolled into one settlement, one bank credit corresponding to one settlement, and multiple ledger lines related to the same settlement or bank credit.
- Raw records have **linkage status** (linked / unlinked / disputed) — this is intermediate data, not a terminal state.
- Reconciliation cases have **outcome state** — one of the five states defined in 1.3.

### 1.3 Reconciliation case outcome states

Every reconciliation case terminates in exactly one of five states:

- **`AUTO_MATCHED`** — All linked records match end-to-end; the case reconciles cleanly with no accounting action required.
- **`AUTO_CLOSED`** — Accounting discrepancy detected. A journal adjustment was instantiated from an allowlisted template, deterministically validated, applied to the synthetic ledger, and reconciliation was re-run confirming the post-adjustment residual is 0 paise.
- **`EXTERNAL_ACTION_REQUIRED`** — Exception confidently understood and categorized. Resolution requires action outside the Controller's authority (e.g., raise a support ticket with Razorpay for a missing settlement UTR, contact the acquiring bank about a delayed credit, follow up on a pending chargeback outcome). The case is not "resolved" — closing it depends on an external event.
- **`REVIEW_REQUIRED`** — A plausible match or adjustment candidate was identified, but it did not qualify for auto-action. Evidence is surfaced for a human reviewer to accept, reject, or modify. Two distinct reasons route here, and they are reported separately (see 1.6): confidence below the auto-action threshold, or exclusion by scope policy (2.5).
- **`ABSTAINED`** — Evidence is insufficient or inconsistent enough that no defensible candidate can be recommended. Escalated as-is for human investigation. Abstention is a designed behavior, not a system failure.

**Optimization principle:** maximum safe automation subject to a low false-match rate and a low unsafe-auto-action rate. A system that achieves 98% match rate by incorrectly matching ambiguous entries is worse than one achieving 91% with zero false matches. The metric surface (1.6) reflects this priority explicitly.

### 1.4 Positioning

Razorpay's product surface for finance operations currently includes:

- **Settlement Reconciliation API** — returns per-transaction settlement data including fees, tax, refunds, adjustments, and settlement UTR for bank matching.
- **Razorpay Recon** — AI-powered reconciliation product for matching gateway settlements, bank statements, and internal records.
- **RazorpayX Bookkeeping Agent** (part of the Agentic Business Banking suite) — posts accurate accounting entries to the merchant's ERP based on **predefined rules**, in real time.
- **RazorpayX Tally integration** — syncs bank statement transactions with Tally and prepares journal entries; the merchant approves and books them (RazorpayX does not directly post).

The AI Settlement Close Controller does **not** attempt to replace generic reconciliation or rules-based bookkeeping automation. It focuses on the hard settlement-close exceptions that remain when three financial sources — merchant ledger, Razorpay settlement evidence, bank statement — disagree in ways that predefined rules cannot resolve:

- Identifying the root cause across multi-source evidence.
- Selecting an appropriate accounting-treatment template.
- Producing evidence-linked, deterministically-validated corrections.
- Applying only safe, template-constrained corrections and re-verifying the resulting books.
- Abstaining when evidence does not justify an automated decision.

**Wedge:** the boundary word in Razorpay's own product language is *"predefined rules."* Predefined rules cover the happy path. Sources disagree, evidence is ambiguous, situations don't match any rule — that's the exception-resolution layer that sits above rules-based bookkeeping. When predefined rules fully cover a case, the Controller adds nothing; when they don't, the Controller is where hard cases get closed safely.

### 1.5 Scope boundary — data representation

Merchant accounting data is represented using a **canonical journal schema** rather than reproducing a specific ERP export format (Tally, Zoho Books, SAP, NetSuite, etc.).

Canonical journal-entry fields:

```
journal_entry_id  (string, unique)
date              (ISO 8601)
account_code      (string, from fixed chart of accounts defined in Section 3)
account_name      (string, denormalized for readability)
debit             (integer, paise)
credit            (integer, paise)
reference         (string, external reference — e.g., payment ID, invoice ID)
narration         (string)
source            (enum: manual, erp_import, controller_adjustment)
```

ERP-specific ingestion connectors are treated as replaceable adapters and are **out of Buildathon scope**. Rationale: reproducing any one ERP's export format credibly is high-effort and produces no signal for the judging bar; the Controller's value is in the reconciliation logic on top of the canonical schema, not in ERP adapters.

### 1.6 Evaluation surface

The system is evaluated against a held-out synthetic dataset with per-case ground-truth labels.

**Ground-truth schema per reconciliation case:**

```
case_id                          (string, unique)
expected_outcome_state           (one of the 5 states in 1.3)
ground_truth_exception_class     (from the four-class taxonomy in 3.3, or NONE)
ground_truth_exception_subtype   (from the subtype list in 3.3, or NONE)
expected_linked_source_records   (list of record IDs across all three sources)
expected_resolution              (text; expected external action if applicable)
expected_journal_entries         (list; see note below)
expected_template_ids            (list of template IDs from 3.4; empty if no entry expected)
expected_decline_reason          (policy | confidence | null)
should_auto_apply                (boolean; see note below)
```

`expected_journal_entries` is a plural list because some corrections require multiple journal lines (e.g., an unposted refund with associated fee adjustment may require paired entries), and because a single case can carry entries from more than one template (3.4).

`expected_journal_entries` is normally empty for non-`AUTO_CLOSED` states, with one designed exception: FR-05 (if built) posts a chargeback recognition entry on a case that terminates in `EXTERNAL_ACTION_REQUIRED` pending dispute resolution. `should_auto_apply` is therefore true for `AUTO_CLOSED` cases **and** for FR-05 recognition entries, not for `AUTO_CLOSED` alone. *(Corrected in REV-10 — v0.3's schema contradicted FR-05.)*

**Reported metrics:**

*Matching*

- `match_rate` — cases the system placed in `AUTO_MATCHED` / **total cases**. The classic finance definition: raw, no-adjustment reconciliation success across the whole batch. *(Revised in REV-01.)*
- `auto_match_recall` — cases correctly reaching `AUTO_MATCHED` / total cases whose ground truth is `AUTO_MATCHED`. Coverage of the cleanly-matchable population. *(Added in REV-01 — this is the metric formerly mislabelled `match_rate` in v0.1.)*
- `false_match_rate` — cases the system marked `AUTO_MATCHED` where ground truth is not `AUTO_MATCHED` / total cases. **Primary safety metric for matching.**

*Adjustment*

- `auto_close_recall` — cases correctly reaching `AUTO_CLOSED` / total cases whose ground truth is `AUTO_CLOSED`. *(Renamed in REV-09 — v0.3 called this `auto_close_rate`, repeating the name-versus-formula mismatch REV-01 corrected for `match_rate`.)*
- `auto_close_precision` — auto-applied entries matching ground truth / **all auto-applied entries**. **Primary safety metric for adjustment.** Distinct from `false_match_rate` because a correct match followed by an incorrect journal is a different failure mode. The denominator is auto-applied *entries* rather than auto-closed *cases* so that FR-05 recognition entries, which attach to `EXTERNAL_ACTION_REQUIRED` cases, are counted. *(Denominator corrected in REV-10.)*
- `auto_match_precision` — cases correctly marked `AUTO_MATCHED` / all cases the system marked `AUTO_MATCHED`. *(Added in REV-09, so both safety metrics have a precision-flavoured form.)*

> **Denominator convention.** Metrics named `*_rate` use **total cases** as the denominator. Metrics named `*_recall` use the ground-truth population for that state. Metrics named `*_precision` use the population the system *predicted*. `false_match_rate` is therefore not the complement of `auto_match_precision`, and the two are not directly comparable. *(Stated in REV-09.)*

*Classification*

- `state_prediction_accuracy` — cases where predicted terminal state equals expected terminal state / total cases.
- `exception_subtype_precision` — among cases the system assigned exception subtype S, the fraction whose ground-truth subtype is S. *(Renamed from `exception_classification_accuracy` in REV-20: a metric over the predicted population is a `*_precision` under the convention REV-09 itself established.)*
- `exception_subtype_recall` — among cases whose ground-truth subtype is S, the fraction the system assigned S. *(Added in REV-20 — this is the recall-side counterpart §2.11 flagged as a known gap. Both are reported per subtype with denominators visible, plus a macro average; see §5.2.)*

*Deferral*

- `declined_by_policy_rate` — cases routed to `REVIEW_REQUIRED` because the required treatment falls in a scope exclusion under 2.5 / total cases. These are correct behavior under v1 scope, not failures. *(Added in REV-02.)*
- `declined_by_confidence_rate` — cases routed to `REVIEW_REQUIRED` because confidence fell below the auto-action threshold / total cases. *(Added in REV-02.)*
- `abstention_rate` — cases in `ABSTAINED` / total cases. Reported alongside a stated operating range — over-abstention degrades system value even if it never produces false actions.
- `deferred_to_human_rate` — (`ABSTAINED` + `REVIEW_REQUIRED`) / total cases. Cases whose next step is a human decision inside the merchant's finance team.
- `open_case_rate` — all cases not in (`AUTO_MATCHED` + `AUTO_CLOSED`) / total cases. Includes `EXTERNAL_ACTION_REQUIRED`, which §1.3 explicitly describes as unresolved. *(REV-09 split v0.3's single `unresolved_rate`, which excluded `EXTERNAL_ACTION_REQUIRED` while §1.3 called those cases unresolved.)*

> `declined_by_confidence_rate` has **no ground-truth population by construction**: ground truth cannot know what the system's confidence will be, so every ground-truth `REVIEW_REQUIRED` case in the dataset is a *policy* decline. Confidence declines appear instead as cases whose ground truth is `AUTO_CLOSED` that the system declined, and are already penalised by `auto_close_recall`. This is expected behaviour, not a gap in the dataset.

*Value and performance*

- `value_coverage` — rupee value in (`AUTO_MATCHED` + `AUTO_CLOSED`) cases / total rupee value across all cases.
- `throughput` — cases per second, measured on the scale batch (FR-02).
- `end_to_end_latency` — wall-clock time on the reference batch (FR-01).

Target thresholds, held-out set composition, and anomaly injection distribution are defined in Section 5 (Evaluation methodology).

### 1.7 Safety and audit invariants

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

### 1.8 Output artifacts per batch run

For each batch run, the Controller produces:

1. **Reconciled ledger** — the synthetic merchant ledger with applied `AUTO_CLOSED` adjustments.
2. **Case log** — every reconciliation case with its terminal outcome state, linked records, and (where applicable) the journal entry applied or recommended.
3. **Exception report** — categorized non-`AUTO_MATCHED` cases with per-case reasoning and recommended next step (or explicit abstention rationale).
4. **Metrics report** — the full metric surface from 1.6 for the run, computed against the ground-truth labels.
5. **Audit trail** — for each `AUTO_CLOSED` case: the template selected, source records cited, deterministic calculations performed, and the specific safety validations passed.

The pitch video will demonstrate the full pipeline on a real batch run and show these five artifacts as concrete evidence that "closes the loop" is a demonstrable behavior, not marketing copy.

---

## 2. MVP scope

### 2.0 Build budget

Aug 28 → Sept 3 is **7 build days at 6 hours/day ≈ 42 hours**. Aug 27 is spent on specification and holds no build time. Every decision in this section is sized against 42 hours, not against the Sept 5 wall. *(Corrected in REV-12 — v0.3 recorded 48 hours by counting Aug 27 as a build day, which also contradicted the Aug 28 → Sept 3 window named in Section 6.)* Sept 4–5 is contingency and holds no planned work.

The scope below was cut once already: at a 4-hour/day budget the auto-close path was reduced to four families. At 6 hours/day, a fifth family is restored and the third bank-format profile moves from stretch to committed. This is recorded so that if the available hours drop again, the reverse cut is mechanical rather than improvised — drop family 5, then the third bank format, then reduce the reference batch to 100 cases. Note that every scope decision in this section was originally sized against 48 hours; at the corrected 42, the cut order is nearer than it reads, and family 5 is the first thing to go. The batch size is cut last because it is the cheapest thing in the build (generator cost is fixed; case count is a parameter) and the most expensive thing to lose (every metric denominator shrinks with it).

### 2.1 Data sourcing

**FR-01a.** All input data is **synthetic**. The Controller makes no live Razorpay API calls at any point in the demo or eval path, and the pipeline MUST run offline end to end.

Synthetic settlement data MUST conform to the field shape of the real Razorpay settlement-recon payload — `entity_id`, `type`, `debit`, `credit`, `amount`, `currency`, `fee`, `tax`, `on_hold`, `settled`, `created_at`, `settled_at`, `settlement_id`, `settlement_utr`, `order_id`, `method` — and settlement-level records MUST carry `id`, `amount`, `status`, `fees`, `tax`, `utr`. All amounts in the smallest currency unit (paise), matching Razorpay's own convention.

Rationale: the track brief specifies a synthetic batch, so synthetic data is the requested form rather than a shortcut. Conforming to the real payload shape gives Razorpay-adjacency that a reviewer can verify against Razorpay's public docs, with no credentials, no rate limits, and no live dependency that can fail mid-demo.

> Assumption: field-shape conformance is checked by eye against Razorpay's published settlement and recon entity docs, not by a contract test against a live sandbox.

### 2.2 Batch size

**FR-01 (reference batch).** The reference batch MUST contain approximately:

| Layer | Count |
|---|---|
| Recon rows (payments, refunds, adjustments) | ~1,500 |
| Settlements | 125 |
| Bank statement lines | ~175 (~98 settlement credits + ~28 orphan-case lines + ~50 non-settlement noise: bank charges, unrelated NEFT, reversals) |
| Ledger entries | ~5,800 *(corrected in REV-23)* |
| **Raw records, total** | **~7,600** *(corrected in REV-23)* |
| **Reconciliation cases** | **150** (125 settlement-anchored + 25 orphan) |

*(Rescaled and corrected in REV-13. v0.3 specified 120 cases and ~3,500 raw records; the record counts were independently wrong even at 120, and the row label omitted `adjustment` rows, which are family 5's only evidence. Ledger-entry and raw-record-total figures further corrected in REV-23 — REV-13's own re-derivation undercounted legs per payment.)*

**Settlement credits number ~98, not 125.** Twenty-seven settlement-anchored cases require by construction that *no* matching bank credit exists at the batch snapshot: the 10 family-4 cases (whose hard precondition in §3.2 is exactly this), the 12 family-4 no-ops (lag still inside the T+2 window), and the 5 `BANK_CREDIT_OVERDUE` cases. Generating a credit for those would destroy the population. The ~175 total is unchanged; only its decomposition was wrong, and the decomposition is the generator's contract. *(Corrected in REV-17.)*

**FR-02 (scale batch).** A second batch of ~360 cases / ~12,800 raw records MUST be generated from the same generator code with a different seed, used **only** for the `throughput` measurement. It is not separately labelled or analyzed. The case count is deliberately *not* rescaled alongside FR-01: `throughput` is reported in cases/second, so the batch size does not affect the measurement, and holding it fixed avoids churn for no gain. Only the record count is restated, to stay consistent with the corrected per-case record density. *(REV-13.)*

Rationale: the unit that matters is **cases**, not raw records, because every metric in 1.6 has a case-count denominator. With five outcome states plus an exception taxonomy underneath, 150 cases yields roughly 17–56 per state and, more importantly, 10 cases per FR-04 family — the minimum at which a per-family accuracy number is worth printing. At the 120 originally specified, each family held 8 cases and a single error moved per-family accuracy by 12.5 points; Section 5 requires a per-close-pattern breakdown, so the extra 30 cases buy statistical legibility for the metric the judging bar cares most about, at zero build cost beyond generator runtime. The track floor of 50 records would produce single-digit denominators across most of the metric surface, which is precisely the "one cherry-picked match proves nothing" failure the judging bar names.

**Honest framing constraint:** synthetic data is free to generate, so batch size is *not* itself an achievement and MUST NOT be presented as one in the README or video. The batch is sized for statistical legibility of the metrics. The scale batch exists so the throughput figure has headroom and is not dominated by per-run fixed cost.

### 2.3 Outcome states in v1

**FR-03.** All five terminal states from 1.3 ship in v1. No state is deferred to v2.

Depth is deliberately unequal:

| State | Build weight | What it requires |
|---|---|---|
| `AUTO_CLOSED` | ~70% | Templates, deterministic derivation, full 1.7.5 validation chain, ledger apply, idempotency, re-reconciliation |
| `EXTERNAL_ACTION_REQUIRED` | ~10% | Exception classification + resolution-text template per category |
| `REVIEW_REQUIRED` | ~10% | Confidence banding, policy-exclusion routing, proposed-JV packaging (FR-07) |
| `AUTO_MATCHED` | ~5% | Falls out of the deterministic matcher |
| `ABSTAINED` | ~5% | Default fallthrough + abstention rationale |

Rationale: `ABSTAINED` and `EXTERNAL_ACTION_REQUIRED` *are* the honest exception list the judging bar demands — cutting them to save time would cut the differentiator. Their marginal cost is low. The asymmetry is stated here so that uniform state coverage is not mistaken for uniform capability depth.

### 2.4 Auto-close families in v1

**FR-04.** The `AUTO_CLOSED` path supports exactly five close-pattern families in v1. Each is a case where the correct entry is fully derivable from source evidence with no accounting judgment:

1. **Unposted Razorpay fee (MDR) + GST on fee**
2. **Settled refund absent from the ledger**
3. **Gross-vs-net posting error** — merchant booked the net bank credit as revenue; fee and GST never recognised
4. **Premature bank-account posting (funds-in-transit misposting)** — the sale was posted `Dr Bank Account` at capture instead of `Dr Razorpay Clearing`, claiming a bank position that does not yet exist *(narrowed in REV-05; v0.3 read "settlement timing / funds-in-transit — clearing entries at a period boundary", which also covered a no-op case that must never be auto-closed)*
5. **Settlement adjustment unposted** — a `type = "adjustment"` recon line carrying a real debit or credit that Razorpay applied to the settlement, with no corresponding merchant-ledger entry *(narrowed in REV-06; v0.3 read "on-hold release or settlement adjustment", and the on-hold half is no longer in scope)*

**FR-05 (stretch, not committed).** A sixth family — **chargeback deduction recognition** — MAY be added if the committed scope lands early. The split is deliberate: the *deduction* is deterministic (the settlement shows the debit, the money left, recognising it against a holding account is arithmetic), but the *final classification* depends on a dispute outcome that has not occurred. So the auto-closable portion is recognition against a holding account only; the case still terminates in `EXTERNAL_ACTION_REQUIRED` pending dispute resolution. If FR-05 is not built, chargeback cases remain in the dataset and are detected and classified, terminating in `EXTERNAL_ACTION_REQUIRED` without a posted entry.

Five families map to approximately 6–8 templates once paired variants are counted. The final count is six, fixed in §3.4. *(REV-08 struck v0.3's trailing clause "consistent with the 8–12 range sketched for Section 3" — 6–8 and 8–12 overlap only at 8, and no 8–12 range appeared anywhere in the document.)*

**Why these are safe to auto-post:** Razorpay's own recon payload supplies every number required. `fee` and `tax` are reported per record; `on_hold` and `settled` are explicit flags; settlement-level `fees`, `tax`, and `amount` are reported directly. Nothing on the auto path requires the model to originate a figure — which is what invariant 1.7.2 demands. GST on MDR stays on the auto path specifically because Razorpay reports `tax` explicitly, so no rate inference is needed; this keeps an India-specific mechanic in the automated set without introducing a tax judgment.

### 2.5 Policy exclusions from the auto-close path

**FR-06.** The following are detected and classified but MUST NOT be auto-posted in v1, regardless of model confidence. They terminate in `REVIEW_REQUIRED` and are counted under `declined_by_policy_rate`:

- TDS treatment — Section 194-O (e-commerce operator) and 194-H
- GST input tax credit eligibility on MDR
- Revenue recognition timing decisions
- Any entry that embeds a tax position
- **Date-only reclassification across a period boundary** — a ledger entry posted to the correct accounts on the wrong date, where the settlement has already credited the bank. No delta entry exists to post; the correct treatment is a period reclassification, which shades directly into revenue-recognition timing. *(Added in REV-11; see §3.3 for the case population this covers.)*

**FR-07.** Every `REVIEW_REQUIRED` case MUST carry a machine-readable proposed journal entry alongside its evidence, in the same schema as an applied entry, flagged as unapplied with an explicit decline reason (`policy` or `confidence`). A reviewer receives a decision to accept or reject, not a research task.

Rationale: a wrongly auto-posted tax entry is the most expensive failure mode in this domain, and a solo builder cannot credibly validate tax-position correctness in 48 hours. The defensible line is: **auto-post only what the source report supplies as a number; recommend-only what requires a tax judgment.** 194-O is retained as detection and recommendation, which preserves the India-specific signal without the exposure.

This caps `auto_close_recall` by construction — some ground-truth cases are `REVIEW_REQUIRED` by policy rather than by low confidence. That is why REV-02 split the deferral metrics: without the split, deliberate scope discipline reads as weak detection.

### 2.6 Bank statement format support

**FR-08.** Three bank statement format profiles ship in v1, defined as **declarative column-mapping configuration**, not as per-bank parser code:

1. **HDFC-shape** — `Date`, `Narration`, `Chq./Ref.No.`, `Value Dt`, `Withdrawal Amt.`, `Deposit Amt.`, `Closing Balance`
2. **ICICI-shape** — serial number column, separate value/transaction dates, `Transaction Remarks`, withdrawal/deposit amount columns
3. **Axis-shape** — `Tran Date`, `Chq No`, `Particulars`, `Debit`, `Credit`, `Balance`, `Init.Br`

Input formats: **CSV and XLSX only.**

The adapter MUST handle: separate withdrawal/deposit columns versus debit/credit naming; `DD/MM/YY` versus `DD-MM-YYYY` date formats; differing narration field names; junk header rows above the table; comma-grouped amount strings; and trailing summary blocks below the table.

**FR-09.** UTR matching MUST NOT assume a clean join. Razorpay's guidance is to reconcile settlements against the bank statement by UTR, but in practice the UTR arrives embedded in free-text narration, sometimes truncated, sometimes absent. The matcher MUST support a fallback on (date window + net amount + settlement identity). Detailed matching strategy is deferred to Section 3/4; what is fixed here is that the clean-join assumption is not permitted.

Rationale: three profiles rather than two costs roughly 20 lines of configuration and pre-empts the obvious reviewer objection that one bank is hardcoded. Beyond three, additional formats produce zero reconciliation-logic signal — it is parser tax.

### 2.7 Demo surface

**FR-10.** The CLI is the product. A single command runs a batch end to end and emits both a console summary and a report file.

**FR-11.** The report is **one self-contained static HTML file** (no server, no build step, no external asset fetch) containing all five artifacts from 1.8: metrics table, filterable case log, per-case evidence and audit-trail drill-down, categorized exception list, and reconciled-ledger diff.

Explicitly not built: web server, SPA framework, database UI, authentication, deployment.

Rationale: the audit trail is what proves the 1.7 safety invariants are real rather than claimed, and a scrolling terminal makes an audit trail unwatchable on video. A static HTML report is roughly half a day and is committable, so a reviewer who never runs the code still sees real output. A live dashboard is a multi-day sink that earns nothing against a bar phrased entirely in throughput, accuracy, and exceptions.

### 2.8 Repository versus video

**FR-12.** Nothing capability-bearing is video-only. The repository MUST contain: the synthetic data generator; the seeded reference dataset, checked in; the full pipeline; the eval harness; sample output reports from a real run; `ARCHITECTURE.md`; and a `README` with a one-command reproduce path.

**FR-13.** The exact run shown in the video MUST be pinned and committed: generator seed, git SHA, and the metrics JSON produced by that run.

**NFR-01.** Given the same seed and SHA, a run MUST reproduce identical metrics on a clean clone. Any LLM-slot nondeterminism must be either eliminated on the eval path or explicitly bounded and disclosed (mechanism deferred to Section 4).

Video-only content is narrative: the walkthrough, and a "what broke and what I did about it" segment — which maps directly to the *Failure recovery* row of the published judging criteria.

**Governing rule:** if it appears in the video, it MUST be reproducible from the repository at that SHA. This is the direct answer to "one cherry-picked match proves nothing" — the counter is not a claim, it is a reproducible run.

### 2.9 Non-functional requirements

- **NFR-02.** `throughput` is measured on the scale batch (FR-02) and reported as cases/second with the hardware stated.
- **NFR-03.** `end_to_end_latency` is measured on the reference batch (FR-01) as wall-clock time.
- **NFR-04.** All monetary arithmetic uses integer paise end to end (traces invariant 1.7.1). No floating-point rupee value may enter matching, residual computation, or JV derivation.
- **NFR-05.** The pipeline MUST run with no network dependency other than the LLM inference endpoint, and MUST expose a fully offline mode for the deterministic path.
- **NFR-06.** A clean clone MUST reproduce the committed run with a single documented command.

### 2.10 Explicit non-goals for v1

Announced up front in the README:

- No ERP connectors (Tally, Zoho Books, SAP, NetSuite) — canonical journal schema only, per 1.5
- No live Razorpay API calls; synthetic data only, shaped to the published recon payload
- No PDF or scanned bank statement parsing; no Account Aggregator feeds
- No tax filing artifacts (GSTR, TDS returns); no tax position auto-posted (per 2.5)
- No chargeback or on-hold-dispute auto-posting beyond the bounded recognition in FR-05, if built
- No multi-currency and no international settlements — INR only
- No writes to any real accounting system; the ledger is a local synthetic store
- No human-in-the-loop acceptance UI — `REVIEW_REQUIRED` surfaces a decision-ready proposal, it does not collect the decision
- No cash forecasting or cash-position projection (a different Track 4 direction)
- No fraud or risk scoring (Track 2 territory)
- No authentication, multi-tenancy, deployment, or scaling infrastructure
- No model fine-tuning

### 2.11 Questions surfaced in Step 2, deferred

- **Resolved in Step 3 (v0.4):** exact template count and per-template allowed accounts and posting directions (§3.4); anomaly injection distribution across the five families (§3.5); orphan-case generation strategy (§3.6).
- **Defer to Step 4:** matching strategy for the UTR fallback in FR-09; which slots are deterministic versus LLM; how NFR-01 determinism is enforced across an LLM slot.
- **Defer to Step 5:** target thresholds for every metric in 1.6; the stated operating range for `abstention_rate`; and one known gap — `exception_classification_accuracy` is conditioned on cases the system *placed* in `EXTERNAL_ACTION_REQUIRED`, making it precision-flavoured and blind to cases that should have landed there and did not. A recall-side counterpart is needed. *(Closed by REV-20; see §5.2.)*
- **Defer to Step 5:** the generator emits its own ground-truth labels, which means the eval grades the pipeline against the same process that created the anomalies. This is the standard synthetic-eval limitation and MUST be disclosed rather than papered over; the mitigation design belongs to evaluation methodology.

---

## 3. Data model, chart of accounts, close patterns, and generation strategy

**Status:** locked in full (3.0–3.6). Sub-topics 3.1 and 3.2 were locked in v0.3 and carry corrections from the v0.4 verification pass (REV-14, REV-15). Sub-topics 3.3–3.6 were completed and locked in v0.4.

### 3.0 Governing assumption — accrual-basis bookkeeping

> **Assumption (project-wide):** all synthetic merchant bookkeeping is **accrual-basis**. Revenue is recognized at point of sale/capture, not at point of cash receipt. `Razorpay Clearing` (3.2) is the standing receivable account tracking money owed by Razorpay between capture and bank settlement.

This governs every account template in 3.2 and was surfaced while resolving family 4 — logged here because it applies retroactively to families 1–3, which were built consistent with it without it having been stated explicitly at the time.

### 3.1 Canonical input schemas

Four separate schemas — not one denormalized table. A recon line and a ledger entry are different things with different lifecycles; collapsing them would hide the exact mismatches the Controller exists to find.

**Recon line (`recon_line`)** — one row per `entity_id` from the Razorpay `/v1/settlements/recon/combined` payload. Raw external evidence, never mutated by the Controller.

```
entity_id          string, unique   # pay_*, rfnd_*, trf_*, adj_*
type                enum             # payment | refund | transfer | adjustment
debit               integer, paise
credit              integer, paise
amount              integer, paise
fee                 integer, paise
tax                 integer, paise
on_hold             boolean          # shape-only in v1; no consumer (see below)
settled             boolean
created_at          integer, unix ts
settled_at          integer, unix ts, nullable
settlement_id       string, nullable # FK -> settlement.id; null for unsettled rows
settlement_utr      string, nullable
payment_id          string, nullable # links refund -> parent payment; null for payments and adjustments
order_id            string, nullable
posted_at           integer, unix ts, nullable  # constant null; never used as evidence (see below)
credit_type         string           # generator emits "default" only (see below)
dispute_id          string, nullable # FK -> dispute (FR-05 stretch only)
description         string, nullable
method              string, nullable
```

Dropped from the real payload as carrying no signal for any family or taxonomy category: `entity` (constant), `currency` (INR-only per 2.10), `order_receipt`, `card_network`, `card_issuer`, `card_type`, `notes`.

> Assumption: `credit_type` — the real payload's only observed value is `"default"`; the generator emits only this value rather than inventing undocumented enum values with no verified source.

**Three field-semantics corrections from the v0.4 verification pass (REV-14):**

- **`posted_at` is emitted as constant `null` and MUST NOT be read as evidence anywhere in the pipeline.** REV-04 retained it as "a plausible signal for ledger-posting status." That reading is wrong on two counts. Checked against `razorpay.com/docs/api/settlements/fetch-recon`: `posted_at` does not appear in the documented Response Parameters list at all, and is `null` in all four sample rows — it is undocumented, exactly like `credit_type`. More importantly, it is a Razorpay-side field, whereas families 1, 2, 3 and 5 are all defined by *absence from the merchant ledger*. Establishing that from anything other than absence in `ledger_entry` would leak ground truth into the model's input.
- **`payment_id` links refunds (and transfers) to a parent payment only.** The docs state it is null for payments, and the sample `adjustment` row also carries null. Family 5 therefore has no parent-payment link and MUST assemble its case on `settlement_id` alone. The same sample adjustment carries `settlement_utr: null` while `settlement_id` is populated, so UTR is not available as a fallback anchor for adjustment rows either.
- **`on_hold` has no consumer in v1.** The docs scope it to transfers ("whether the account settlement for transfer is on hold"), and `type = "transfer"` is excluded from the generator entirely (§3.5). The field is retained for payload-shape fidelity and is always `false`.

`settlement_id` is nullable because `settled` is a real field doing real work: unsettled rows have no settlement anchor. Making `settlement_id` mandatory would have forced `settled` to a constant `true` and left two fields dead. *(REV-14.)*

**Settlement header (`settlement`)** — one row per `settlement_id`. Matches the verified Razorpay entity exactly.

```
id                  string, unique   # setl_*
amount              integer, paise
status              enum             # created | processed | failed — v1 generates "processed" only
fees                integer, paise
tax                 integer, paise
utr                 string
created_at          integer, unix ts
```

> Assumption: v1 generates `status = "processed"` only. `created`/`failed` settlements can't anchor a closed reconciliation case and don't map to any of the five families or the orphan-case set — excluded from the reference batch, not a lost signal.

**Bank statement line (`bank_line`)** — not a Razorpay API shape. This is the **post-adapter canonical shape** the pipeline consumes after FR-08's column-mapping adapter normalizes any of the three bank format profiles. The adapter itself (parsing logic) is Step 4 territory.

```
line_id             string, unique, synthetic
value_date          date            # normalized from bank-specific date format
narration           string          # raw bank text; UTR often embedded here per FR-09
bank_ref_no          string, nullable   # bank's own reference/cheque number; secondary matching signal alongside narration-embedded UTR
withdrawal_paise     integer         # 0 if not a withdrawal
deposit_paise        integer         # 0 if not a deposit
closing_balance_paise integer
bank_profile         enum            # hdfc | icici | axis — tag only, not a COA dimension
```

**Merchant ledger entry (`ledger_entry`)** — restates the canonical journal schema locked in 1.5, with two fields added to make locked invariant 1.7.4 (idempotency on `(case_id, resolution_id)`) implementable. 1.5 didn't specify these fields either way; this is a completion of that schema, not a revision to it.

```
journal_entry_id    string, unique
date                ISO 8601
account_code        string           # FK -> chart of accounts, 3.2
account_name        string
debit                integer, paise
credit                integer, paise
reference            string          # external ref: payment ID, invoice ID
narration            string
source                enum             # manual | erp_import | controller_adjustment
resolution_id        string, nullable   # set only when source = controller_adjustment
case_id               string, nullable   # set only when source = controller_adjustment
```

### 3.2 Chart of accounts

7 accounts, deliberately small. Every account is used by at least one committed FR-04 family — nothing speculative. No per-bank-profile account fragmentation (bank profile is a `bank_line` tag, not a COA dimension) and no per-family clearing account duplication.

| Account | Type | Used by |
|---|---|---|
| `Bank Account` | Asset | Family 4 |
| `Razorpay Clearing` | Asset (clearing/receivable) | Families 1, 2, 4, 5 |
| `Sales Revenue` | Revenue | Family 3 |
| `Sales Returns and Allowances` | Contra-revenue | Family 2 |
| `Payment Gateway Charges` | Expense | Families 1, 3 |
| `GST on Gateway Charges` | Expense | Families 1, 3 |
| `Razorpay Settlement Adjustments` | Revenue/Expense (bidirectional) | Family 5 |

*(The `Used by` column is corrected in REV-15. v0.3 listed `Sales Revenue` as used by "Family 2 (contra), 3, 4". Family 2 posts to `Sales Returns and Allowances` and never touches `Sales Revenue` — that is the entire purpose of the contra account. Family 4's template uses only `Razorpay Clearing` and `Bank Account`, as the family's own text states. The column is the audit for "every account is used by at least one committed family"; that claim still holds after correction.)*

**Account codes:**

| Code | Account | Type |
|---|---|---|
| `1010` | `Bank Account` | Asset |
| `1020` | `Razorpay Clearing` | Asset (clearing/receivable) |
| `4010` | `Sales Revenue` | Revenue |
| `4020` | `Sales Returns and Allowances` | Contra-revenue |
| `5010` | `Payment Gateway Charges` | Expense |
| `5020` | `GST on Gateway Charges` | Expense |
| `4900` | `Razorpay Settlement Adjustments` | Other operating income/(expense), bidirectional |

**Deferred:** a chargeback holding account for FR-05 (stretch) is explicitly not part of the v1 COA. Added only if FR-05 is actually built, to keep the locked COA free of speculative accounts.

**FR-04 family-to-account mapping, with per-family reasoning:**

**Family 1 — Unposted MDR fee + GST on fee.** Fee/GST were never posted; revenue was booked correctly at gross. Template: `Dr Payment Gateway Charges, Dr GST on Gateway Charges / Cr Razorpay Clearing`.

`GST on Gateway Charges` recognizes the deducted tax amount as a straight expense — arithmetic, sourced directly from `recon_line.tax`, no judgment. This is distinct from and does not touch **ITC eligibility on that GST**, which stays excluded under FR-06 (2.5). The account is deliberately not named anything containing "Input Tax Credit," so the FR-06 boundary is structurally visible rather than merely documented.

**Family 2 — Settled refund absent from the ledger.** A `refund`-type recon line with `debit` = refund amount, settled, but never recorded in the ledger — so revenue still stands for a sale that was refunded. Template: `Dr Sales Returns and Allowances / Cr Razorpay Clearing`.

Uses a contra-revenue account rather than a direct debit to `Sales Revenue` — standard double-entry practice, preserves the historical gross-sales-vs-refunds record, and serves the audit-trail differentiator (1.8, FR-11) better than a figure that silently erases the original sale.

> Assumption: Razorpay's MDR fee is **non-refundable on refund** — consistent with general gateway industry norm and with the zero `fee`/`tax` observed on the sample refund line in Razorpay's own docs (not independently verified as Razorpay policy). Family 2's template is therefore a clean two-line entry with no fee-reversal leg. If this proves wrong later, it is a one-line generator and template correction, not a structural change.

**Family 3 — Gross-vs-net posting error.** The merchant booked the *net* bank credit directly as revenue, so fee and GST were never recognized anywhere and revenue is understated by exactly `fee + tax`. Template: `Dr Payment Gateway Charges, Dr GST on Gateway Charges / Cr Sales Revenue`.

Worked arithmetic (illustrative): gross sale ₹1000, fee ₹20 (2% MDR), tax ₹3.60 (18% GST on the fee), net credited ₹976.40. Wrong entry: `Dr Bank/Clearing 976.40 / Cr Sales Revenue 976.40`. Correct entry: `Dr Bank/Clearing 976.40, Dr Payment Gateway Charges 20, Dr GST on Gateway Charges 3.60 / Cr Sales Revenue 1000`. Correction (the delta, which is what the template posts): `Dr Payment Gateway Charges 20, Dr GST on Gateway Charges 3.60 / Cr Sales Revenue 23.60`.

*(Re-cut at 2% in REV-15. v0.3's example used a ₹29 fee on ₹1000 — a 2.9% rate lifted from the Razorpay docs sample, which is a USD/AMEX line. Razorpay's standard Indian domestic rate is 2% plus 18% GST on the fee. The arithmetic was internally correct; the rate was wrong for an INR-only spec.)*

Same two expense accounts as family 1; credit leg lands on `Sales Revenue` instead of `Razorpay Clearing`. No new accounts.

**Family 4 — Settlement timing / funds-in-transit (narrowed).** Scoped specifically to a **wrong-account posting error**, not generic T+2 settlement lag. Under the accrual-basis assumption (3.0), the correct entry at sale time is `Dr Razorpay Clearing / Cr Sales Revenue`. The correctable error is when the ledger instead posted `Dr Bank Account / Cr Sales Revenue` — debiting the bank account prematurely, before the cash actually arrived. Template: `Dr Razorpay Clearing / Cr Bank Account` (reclassifying the premature bank debit into the correct clearing position).

Detected deterministically: the ledger shows a `Bank Account` debit dated at/near capture, but the corresponding `bank_line` shows no matching credit until later (or never, at the amount claimed) — an internal contradiction between the ledger's claimed bank position and the actual bank statement.

**Hard precondition (REV-15).** Family 4 applies **only** where no bank credit matching the settlement exists in `bank_line` as of the batch snapshot. If the credit has already landed, the merchant's single entry sits at the *correct accounts on the wrong date*: the ledger's bank balance already agrees with the statement, and posting `Dr Razorpay Clearing / Cr Bank Account` would understate bank against the statement and leave clearing permanently open — an auto-close that *creates* a reconciliation break. That situation is a date error, not an account error; it terminates in `REVIEW_REQUIRED` with `decline_reason = policy` under the exclusion added to §2.5 by REV-11. v0.3 left this precondition implicit in the phrase "no matching credit until later (or never, at the amount claimed)", which is ambiguous on exactly this point. Invariant 1.7.5's post-adjustment residual check would probably catch the bad posting and downgrade the case, but a family that relies on failing validation for a share of its instances is not correctly scoped.

**Amount rule.** The reclassified amount is the amount of the ledger's premature `Bank Account` debit, whatever it is — not a recomputed net figure. The generator produces family-4 cases fee-clean (correctly posted fee and GST, or no fee posting error) so that family 4 never overlaps families 1 or 3 within a single case.

**Explicitly not this family — a no-op case that must not be auto-closed:** ordinary settlement lag where the ledger correctly posted `Dr Razorpay Clearing / Cr Sales Revenue` at sale time, and clearing simply hasn't yet flipped to `Bank Account` at the batch snapshot. No error exists to correct. Under the taxonomy in 3.3 this case is `AUTO_MATCHED` **and** `EXPECTED_TIMING_DIFFERENCE` — state and exception class are independent axes, not alternatives.

Uses only `Razorpay Clearing` and `Bank Account`, both pre-existing. No new accounts.

**Family 5 — On-hold release or settlement adjustment unposted (narrowed).** Scoped specifically to `recon_line` rows with `type = "adjustment"` carrying a real `debit` or `credit` amount that Razorpay applied to the settlement, with no corresponding merchant-ledger entry — nothing in the merchant's own sales/refund records would have predicted an arbitrary Razorpay-side adjustment. A bare on-hold status change (`on_hold` true→false) with no associated `adjustment`-type line and no amount is **not** in scope for this family — it's a status fact, not a posting trigger.

Template, direction depends on adjustment sign:
- Credit adjustment (Razorpay added money): `Dr Razorpay Clearing / Cr Razorpay Settlement Adjustments`
- Debit adjustment (Razorpay deducted money): `Dr Razorpay Settlement Adjustments / Cr Razorpay Clearing`

`Razorpay Settlement Adjustments` is a new account, kept separate from `Sales Revenue` so the P&L doesn't misrepresent Razorpay-initiated corrections as operating revenue — also gives the audit trail (1.8) a distinct, honestly-labeled bucket, serving the judging bar's "honest exception list" framing directly.

### 3.3 Close-pattern taxonomy

**The taxonomy is a second axis, not a refinement of the outcome states.** Outcome state (1.3) answers *what the Controller did*; exception class answers *what was actually wrong with the case*. Every case carries both labels independently. FR-06 tax cases demonstrate the independence: the exception class is a genuine accounting correction, while the outcome state is `REVIEW_REQUIRED`. v0.3's §3.2 wrote the family-4 no-op as terminating "`AUTO_MATCHED` **or** classified `EXPECTED_TIMING_DIFFERENCE`"; that "or" was a category error and is corrected above.

The taxonomy has **two levels**. Four classes, plus subtypes beneath them. The subtype level exists because `exception_subtype_precision` (1.6, renamed in REV-20) grades exception type among cases the system placed in `EXTERNAL_ACTION_REQUIRED` — if the taxonomy stopped at four classes, essentially every such case would be `OPERATIONAL_EXCEPTION` and the metric would read near-100% while measuring nothing.

**A fifth sentinel value, `NONE`,** is required because 1.6 mandates an exception label on every case and a fully clean case is none of the four. It is an explicit value rather than a null so that it is countable.

#### The four classes

**`ACCOUNTING_CORRECTION`** — the merchant's books are wrong as of the snapshot, the error is fully determined by source evidence, and a journal entry against the fixed chart of accounts restores them. No counterparty action is required. Subtypes: `OMISSION` (an economic event occurred and nothing was posted) and `MISPOSTING` (an event was posted to the wrong account, the wrong amount, **or the wrong period**). *(The period clause added in REV-19: the family-4 date-error variant is labelled `MISPOSTING` in the population map below, but under v0.4's two-clause definition its accounts and amount are both correct, so the label contradicted the definition.)* This split is the classical omission-versus-misposting distinction from reconciliation practice, not an invention of this spec. Terminal state is `AUTO_CLOSED` where a template matches and the 1.7.5 chain passes, `REVIEW_REQUIRED` where the treatment is policy-excluded (2.5) or confidence is below threshold.

**`OPERATIONAL_EXCEPTION`** — a real discrepancy that no journal entry can resolve, because the underlying fact is still open or a counterparty must act. The books may or may not be wrong; it cannot yet be known. Terminal state `EXTERNAL_ACTION_REQUIRED`. Subtypes:

| Subtype | Trigger |
|---|---|
| `SETTLEMENT_UTR_MISSING` | Settlement is `processed` but carries no UTR, so no bank-side anchor exists |
| `BANK_CREDIT_OVERDUE` | Settlement window has elapsed with no matching bank credit |
| `SETTLEMENT_AMOUNT_MISMATCH` | Settlement header amount ≠ sum of its recon lines net of fees and tax |
| `UNMATCHED_INBOUND_CREDIT` | Bank credit with an identifiable counterparty but no Razorpay anchor |
| `REVERSAL_UNMATCHED` | Bank reversal with no matching prior credit in the batch |
| `DUPLICATE_CREDIT` | Same UTR credited twice on the bank statement |

`DISPUTE_PENDING` is a seventh subtype attached to the FR-05 chargeback population, which exists in the dataset and is classified whether or not FR-05's recognition entry is built.

**`EXPECTED_TIMING_DIFFERENCE`** — the sources disagree, and that disagreement is the *correct* state of the world at the snapshot under the accrual assumption (3.0). It self-resolves with no intervention. This is a positive classification of "this break is not a break," and it is what makes correct inaction gradeable rather than accidental. Terminal state `AUTO_MATCHED`, zero adjustments.

For this to be reachable, the matcher needs one rule stated explicitly, or it will compute a non-zero residual and never emit `AUTO_MATCHED`:

> **Timing-residual rule.** A case reconciles cleanly when its residual is fully attributable to an expected timing item — that is, the settlement's `created_at` falls within the settlement window as of the batch snapshot. Past that window with no bank credit, the case flips to `OPERATIONAL_EXCEPTION` / `BANK_CREDIT_OVERDUE` and terminates in `EXTERNAL_ACTION_REQUIRED`.

> **Settlement window: T+2 working days**, weekends excluded, no public-holiday calendar. Razorpay's merchant terms commit to settlement within two escrow-bank working days following the transaction date, and Indian card settlement is conventionally T+2 (UPI T+1). The weekends-only calendar is a deliberate simplification and is disclosed as such; a real close would use a banking-holiday calendar.

**`AMBIGUOUS_CASE`** — evidence is insufficient or internally inconsistent such that no single defensible treatment exists: several mutually exclusive readings fit the evidence, or a required piece of evidence is absent. The test separating this from `OPERATIONAL_EXCEPTION` is one question — *do we know what happened and who must act?* If yes, operational. If no, ambiguous. Terminal state `ABSTAINED`.

#### Family and population mapping

| Case population | Class / subtype | Expected state |
|---|---|---|
| Family 1 — unposted MDR + GST | `ACCOUNTING_CORRECTION` / `OMISSION` | `AUTO_CLOSED` |
| Family 2 — settled refund unposted | `ACCOUNTING_CORRECTION` / `OMISSION` | `AUTO_CLOSED` |
| Family 3 — gross-vs-net posting error | `ACCOUNTING_CORRECTION` / `MISPOSTING` | `AUTO_CLOSED` |
| Family 4 — premature bank debit, credit not landed | `ACCOUNTING_CORRECTION` / `MISPOSTING` | `AUTO_CLOSED` |
| Family 4 date-error variant — credit landed, accounts correct | `ACCOUNTING_CORRECTION` / `MISPOSTING` | `REVIEW_REQUIRED`, `policy` |
| Family 4 no-op — lag within the settlement window | `EXPECTED_TIMING_DIFFERENCE` | `AUTO_MATCHED` |
| Family 5 — unposted adjustment line | `ACCOUNTING_CORRECTION` / `OMISSION` | `AUTO_CLOSED` |
| FR-06 tax positions (194-O, ITC eligibility) | `ACCOUNTING_CORRECTION` / either | `REVIEW_REQUIRED`, `policy` |
| Chargebacks (FR-05 population) | `OPERATIONAL_EXCEPTION` / `DISPUTE_PENDING` | `EXTERNAL_ACTION_REQUIRED` |
| Fully clean cases | `NONE` | `AUTO_MATCHED` |

**Bare on-hold is not a case population in v1.** A hold changes *when cash moves*, not *what is owed*, so under accrual it is by definition a timing phenomenon and would classify as `EXPECTED_TIMING_DIFFERENCE`. It was nonetheless cut from the dataset, because `on_hold` is documented as transfer-scoped and `type = "transfer"` is excluded from the generator (§3.5) — generating holds on payment rows would apply a field to precisely the row type its documentation excludes, requiring a realism disclosure to defend a population that family 4's no-op already supplies. The `HOLD_OVERDUE` subtype was dropped with it.

### 3.4 Accounting templates — final count and posting-direction rules

**Six templates**, inside FR-04's 6–8 estimate. No correction to FR-04 is required on count.

| ID | Family | Debit legs | Credit legs | Amount source |
|---|---|---|---|---|
| `T-01` | 1 | `Payment Gateway Charges`, `GST on Gateway Charges` | `Razorpay Clearing` | `recon_line.fee`, `recon_line.tax`; credit leg = `fee + tax` |
| `T-02` | 2 | `Sales Returns and Allowances` | `Razorpay Clearing` | `recon_line.debit` |
| `T-03` | 3 | `Payment Gateway Charges`, `GST on Gateway Charges` | `Sales Revenue` | `recon_line.fee`, `recon_line.tax`; credit leg = `fee + tax` |
| `T-04` | 4 | `Razorpay Clearing` | `Bank Account` | the ledger's premature `Bank Account` debit amount |
| `T-05` | 5, credit adjustment | `Razorpay Clearing` | `Razorpay Settlement Adjustments` | `recon_line.credit` |
| `T-06` | 5, debit adjustment | `Razorpay Settlement Adjustments` | `Razorpay Clearing` | `recon_line.debit` |

`T-01` and `T-03` remain separate templates despite sharing debit legs. Merging them would force an *account choice* at instantiation time, which invariant 1.7.2 forbids the model from making. Two templates keeps the choice in classification, where it is gradeable.

Splitting family 5 into `T-05` and `T-06` is also what makes `Razorpay Settlement Adjustments` safe: no account is bidirectional *within* a template, so 1.7.5's posting-direction check retains real content. Direction is selected deterministically from which of `recon_line.debit` / `recon_line.credit` is non-zero, never by the model.

#### Two validation layers for invariant 1.7.5

**Per-template.** Each template declares an allowed debit-account set, an allowed credit-account set, and a required amount source per leg. Validation is set membership plus exact-amount derivation. No fuzzy matching.

**Global account-direction allowlist.** Nearly free, and it catches malformed templates, which the per-template check cannot: a broken template passes its own rules by definition.

| Account | Permitted direction |
|---|---|
| `Sales Revenue` | Credit only |
| `Sales Returns and Allowances` | Debit only |
| `Payment Gateway Charges` | Debit only |
| `GST on Gateway Charges` | Debit only |
| `Bank Account` | Credit only (v1: only `T-04` touches it) |
| `Razorpay Clearing` | Both permitted across templates; one direction fixed within each |
| `Razorpay Settlement Adjustments` | Both permitted across templates; one direction fixed within each |

Any candidate entry using an account outside the seven, or in a direction outside this table, is rejected before the balance check runs.

#### Four instantiation rules

**Zero-amount legs are omitted, not posted.** Razorpay's own samples show `tax: 0` on refund and AMEX lines, so a zero GST leg is live. A leg whose derived amount is 0 is dropped, provided the entry still balances and retains at least two legs. `T-01` with `tax = 0` collapses to `Dr Payment Gateway Charges / Cr Razorpay Clearing` — a legal instantiation of `T-01`, not a seventh template. Posting zero-value lines would produce ledger noise a reviewer would rightly flag.

**Aggregation: one entry per case per template.** A settlement case rolls up roughly eleven payments, each with its own fee and tax. The Controller posts a single aggregated entry per template per case, with amounts summed over the affected lines and every contributing record ID cited in the audit trail. This matches how a merchant actually books a settlement and keeps FR-11's reconciled-ledger diff legible on video; per-record evidence survives in the citation list rather than in the ledger. The cost is that a partially-wrong aggregate grades all-or-nothing against ground truth, which is the honest grading in any case.

**Multiple templates per case are permitted.** A case can carry both an unposted fee (`T-01`) and an unposted refund (`T-02`). 1.6's `expected_journal_entries` is already plural. `resolution_id` is unique per `(case_id, template_id)`, so invariant 1.7.4's idempotency guarantee holds with several entries on one case.

**Each template declares an evidence predicate.** Invariant 1.7.5 requires that all cited source records "exist and are unposted for this specific correction," which is only checkable against a formal predicate. These are data-model constraints, not matcher logic, and they double as the generator's contract in 3.5.

| Template | Evidence predicate |
|---|---|
| `T-01` | A settled `payment` recon line with `fee > 0`, no `Payment Gateway Charges` ledger entry referencing that `entity_id`, **and** a `Sales Revenue` credit referencing it whose amount equals gross `amount` *(the gross conjunct added in REV-16; without it, every family-3 case satisfies this predicate too)* |
| `T-02` | A settled `refund` recon line with `debit > 0`, and no `Sales Returns and Allowances` ledger entry referencing that `entity_id` |
| `T-03` | A settled `payment` recon line with `fee > 0`, and a `Sales Revenue` credit referencing it whose amount equals `amount − fee − tax` |
| `T-04` | A `Bank Account` debit dated at or near capture referencing the payment, **and** no `bank_line` credit matching the settlement UTR or net amount as of the snapshot |
| `T-05` | An `adjustment` recon line with `credit > 0` on a settled settlement, and no `Razorpay Settlement Adjustments` ledger entry referencing that `entity_id` |
| `T-06` | An `adjustment` recon line with `debit > 0` on a settled settlement, and no `Razorpay Settlement Adjustments` ledger entry referencing that `entity_id` |

**Predicates MUST be mutually exclusive per record.** `T-01` and `T-03` are distinguished solely by what the ledger's `Sales Revenue` credit says: gross `amount` selects `T-01` (revenue right, fee side missing), net `amount − fee − tax` selects `T-03` (revenue understated by exactly `fee + tax`). Both conjuncts are required. The pipeline MUST assert at instantiation time that at most one template predicate fires per `(case_id, entity_id)`; a double fire is a hard error, not a resolved-by-precedence situation, because the two templates post to different credit accounts and both instantiations balance — a wrong selection would pass 1.7.5's debit-equals-credit check and be caught only by the post-adjustment residual. *(REV-16.)*

### 3.5 Synthetic generation strategy

#### Record shape

**Payments per settlement:** truncated lognormal, `mu = ln(10)`, `sigma = 0.5`, clipped to `[3, 25]`. Mean lands near 11, mode near 10, and the upper tail reaches 25 rarely. All four values are generator config, satisfying the v0.3 requirement that a long-tail variant be a parameter change rather than a rewrite. Long-tail remains a stretch item.

Refunds are generated at roughly 6% of payments; adjustments at roughly 35 across the batch. `type = "transfer"` is **excluded from the generator entirely** — no family uses it, no chart-of-accounts entry exists for Route transfers, and generating them would create cases with no defined correct treatment. The enum value is retained in the `recon_line` schema for payload-shape fidelity.

**Money:** payment amounts lognormal, roughly ₹100 to ₹50,000, median near ₹1,500. Fee is 2% of amount, GST is 18% of fee, both rounded `ROUND_HALF_UP` to integer paise. The rounding convention is stated explicitly because 1.7.5's pass condition is a 0-paise residual, and an unstated convention is a silent test failure. The generator asserts `settlement.amount == sum(credits) − sum(debits) − fees − tax` across each settlement's lines as a hard invariant; the `SETTLEMENT_AMOUNT_MISMATCH` cases are the only deliberate violations.

**Seeds:** one seed drives the entire reference batch; a second, distinct seed drives the scale batch per FR-02. This traces NFR-01.

#### Case allocation — 125 settlement-anchored

| Population | n | Class | Expected state |
|---|---|---|---|
| Fully clean | 18 | `NONE` | `AUTO_MATCHED` |
| Family-4 no-op (lag within window) | 12 | `EXPECTED_TIMING_DIFFERENCE` | `AUTO_MATCHED` |
| Family 1 | 10 | `ACCOUNTING_CORRECTION` / `OMISSION` | `AUTO_CLOSED` |
| Family 2 | 10 | `ACCOUNTING_CORRECTION` / `OMISSION` | `AUTO_CLOSED` |
| Family 3 | 10 | `ACCOUNTING_CORRECTION` / `MISPOSTING` | `AUTO_CLOSED` |
| Family 4 | 10 | `ACCOUNTING_CORRECTION` / `MISPOSTING` | `AUTO_CLOSED` |
| Family 5 | 10 | `ACCOUNTING_CORRECTION` / `OMISSION` | `AUTO_CLOSED` |
| Family-4 date error | 5 | `ACCOUNTING_CORRECTION` / `MISPOSTING` | `REVIEW_REQUIRED`, `policy` |
| FR-06 tax positions | 12 | `ACCOUNTING_CORRECTION` | `REVIEW_REQUIRED`, `policy` |
| `SETTLEMENT_UTR_MISSING` | 5 | `OPERATIONAL_EXCEPTION` | `EXTERNAL_ACTION_REQUIRED` |
| `BANK_CREDIT_OVERDUE` | 5 | `OPERATIONAL_EXCEPTION` | `EXTERNAL_ACTION_REQUIRED` |
| `SETTLEMENT_AMOUNT_MISMATCH` | 4 | `OPERATIONAL_EXCEPTION` | `EXTERNAL_ACTION_REQUIRED` |
| `DISPUTE_PENDING` | 5 | `OPERATIONAL_EXCEPTION` | `EXTERNAL_ACTION_REQUIRED` |
| Ambiguous | 9 | `AMBIGUOUS_CASE` | `ABSTAINED` |

#### Label emission

**Labels come from the injection plan, never re-derived from generated records.** The generator selects a scenario, writes the ground-truth row, then writes records to match. Re-deriving labels by inspecting output would embed the same matching logic the pipeline uses, and the evaluation would be grading a mirror of itself.

**Fingerprint control.** The failure mode that matters is anomalous cases becoming identifiable by *artifact* rather than by *evidence* — sequential IDs assigned per scenario, timestamps generated in scenario blocks, narration strings unique to one anomaly type. Any of these silently inflates every metric in 1.6. Mitigation: generate all records first, then assign IDs and timestamps in a single global shuffled pass, and draw narration text from one shared pool regardless of scenario.

#### Anomaly enrichment — disclosure requirement

30 of 150 cases require no action at all. Real settlement reconciliation runs at a break rate closer to low single digits, so this batch is roughly an order of magnitude anomaly-enriched. That is the correct choice — a realistic mix would leave some outcome states with two or three cases and make every per-state metric unreadable — but it has a consequence that must be stated rather than discovered:

> **`match_rate` on this batch is not comparable to any industry figure.** The batch is deliberately anomaly-enriched for metric legibility. The enrichment factor MUST be stated in the README, in the report header of the FR-11 HTML artifact, and in the pitch video, alongside the observation that `EXTERNAL_ACTION_REQUIRED` runs high (roughly 21%) because orphan cases are unresolvable by construction.

No second realistic-mix batch is generated. It would double generator work for a number that would need the same caveat anyway.

### 3.6 Orphan-case generation

25 non-settlement-anchored cases, drawn from the noise universe §2.2 already names.

| Population | n | Class / subtype | Expected state |
|---|---|---|---|
| Inbound NEFT, counterparty named in narration | 8 | `OPERATIONAL_EXCEPTION` / `UNMATCHED_INBOUND_CREDIT` | `EXTERNAL_ACTION_REQUIRED` |
| Inbound credit, opaque narration | 8 | `AMBIGUOUS_CASE` | `ABSTAINED` |
| Bank reversal with no matching prior credit | 6 | `OPERATIONAL_EXCEPTION` / `REVERSAL_UNMATCHED` | `EXTERNAL_ACTION_REQUIRED` |
| Duplicate credit, same UTR twice | 3 | `OPERATIONAL_EXCEPTION` / `DUPLICATE_CREDIT` | `EXTERNAL_ACTION_REQUIRED` |

**Granularity:** one case per bank line, except that a duplicate credit and the original credit carrying the same UTR form a single case. The three `DUPLICATE_CREDIT` cases therefore span six bank lines. *(Corrected in REV-18. v0.4's exception named a reversal and its original credit, but the `REVERSAL_UNMATCHED` population is defined by the absence of a matching prior credit, so that exception covered no cases while the one it was needed for went unstated.)*

**Bank charges stay noise, not cases.** §2.2's noise list describes *bank-statement noise* — lines the matcher must correctly ignore — which is a different job from lines that must each become a closeable case. Bank charge lines are generated, they exercise the matcher's ignore path, and they do not form reconciliation cases. This keeps the chart of accounts at seven, keeps the template count at six, and keeps FR-04 at exactly five families. The alternative considered and rejected was adding a `Bank Charges` expense account plus a `T-07` template, which would have required a revision expanding locked FR-04 to a sixth family for the cheapest correction in the batch.

**Ledger-side orphans were cut.** A ledger entry referencing a non-existent settlement would have exercised a separate case-assembly path, but for two cases, and it is the least realistic scenario in the set.

#### Batch totals

| Outcome state | Cases | Share |
|---|---|---|
| `AUTO_MATCHED` | 30 | 20.0% |
| `AUTO_CLOSED` | 50 | 33.3% |
| `REVIEW_REQUIRED` | 17 | 11.3% |
| `EXTERNAL_ACTION_REQUIRED` | 36 | 24.0% |
| `ABSTAINED` | 17 | 11.3% |
| **Total** | **150** | |

Every FR-04 family holds 10 cases. Every `OPERATIONAL_EXCEPTION` subtype holds at least 3, and the six subtypes divide 36 cases at roughly 6 each — thin, and stated as such: Section 5 must report `exception_subtype_precision` and `exception_subtype_recall` (1.6, REV-20) with their per-subtype denominators visible rather than as a single headline number.

## 4. Architecture and tooling

**Status:** locked. Written at deliberately lower depth than Section 3. Section 3 defined the data contract, where a wrong decision forces regenerating everything downstream; architecture does not have that property, so anything that can be decided better after seeing code run is deferred here and marked as deferred rather than guessed at.

### 4.1 Component decomposition

Ten components, one direction of data flow, no cycles. The generator is a separate entry point, not a pipeline stage — the pipeline never imports it, which is what keeps generator logic out of the thing being graded.

| # | Component | Job |
|---|---|---|
| G | Generator | Emits reference / held-out / scale batches plus ground-truth labels from a seed (§3.5, §3.6) |
| 1 | Adapters | FR-08 declarative column mapping → canonical `bank_line`; loaders for `recon_line`, `settlement`, `ledger_entry` |
| 2 | Case assembly | Recon lines grouped by `settlement_id` → settlement-anchored cases; residual bank lines → orphan cases (§1.2) |
| 3 | Matcher | FR-09 tier cascade, T+2 settlement window, integer-paise residual |
| 4 | Predicate evaluator | The six §3.4 evidence predicates plus the `OPERATIONAL_EXCEPTION` subtype triggers (§3.3) |
| 5 | Classifier | Exception class and subtype assignment |
| 6 | Instantiator | Template → candidate JV; deterministic amount derivation, zero-leg omission, per-case aggregation (§3.4) |
| 7 | Validator | The invariant 1.7.5 chain plus both §3.4 validation layers |
| 8 | Apply and re-reconcile | Ledger write under the 1.7.4 idempotency constraint, residual recheck, terminal state assignment |
| 9 | Reporter | Metric surface against ground truth, the five §1.8 artifacts, single-file HTML per FR-11 |

Components 4 and 6–8 are the invariant-bearing core. Component 5 is the only one carrying a model on the graded path.

### 4.2 Deterministic versus LLM slots

Invariant 1.7.2 permits the model to classify which template applies. §3.4 then made the template evidence predicates formal and deterministic — "these are data-model constraints, not matcher logic" — and REV-16 made them mutually exclusive. Once that is true, predicate evaluation already returns the template. A model classifier layered on top of a deterministic function that has the answer cannot raise accuracy; it can only introduce disagreement, and every disagreement must resolve in favour of the predicate or the safety invariant is decorative.

**Template selection is therefore not an LLM slot, even though 1.7.2 would allow it.** That permission bounds what the model may do; it does not oblige the system to use it. The model earns its place in exactly one graded slot, one ungraded slot, and a third that is deferred.

**Slot A — exception subtype classification on non-`AUTO_CLOSED` cases. LLM. Graded.**
This is the one place in the system where the correct answer is not derivable from arithmetic. `UNMATCHED_INBOUND_CREDIT` versus `AMBIGUOUS_CASE` on an orphan bank credit turns entirely on whether the free-text narration identifies a counterparty (§3.6 splits 16 orphan cases on exactly that line), and no residual computation decides it. The model receives a structured evidence bundle and returns one value from an eight-value enum — the seven `OPERATIONAL_EXCEPTION` subtypes plus `AMBIGUOUS_CASE` — under constrained decoding. It never sees or emits an account, an amount, or a postable narration. Fires on roughly 70 of 150 cases. Graded by `exception_subtype_precision` and `exception_subtype_recall` (§5.2).

**Slot B — resolution text, abstention rationale, per-case reasoning prose. LLM. Ungraded, off the money path.**
`EXTERNAL_ACTION_REQUIRED` requires a recommended external action in readable English (§1.3); `ABSTAINED` requires a rationale. Both are language tasks over facts the deterministic path has already fixed. The FR-11 report MUST label every Slot B string as model-generated prose over deterministic facts, so narration can never be mistaken for evidence in the audit trail.

**Slot C — FR-06 policy-exclusion detection. Deterministic in v1; LLM deferred.**
A 194-O deduction has a signature in the adjustment line and a predicate probably suffices. If the predicate proves brittle against the generator's narration variety, an LLM gate is safe here specifically because the output routes to `REVIEW_REQUIRED` regardless of what it says. **Deferred: this is decidable better after the first run than now.**

**Everything else is deterministic**, including adapters, case assembly, the full FR-09 cascade, all six evidence predicates, template instantiation, amount derivation, the validator chain, ledger apply, and re-reconciliation.

**Considered and cut: LLM triage of cases where no predicate fires.** A case with a non-zero residual and no firing predicate terminates in `REVIEW_REQUIRED` or `ABSTAINED` whichever way a model reads it, so the output changes nothing a reviewer or a judge sees. Cut on the §7 rule that a component which changes neither the build nor the demo is not built.

**Stated risk.** A reviewer skimming may read "one graded LLM slot" as a thin AI project. The counter is the published judging line — *the right tool in the right place, and where you chose not to use one* — which is the only criterion that explicitly rewards restraint. The defensible position is that the model does not touch the money path, an invariant forbids it from doing so, and the one slot where it does work reports a measured number against a deterministic baseline (§5.4). This is a deliberate bet, recorded as one.

### 4.3 Determinism across the LLM slot (NFR-01)

Temperature 0 with a fixed seed is necessary and insufficient: no inference provider guarantees bitwise reproducibility across batching and kernel scheduling. Three layers:

1. **Constrained decoding to the eight-value enum.** Even under drift the output space is eight values, so nondeterminism is bounded rather than open-ended.
2. **A SHA-256-keyed prompt/response cache, committed to the repository.** The key is the hash of the exact prompt string. The eval path runs `--llm-cache=strict`, where a cache miss is a hard error rather than a fallthrough to the API. `--llm-cache=refresh` is the only mode that calls Fireworks.
3. **The cache is committed alongside the pinned run** required by FR-13, and cache hit rate is reported in the metrics JSON.

This makes NFR-01 literally rather than approximately true, makes NFR-05's offline mode real, and lets a judge reproduce the committed run with no Fireworks credentials and no credits. Roughly forty lines of code.

> **Stretch, only if Phase 5 finishes early:** run Slot A three times against a cold cache on the same batch and report the observed disagreement rate as one number. That converts "nondeterminism is bounded" from a claim into a measurement. Optional; cut without ceremony if the day is tight.

### 4.4 Model choice

**`llama-v3p3-70b-instruct` on Fireworks, primary.** The model ID is a config parameter, not a literal, so a swap is a one-line change; one head-to-head against a Qwen3 model is run after the first real batch, not before. Rationale: strong instruction-following on constrained enum classification with structured output, and dense models above 16B are priced flat per million tokens on Fireworks' serverless tier — the entire eval workload across every development run is a few million tokens.

**The $120 budget is not a binding constraint** and MUST NOT drive the choice. Accuracy on Slot A is the only selection criterion.

> Assumption: Fireworks deprecates model versions on its own schedule. The exact model ID MUST be re-checked against the live catalog before it is pinned under FR-13, and the pinned ID is recorded in the metrics JSON alongside seed and SHA.

### 4.5 Tech stack and storage

Stated, not argued.

- **Python 3.11+**, dependencies via `uv`.
- **Pydantic v2** for the four canonical schemas in §3.1.
- **Money:** `int` paise end to end behind a `Paise = NewType('Paise', int)` alias. `decimal.Decimal` with `ROUND_HALF_UP` appears only inside the generator's fee and GST rounding and is cast to `int` immediately. No float touches matching, residual computation, or JV derivation (NFR-04).
- **Storage:** **SQLite** for the synthetic ledger, with a `UNIQUE(case_id, resolution_id)` constraint at the schema level. This is the reason for the choice: invariant 1.7.4's idempotency guarantee becomes a database constraint rather than an application check, which is the difference between an invariant and a convention. Raw `sqlite3`, no ORM. Inputs and ground truth as JSONL.
- **Bank statements:** `pandas` plus `openpyxl` for CSV and XLSX. The three FR-08 profiles are YAML column maps, not code.
- **Report:** Jinja2 rendering one HTML file with inlined CSS and a JSON blob in a `<script>` tag, filtered by vanilla JS. No CDN, no build step, no external fetch (FR-11).
- **CLI:** `typer`.
- **Tests:** `pytest`, concentrated on the validator chain and the six templates.
- **LLM:** the `openai` SDK against the Fireworks OpenAI-compatible endpoint.

### 4.6 FR-09 UTR fallback matching

A four-tier cascade, first hit wins, with the winning tier recorded in the audit trail for every matched case.

| Tier | Rule |
|---|---|
| 0 | `settlement.utr` appears as a token in `bank_line.narration`, or equals `bank_ref_no`, after uppercasing and stripping non-alphanumerics |
| 1 | Extract every alphanumeric token of length ≥ 8 from the narration; accept if a token is a contiguous prefix of the settlement UTR of length ≥ 8 (embedded or truncated UTR) |
| 2 | A bank credit whose `deposit_paise` equals `settlement.amount` exactly, with `value_date` inside the T+2 working-day window plus one slack day |
| 3 | No match. Inside the window → `EXPECTED_TIMING_DIFFERENCE` / `AUTO_MATCHED`. Past it → `OPERATIONAL_EXCEPTION` / `BANK_CREDIT_OVERDUE` |

**Tier 2 is accepted only if exactly one candidate exists in the window.** A tie is not a match; it routes to ambiguity. This uniqueness rule is also what surfaces `DUPLICATE_CREDIT`.

**Amount comparison is exact integer paise with no tolerance band.** Invariant 1.7.1 requires exactness, and a tolerance would manufacture false matches — the primary safety metric.

**Generator obligation.** The cascade is only tested if the narration varies. The generator MUST produce roughly 50% clean UTR, 25% embedded, 15% truncated, 10% absent. Without this, FR-09's "no clean join" premise is asserted rather than exercised.

**`match_tier_distribution`** — the count of matches at each tier — is reported in the metrics JSON. One line of output, and it is the evidence that the cascade does real work rather than degenerating to tier 0.

## 5. Evaluation methodology

**Status:** locked, with every threshold in 5.5 explicitly provisional.

### 5.1 Held-out set composition

Worth stating what "held-out" means in a system where nothing is fitted. For the deterministic path it is close to vacuous — no parameters are learned. It is not vacuous for Slot A, whose prompt is hand-tuned, or for the thresholds in 5.5, which are hand-set. Three batches:

| Batch | Seed | Cases | Use |
|---|---|---|---|
| Development | 1 | 150 | Fully visible. Debugging, prompt iteration, threshold setting |
| Held-out | 2 | 150 | Same generator, same distribution. Generated once and **not inspected case by case during prompt tuning**. Headline metrics are reported on this batch |
| Scale | 3 | ~360 | `throughput` only, per FR-02. Not labelled or analysed |

Both development and held-out metrics are reported side by side. **A gap between them is itself a finding** and is printed in the report rather than explained away.

**The rule that gives this teeth:** any prompt or threshold change made in response to inspecting held-out cases MUST be logged in `BUILDLOG.md` with the reason. Peeking is permitted; unrecorded peeking is not. A held-out set that has been quietly tuned against is worse than no held-out set, because it carries authority it has not earned.

### 5.2 Exception-classification metrics — the recall counterpart

§2.11 flagged that `exception_classification_accuracy` is conditioned on cases the system *placed* in `EXTERNAL_ACTION_REQUIRED`, making it precision-flavoured and blind to cases that should have landed there and did not. It is also, by REV-09's own denominator convention, misnamed: a metric over the predicted population is a `*_precision`.

- **`exception_subtype_precision`** *(renamed from `exception_classification_accuracy`)* — among cases the system assigned subtype S, the fraction whose ground-truth subtype is S.
- **`exception_subtype_recall`** *(new — this is the counterpart §2.11 asked for)* — among cases whose ground-truth subtype is S, the fraction the system assigned S.

**Both MUST be reported per subtype with denominators visible, plus a macro average across the seven subtypes.** §3.6 already requires this: six subtypes divide 36 cases at roughly six each, and a single headline number over denominators that thin would be dishonest.

Two additions, both cheap and both directly on the judging bar:

- **A 5×5 outcome-state confusion matrix** over the five states in §1.3.
- **A 5×5 exception-class confusion matrix** over the four classes plus `NONE`.

Both render in the FR-11 HTML report. Around fifteen lines of code between them, and they are the most legible artifact available to a judge who wants to see where the system is actually wrong rather than a headline that hides it.

**Cut:** ROC curves, per-family ablation grids, calibration plots. None changes what gets built or what a judge sees.

### 5.3 Synthetic-eval disclosure (deferred from §2.11)

The generator emits ground truth from its own injection plan (§3.5), and the eval grades the pipeline against that same plan. This is the standard synthetic-eval limitation and it caps how far the headline numbers can be trusted.

> **Disclosure, required in the README, the FR-11 report header, and the pitch video, alongside the anomaly-enrichment disclosure from §3.5:** ground-truth labels and the records being graded come from one generator. The evaluation measures whether the pipeline recovers the injected intent; it does not establish that the injected intent resembles a real merchant's books.

Two mitigations, both worth their hours:

**1. Fingerprint assertions, promoted to a reported check.** §3.5's controls — IDs and timestamps assigned in a single global shuffled pass, narration drawn from one shared pool regardless of scenario — are asserted in code, and the metrics JSON carries a pass/fail line confirming that no ID ordering or timestamp block correlates with scenario. If anomalous cases are identifiable by artifact rather than by evidence, every metric in §1.6 is silently inflated, so this check gates the headline numbers rather than sitting beside them.

**2. A hand-authored adversarial set of 10–12 cases.** Written by hand with hand-written labels, not produced by the generator, and targeted at the boundaries the v0.5 verification pass exposed: `T-01` versus `T-03` (REV-16), family 4 proper versus its date-error variant, duplicate credit versus reversal (REV-18), and at least one case designed to be genuinely unresolvable. **Reported separately and never mixed into headline metrics.** This is the only independent ground truth in the project and it is the direct answer to the objection that the eval grades a mirror of itself. Roughly 1.5 hours, in Phase 7.

### 5.4 Ablation

**One ablation, conditional: Slot A off.** Replace the LLM classifier with the deterministic keyword baseline, rerun the held-out batch, and report the delta on `exception_subtype_recall` (macro).

This answers the *AI judgment* row of the published judging criteria with a number rather than a claim. It costs about an hour, and it is never wasted work because Phase 5 builds the baseline first and the LLM on top of it (§6). Committed if Phase 6 finishes on schedule; cut otherwise, and the cut is disclosed.

No other ablation is run. Model swaps and prompt variants produce numbers that change neither the build nor the demonstration.

### 5.5 Target thresholds — provisional

**Every figure below is provisional and is set properly after the first real run against the development batch.** Publishing them before the run still buys something: any change must be logged in Section 8 with its reason, so a moved goalpost is visible rather than silent.

| Metric | Provisional target | Note |
|---|---|---|
| `false_match_rate` | 0 | Primary safety metric. Any non-zero value is investigated and reported case by case, never as a rate |
| `auto_close_precision` | ≥ 0.98 | Primary safety metric for adjustment. The 1.7.5 chain should make 1.00 reachable; whether it holds is the interesting question |
| `auto_match_precision` | ≥ 0.95 | |
| `auto_close_recall` | 0.80 – 0.95 | Nothing structural caps this — all 50 auto-close cases are in scope — so a low value means detection weakness, not policy discipline |
| `auto_match_recall` | 0.85 – 0.95 | |
| `state_prediction_accuracy` | 0.80 – 0.90 | |
| `exception_subtype_recall`, macro | 0.70 – 0.85 | The Slot A metric. Thin per-subtype denominators; read with §5.2's breakdown, not alone |
| `exception_subtype_precision`, macro | 0.75 – 0.90 | |
| `abstention_rate` | operating range 8 – 18% | Ground truth is 11.3% (§3.6). Below 8% suggests the system is forcing calls it should decline; above 18%, over-abstention degrading value |
| `declined_by_policy_rate` | ≈ 11.3% | By construction, not by performance. A large deviation is a bug in policy routing, and MUST be read as one |
| `match_rate` | reported, no target | Not comparable to any industry figure — see the §3.5 enrichment disclosure |
| `value_coverage` | reported, no target | |
| `throughput`, `end_to_end_latency` | reported, no target | Hardware stated alongside (NFR-02, NFR-03) |

### 5.6 Reproducibility protocol

1. The pinned run records generator seed, git SHA, Fireworks model ID, and the metrics JSON (FR-13).
2. The LLM cache for that run is committed (§4.3), so `--llm-cache=strict` reproduces it with no network.
3. A clean-clone reproduce test runs in Phase 7 and MUST produce a metrics JSON byte-identical to the committed one (NFR-01, NFR-06).
4. Anything shown in the video exists in the repository at that SHA (FR-12's governing rule).

## 6. Phase plan

**Status:** locked. Seven days, Aug 28 → Sept 3, six hours each, 42 hours total (§2.0). Sept 4–5 is contingency and holds no planned work.

**Checkpoint principle:** every checkpoint below is an assertion in code or a committed artifact. "Looks right" is not a checkpoint. A phase is not complete until its checkpoint passes.

**Cut order (§2.0), restated so that falling behind triggers a known cut rather than improvisation:** drop family 5 → drop the third bank format → reduce the reference batch to 100 cases. Each phase below names which cut fires if it overruns.

### Phase 1 — Day 1, Aug 28. Skeleton, schemas, generator core

Repository, `uv` environment, `AGENT.md`, `BUILDLOG.md`, package layout. The four §3.1 schemas as Pydantic models. SQLite DDL including `UNIQUE(case_id, resolution_id)`. Generator: record shape, money and rounding, seed handling, clean-case path only.

**Checkpoint.** `generate --seed 1` runs end to end. §3.5's settlement invariant `settlement.amount == sum(credits) − sum(debits) − fees − tax` asserts true on every settlement. The generated ledger balances globally: Σ debits == Σ credits, in integer paise.

### Phase 2 — Day 2, Aug 29. Anomaly injection, all populations, ground truth

All fourteen settlement-anchored populations (§3.5) and all four orphan populations (§3.6). Labels emitted from the injection plan, never re-derived. Global shuffled ID and timestamp pass. Shared narration pool. The §4.6 UTR narration variety split.

**Checkpoint.** Population counts match the §3.5 and §3.6 tables exactly, asserted in code rather than checked by eye. Fingerprint assertions (§5.3) pass. Ground truth validates against the §1.6 schema. Bank-line decomposition matches REV-17: ~98 settlement credits, ~28 orphan lines, ~50 noise.

**Highest-risk phase.** If it overruns, the first cut fires: **drop family 5**, removing 10 cases and templates `T-05` and `T-06`.

### Phase 3 — Day 3, Aug 30. Adapters, case assembly, matcher

Three FR-08 bank profiles as YAML column maps. CSV and XLSX, junk header rows, trailing summary blocks, both date formats, comma-grouped amounts. Case assembly, settlement-anchored and orphan. The §4.6 four-tier cascade, T+2 working-day window, integer-paise residual.

**Checkpoint.** All 150 cases assemble. `match_tier_distribution` prints and shows matches at more than one tier. Every clean case has residual 0. `AUTO_MATCHED` fires on the expected 30 cases.

**If behind:** cut the Axis profile — about 45 minutes recovered.

### Phase 4 — Day 4, Aug 31. The core: predicates, templates, validator, apply, re-reconcile

Six evidence predicates including the REV-16 mutual-exclusivity assertion. Template instantiation with zero-leg omission and per-case aggregation (§3.4). Both §3.4 validation layers and the full 1.7.5 chain. Ledger apply under the idempotency constraint. Re-reconciliation.

**Checkpoint.** First end-to-end run producing all five terminal states. Every `AUTO_CLOSED` case shows a 0-paise post-adjustment residual. Running the same batch twice posts nothing on the second pass. No `(case_id, entity_id)` fires more than one template predicate.

**This phase cannot be cut.** If it overruns, hours come from Phase 6, not from Phase 5.

### Phase 5 — Day 5, Sept 1. Classification slot and the prompt cache

Build the **deterministic keyword baseline first**, then Slot A on top of it. That ordering matters twice: falling behind degrades to a disclosed baseline rather than to nothing, and the baseline is the §5.4 ablation arm regardless, so it is never wasted work. Then the SHA-keyed cache with strict mode (§4.3), then Slot B text with its model-generated labelling.

**Checkpoint.** `--llm-cache=strict` produces identical metrics on two consecutive runs. The cache is committed. A run with networking disabled succeeds end to end (NFR-05).

### Phase 6 — Day 6, Sept 2. Metrics, eval harness, report

The full §1.6 surface plus both §5.2 confusion matrices and the per-subtype breakdown. Held-out batch generated and run. Thresholds reviewed against §5.5, with any change logged. The single-file HTML report carrying all five §1.8 artifacts.

**Checkpoint.** Metrics JSON committed. The HTML opens from a `file://` URL with networking disabled and renders completely. The development-versus-held-out gap is recorded in `BUILDLOG.md`.

**If behind:** cut the reconciled-ledger diff view down to a committed CSV, keeping metrics, case log, exception list, and audit drill-down in HTML. **Last resort:** reduce the reference batch to 100 cases.

### Phase 7 — Day 7, Sept 3. Scale, adversarial set, pin, record

Scale-batch throughput run (FR-02). The §5.3 hand-authored adversarial set. The §5.4 ablation if time permits. Pin seed, SHA, model ID, and metrics JSON per FR-13. Clean-clone reproduce test. Record the five-minute video.

**Checkpoint.** A clean clone, one documented command, metrics byte-identical to the committed JSON (NFR-06).

### Documentation is written incrementally, not on Day 7

Phase 7 as first sketched also carried `README.md` and `ARCHITECTURE.md`, and it does not fit — a five-minute video with retakes consumes two hours by itself. Instead, both documents are written **twenty minutes at the end of each day, drawn from that day's `BUILDLOG.md` entries.** This keeps them inside the daily six hours and means they describe what was built rather than what was planned.

### 6.1 `AGENT.md`

The standing brief every coding session opens with. Contents:

- The project one-liner, and `spec.md` named as the single source of truth.
- Invariants 1.7.1 through 1.7.5, restated verbatim. These are what a coding session will silently violate under time pressure.
- The money rule: integer paise end to end, no floats, ever.
- The determinism rules: no unseeded `random`, no `datetime.now()` anywhere in the pipeline, the batch snapshot date is a parameter.
- Invariant 1.7.2 in full: the model may classify which template applies; it may never originate an account, an amount, or a narration on the auto path.
- Repository layout and the command surface.
- The current phase pointer and the §2.0 cut order.
- One instruction that outranks the rest: **do not invent scope. If the spec does not cover it, stop and ask.**

### 6.2 `BUILDLOG.md`

Append-only, one entry per session, five fixed subheads: **Built / Broke / Cut / Decided / Next.**

It does three jobs. It feeds the incremental README and ARCHITECTURE writing above. It is the raw material for the video's "what broke and what I did about it" segment — the *Failure recovery* row of the published judging criteria, answered from a contemporaneous artifact rather than from memory a week later. And, per §6.3, its **Next** field is the handoff between implementation sessions, which have no memory of each other.

Anything decided during implementation that the spec does not cover is recorded under **Decided**. Anything that contradicts a locked section stops the session and becomes a Section 8 revision instead.

**The `Next` field is mandatory and MUST be specific.** "Continue Phase 4" is not a handoff. "The validator chain exists but re-reconciliation is unwritten; `apply_and_reconcile()` in `pipeline/apply.py` is a stub" is.

### 6.3 Session decomposition

*(Added in REV-21.)*

§6's phases are **days, not sessions.** Six hours is three or four implementation sessions with a context reset between each, and a phase handed over as a single unit — Phase 4 especially, which spans predicates, templates, validator, apply and re-reconciliation — will run long, compact mid-session, and lose invariant discipline at precisely the point where it matters most.

**Session boundary rule.** A session ends where one artifact exists, one checkpoint runs, and the work can be committed and walked away from. Not where the clock says two hours. If the checkpoint does not pass, the session is not complete, and half-built work is not carried into the next session's context.

**Sessions are stateless.** Each implementation session starts blind. `AGENT.md` (§6.1) carries the standing rules; the previous entry's **Next** field carries the state. Between the two, the next session's prompt is reconstructable without any memory of the last one. Every session starts from a cleared context rather than continuing the previous one.

**Model selection criterion: how silent the failure mode is, not how hard the work is.** A bug that crashes is cheap — the checkpoint catches it. A bug that produces a plausible wrong number is expensive, and the v0.4 verification pass found fifteen defects of exactly that kind across locked sections. The strongest available model is therefore assigned to the five sessions where a wrong answer looks right, and not distributed by apparent difficulty.

| Day | Session | Artifact | Checkpoint | Model |
|---|---|---|---|---|
| 1 | 1.1 | Skeleton, `uv`, package layout with generator/pipeline import guard, `AGENT.md`, BUILDLOG template | Import guard test fails if `pipeline` imports `generator` | Haiku |
| 1 | 1.2 | Four §3.1 schemas, `Paise` alias, SQLite DDL with `UNIQUE(case_id, resolution_id)` | Round-trip per schema; the unique constraint rejects a duplicate insert | Sonnet |
| 1 | 1.3 | Generator core, clean path, seeded RNG, `ROUND_HALF_UP` money | The three Phase 1 assertions | Sonnet |
| 2 | 2.1 | The five FR-04 family injections | Per-family counts assert to 10 | Sonnet |
| 2 | 2.2 | Exception, tax, ambiguous and orphan populations | §3.5 and §3.6 tables assert exactly; REV-17 bank-line split holds | Sonnet |
| 2 | 2.3 | Global shuffle pass, shared narration pool, UTR variety, fingerprint assertions | Fingerprint checks pass; no ID or timestamp block correlates with scenario | **Opus** |
| 3 | 3.1 | Bank adapter, three YAML profiles, junk headers, summary blocks, dates, comma amounts | All three profiles parse to an identical canonical `bank_line` | Sonnet |
| 3 | 3.2 | Case assembly, settlement-anchored and orphan | 150 cases assemble; orphan granularity matches REV-18 | Sonnet |
| 3 | 3.3 | Four-tier cascade, T+2 window, residual, `match_tier_distribution` | Matches at more than one tier; 30 cases reach `AUTO_MATCHED` | Sonnet |
| 4 | 4.1 | Six evidence predicates plus the REV-16 mutual-exclusivity assertion | No `(case_id, entity_id)` fires two predicates | **Opus** |
| 4 | 4.2 | Instantiator: amount derivation, zero-leg omission, per-case aggregation | Every candidate JV balances; a `tax = 0` `T-01` collapses correctly | Sonnet |
| 4 | 4.3 | Full 1.7.5 chain, both §3.4 layers, ledger apply, idempotency, re-reconcile | All five states produced; 0-paise residual on every `AUTO_CLOSED`; second run posts nothing | **Opus** |
| 5 | 5.1 | Evidence bundle builder plus the deterministic keyword baseline | Baseline classifies all ~70 non-auto-close cases without crashing | Sonnet |
| 5 | 5.2 | Slot A, constrained decoding, SHA-keyed cache, strict and refresh modes | Two consecutive strict runs give identical metrics | Sonnet |
| 5 | 5.3 | Slot B text, model-generated labelling, offline verification | Full run with networking disabled succeeds | Sonnet |
| 6 | 6.1 | Full §1.6 metric surface against ground truth | Denominators hand-checked against §3.6's batch totals | **Opus** |
| 6 | 6.2 | Confusion matrices, per-subtype breakdown, held-out run, threshold review | Dev-versus-held-out gap recorded in BUILDLOG | **Opus** |
| 6 | 6.3 | Jinja2 single-file HTML report, five §1.8 artifacts | Opens from `file://` with networking off | Sonnet |
| 7 | 7.1 | Scale batch, throughput, hand-authored adversarial set | Adversarial set runs and is reported separately | Sonnet |
| 7 | 7.2 | Ablation if time, docs finalised, pin seed/SHA/model/metrics, clean-clone test | Clean clone reproduces the metrics JSON byte-identically | Sonnet |

Session 7.3 is recording the video and is not an implementation session.

**Why those five sessions and not others.** 2.3 — if anomalous cases become identifiable by artifact rather than evidence, every metric in §1.6 inflates and nothing visibly breaks. 4.1 — a predicate overlap passes 1.7.5's balance check, as REV-16 established. 4.3 — the validator chain is what makes the safety invariants real, and a check that passes when it should not is undetectable from output. 6.1 and 6.2 — denominator errors are the exact defect class REV-01 and REV-09 corrected, and shipping numbers that overstate the system is the one failure the judging bar names directly.

### 6.4 Version control protocol

*(Added in REV-22. FR-12, FR-13, NFR-01 and NFR-06 already fix what the repository must contain and what the pinned run must reproduce; this subsection covers cadence, tagging and secrets, which they do not.)*

**Cadence: one commit per passing checkpoint, at minimum.** This falls out of §6.3's session boundary — checkpoint passes, BUILDLOG entry is written, commit. Intra-session commits at logical points are encouraged; the checkpoint commit is the one that MUST exist. With nineteen stateless sessions, a commit per checkpoint is the only cheap rollback available when a session goes wrong.

**Tag each phase completion** `phase-1` through `phase-7`. If Phase 4 fails on Day 4, `git reset --hard phase-3` must be a thirty-second decision rather than an archaeology exercise. The §2.0 cut order fires at phase granularity, so the tags are the recovery points the cut order assumes.

**Commit messages reference the session number and the checkpoint**, not just the change — `1.1: import guard rejects pipeline→generator imports`. The log then reads as a build narrative, which is what the README and ARCHITECTURE increments (§6) are written from. The history is also evidence for the *Build quality* and *Failure recovery* judging rows: a log showing a validator fixed because a checkpoint caught it is worth more than a tidy one, so failed checkpoints and their fixes are committed rather than squashed away.

**The implementation session stages and proposes; the builder commits.** An agent that commits unreviewed will eventually commit something that passes its own checkpoint and violates an invariant.

**Committed, ignored, never committed.**

| | Contents |
|---|---|
| **Committed** | The seeded reference dataset and its ground-truth labels (FR-12 — a reviewer who never runs the code cannot regenerate them, so regenerability is not an argument here). The LLM prompt/response cache (§4.3). The metrics JSON for the pinned run (FR-13). The hand-authored adversarial set (§5.3 — it is source, not output; nothing regenerates it). The three YAML bank profiles. `.env.example`. |
| **Gitignored** | The SQLite ledger, which is mutated by every run and rebuilt from the committed dataset. `.venv`, `__pycache__`, and run scratch output. |
| **Never committed** | The Fireworks API key. `.env` enters `.gitignore` in **Session 1.1**, four days before Session 5.2 creates a key to leak — by then `.gitignore` is something the builder has not thought about in four days. |

**Repository visibility.** Public from Session 1.1. It makes the commit history legible as build evidence, and it removes a last-day flip as a failure mode. Nothing here is private: the data is entirely synthetic (FR-01a) and the approach is described in a public track brief.

> **Recorded honestly:** six hours a day assumes six *productive* hours. Nineteen implementation sessions will not all land first time, and this plan holds no slack for the two or three that need redoing. That is what the §2.0 cut order exists for, and Phase 2 and Phase 6 are where it will fire first.

## 7. Out of scope

*(To be finalized after Sections 3–6. Section 2.10 holds the current non-goals list.)*

---

## 8. Revision log

Revisions to locked sections. Required by the convention in Section 0.

### REV-01 — `match_rate` definition corrected *(v0.2, Aug 27, 2026 — revises locked §1.6)*

**Issue.** v0.1 defined `match_rate` as correctly-`AUTO_MATCHED` cases over *total cases whose ground truth is `AUTO_MATCHED`*, and labelled it "classic finance definition." That formula is recall over the auto-matchable population. The classic finance definition uses total cases as the denominator. The name and the formula disagreed, and reporting a recall figure under a name finance reviewers read as batch-level match rate would overstate the result.

**Change.** `match_rate` is redefined with total cases as the denominator. The original formula is retained under the accurate name `auto_match_recall`. Both are now reported.

### REV-02 — deferral metrics split by cause *(v0.2, Aug 27, 2026 — revises locked §1.3 and §1.6)*

**Issue.** Section 2.5 excludes tax-position cases from the auto-close path by policy. Those cases land in `REVIEW_REQUIRED` alongside cases deferred for low confidence. Reported as a single number, deliberate scope discipline is indistinguishable from weak detection, and `unresolved_rate` reads worse than the system's actual behavior warrants.

**Change.** `declined_by_policy_rate` and `declined_by_confidence_rate` added to §1.6. §1.3's `REVIEW_REQUIRED` definition updated to name both routing causes.

### REV-03 — deadline and header corrected *(v0.2, Aug 27, 2026 — revises header)*

**Issue.** v0.1 recorded the Sept 5 deadline as unconfirmed, sourced only from participant communications, and noted that the public Buildathon page did not state a date. The page does state it, in the hero: "You have from today till 5 September." The v0.1 note was wrong on a checkable fact.

**Change.** Deadline recorded as confirmed from primary source. Sept 3 retained as internal ship target with Sept 4–5 as unplanned contingency. `Today` advanced to Aug 27, 2026.

### REV-04 — recon-line field list corrected against primary source *(v0.3, Aug 27, 2026 — revises locked §2.1 / FR-01a)*

**Issue.** FR-01a's field list for the Razorpay recon-combined payload (`GET /v1/settlements/recon/combined`) was written without verification against Razorpay's own API documentation. Checked in this chat against the live pages `razorpay.com/docs/api/settlements/entity` and `razorpay.com/docs/api/settlements/fetch-recon`. The settlement-entity field list was correct (missing only the low-value constant `entity` and `created_at`, which is now added). The recon-line field list, however, omitted several fields that bear directly on committed FR-04 families and the FR-05 stretch family:

- `type` was documented as if the value set were limited to payment/refund; the real value set is `payment | refund | transfer | adjustment`. The `adjustment` value is the primary evidence record for family 5.
- `dispute_id` was missing entirely — the deterministic signal needed to detect chargeback-linked lines for FR-05 (stretch).
- `payment_id` was missing — needed to link a `refund`/`transfer`/`adjustment` row back to its parent payment for multi-record case assembly.
- `posted_at` and `credit_type` were missing — `posted_at` is a plausible signal for ledger-posting status; `credit_type` is retained with its only observed value (`"default"`) per the assumption logged in §3.1.

**Change.** FR-01a's field list is superseded by the `recon_line` schema in §3.1, which is the authoritative field list going forward. No change to the *intent* of FR-01a (synthetic data shaped to the real payload) — only to the specific fields tracked, correcting an unverified list against primary source.

---

*REV-05 through REV-15 all arise from a single verification pass conducted at the start of v0.4, re-checking every locked section against primary sources and against internal consistency. Fifteen defects were found; the eleven requiring changes are logged below.*

### REV-05 — family 4 narrowed, with an explicit precondition *(v0.4, Aug 27, 2026 — revises locked §2.4 / FR-04)*

**Issue.** FR-04 described family 4 as "settlement timing / funds-in-transit — clearing entries at a period boundary." §3.2 had already narrowed it to a wrong-account posting error, but FR-04's text was never amended and no revision was logged. Worse, the narrowed definition was still incomplete: it did not exclude the case where the settlement's bank credit has already landed. In that situation the merchant's entry sits at the correct accounts on the wrong date, and applying the family-4 correction understates bank against the statement — an auto-close that creates a break.

**Change.** FR-04's family 4 is retitled "premature bank-account posting (funds-in-transit misposting)." A hard precondition is added in §3.2: no matching bank credit exists as of the batch snapshot. An amount rule is added: the reclassified amount is the ledger's premature bank debit, and family-4 cases are generated fee-clean so the family never overlaps families 1 or 3.

### REV-06 — family 5 narrowed *(v0.4, Aug 27, 2026 — revises locked §2.4 / FR-04)*

**Issue.** FR-04 described family 5 as "on-hold release or settlement adjustment deducted in the settlement but unposted." §3.2 narrowed it to adjustment lines only, excluding bare on-hold status changes, without amending FR-04 or logging a revision.

**Change.** FR-04's family 5 is retitled "settlement adjustment unposted," scoped to `type = "adjustment"` recon lines carrying a real debit or credit. Bare on-hold is out of scope, and §3.3 records why it is also absent from the dataset.

### REV-07 — `ledger_entry` field list completed *(v0.4, Aug 27, 2026 — revises locked §1.5)*

**Issue.** §3.1 added `resolution_id` and `case_id` to the canonical journal schema locked in §1.5, describing the addition as "a completion of that schema, not a revision to it." §1.5 presents a closed field list, and Section 0 requires revisions to locked sections to be logged.

**Change.** No change to the fields themselves — they are required to make invariant 1.7.4 implementable. Logged so the field list has one authoritative location: §3.1.

### REV-08 — phantom template range struck *(v0.4, Aug 27, 2026 — revises locked §2.4)*

**Issue.** §2.4 read "approximately 6–8 templates once paired variants are counted, consistent with the 8–12 range sketched for Section 3." 6–8 and 8–12 overlap only at 8, and no 8–12 range appeared anywhere in the document.

**Change.** The trailing clause is struck. The 6–8 estimate stands and is met: §3.4 fixes the count at six.

### REV-09 — metric names and denominators corrected *(v0.4, Aug 27, 2026 — revises locked §1.6)*

**Issue.** Three problems. `auto_close_rate` was defined as recall but named "rate," repeating exactly the name-versus-formula mismatch REV-01 corrected for `match_rate`. The two primary safety metrics used different denominator conventions with no statement of that fact, so they read as comparable when they are not. And `unresolved_rate` excluded `EXTERNAL_ACTION_REQUIRED`, while §1.3 explicitly describes those cases as not resolved.

**Change.** `auto_close_rate` renamed `auto_close_recall`. `auto_match_precision` added. A denominator convention is stated for `*_rate`, `*_recall` and `*_precision`. `unresolved_rate` is replaced by `deferred_to_human_rate` and `open_case_rate`. A note records that `declined_by_confidence_rate` has no ground-truth population by construction.

### REV-10 — ground-truth schema extended and reconciled with FR-05 *(v0.4, Aug 27, 2026 — revises locked §1.6)*

**Issue.** §1.6's ground-truth schema stated that `expected_journal_entries` is empty for non-`AUTO_CLOSED` states and `should_auto_apply` is true only for `AUTO_CLOSED`. FR-05 specifies the opposite: a chargeback recognition entry posted on a case that terminates in `EXTERNAL_ACTION_REQUIRED`. Locked §1.6 and locked §2.4 contradicted each other. Separately, the taxonomy and template decisions in §3.3 and §3.4 require label fields the schema did not carry.

**Change.** FR-05's design is retained and §1.6 is revised: entries are permitted on `EXTERNAL_ACTION_REQUIRED` cases, and `should_auto_apply` covers `AUTO_CLOSED` plus FR-05 recognition. `auto_close_precision`'s denominator becomes all auto-applied entries rather than all auto-closed cases. `ground_truth_exception_type` is split into `ground_truth_exception_class` and `ground_truth_exception_subtype`; `expected_template_ids` and `expected_decline_reason` are added.

### REV-11 — date-only reclassification added to policy exclusions *(v0.4, Aug 27, 2026 — revises locked §2.5 / FR-06)*

**Issue.** The family-4 precondition added by REV-05 creates a case population — correct accounts, wrong date, credit already landed — with no defined terminal state.

**Change.** "Date-only reclassification across a period boundary" is added to the §2.5 exclusion list. These cases terminate in `REVIEW_REQUIRED` with `decline_reason = policy` and count under `declined_by_policy_rate`. The exclusion belongs there on the merits: a period-boundary date correction shades directly into revenue-recognition timing, which §2.5 already excludes.

### REV-12 — build budget corrected to 42 hours *(v0.4, Aug 27, 2026 — revises locked §2.0)*

**Issue.** Three different budgets coexisted: the header said 7 days to Sept 3, §2.0 said "8 calendar days ≈ 48 hours" by counting Aug 27 as a build day, and §6 planned Aug 28 → Sept 3, which is 7 days and 42 hours. Aug 27 is spent on specification.

**Change.** §2.0 restated as 42 hours across Aug 28 → Sept 3. Every scope decision in Section 2 was sized against 48, so §2.0 now records that the cut order is nearer than it reads and that family 5 goes first. The fallback batch size in the cut order is raised from 80 to 100 cases, consistent with the rescale in REV-13.

### REV-13 — reference batch rescaled and record counts corrected *(v0.4, Aug 27, 2026 — revises locked §2.2 / FR-01, FR-02)*

**Issue.** At 120 cases each FR-04 family held 8 cases, so a single error moved per-family accuracy by 12.5 points, against a Section 5 requirement for a per-close-pattern breakdown. Separately, §2.2's record counts were independently wrong: the row label "payment + refund recon rows" omitted `adjustment` rows, which are family 5's only evidence, and the ledger-entry and total-record figures did not follow from the payments-per-settlement distribution.

**Change.** Reference batch rescaled to 150 cases (125 settlement-anchored + 25 orphan), 10 per FR-04 family. Record counts re-derived: ~1,500 recon rows, 125 settlements, ~175 bank lines, ~3,540 ledger entries, ~5,340 raw records. FR-02's case count is deliberately held at ~360 because `throughput` is measured in cases/second and is insensitive to batch size; only its record count is restated to ~12,800.

### REV-14 — `recon_line` field semantics corrected *(v0.4, Aug 27, 2026 — revises locked §3.1 and REV-04)*

**Issue.** Three fields were carrying meanings the primary source does not support. REV-04 justified retaining `posted_at` as "a plausible signal for ledger-posting status"; checked against `razorpay.com/docs/api/settlements/fetch-recon`, the field does not appear in the documented Response Parameters list and is null in all four sample rows. It is also a Razorpay-side field, so reading it as merchant-ledger posting status would leak ground truth into the model's input. §3.1's `payment_id` comment claimed it links adjustments to a parent payment; the docs state it is null for payments, and the sample adjustment row carries null. `settlement_id` was non-nullable while `settled` existed as a field, making one of the two dead.

**Change.** `posted_at` is emitted as constant null and is barred from use as evidence anywhere in the pipeline. `payment_id`'s comment is corrected to refunds and transfers only, with a note that family 5 must assemble on `settlement_id` alone. `settlement_id` becomes nullable. `on_hold` is marked shape-only with no v1 consumer, since the docs scope it to transfers and transfers are excluded from the generator.

### REV-15 — chart-of-accounts table and family-3 example corrected *(v0.4, Aug 27, 2026 — revises locked §3.2)*

**Issue.** The `Used by` column listed `Sales Revenue` as used by "Family 2 (contra), 3, 4." Family 2 posts to `Sales Returns and Allowances` and never touches `Sales Revenue`, and family 4's own text states it uses only `Razorpay Clearing` and `Bank Account`. The column is the audit for "every account is used by at least one committed family," so a wrong entry there undermines the only check on chart-of-accounts minimality. Separately, family 3's worked example used a ₹29 fee on a ₹1000 sale — a 2.9% rate taken from the Razorpay docs sample, which is a USD/AMEX line, in a spec that is INR-only.

**Change.** `Sales Revenue`'s `Used by` corrected to Family 3. Account codes assigned. Family 3's example re-cut at Razorpay's standard Indian domestic rate: 2% MDR plus 18% GST on the fee. The family-4 precondition and amount rule from REV-05 are written into §3.2. The minimality claim still holds after correction — all seven accounts remain in use.

---

*REV-16 through REV-19 arise from reading v0.4 in full at the start of Step 4. Four defects; all four required changes. REV-20 arises from Section 5 itself, closing a gap §2.11 had already named.*

### REV-16 — `T-01` and `T-03` evidence predicates disambiguated *(v0.5, Aug 27, 2026 — revises locked §3.4)*

**Issue.** `T-01`'s evidence predicate read "a settled `payment` recon line with `fee > 0`, and no `Payment Gateway Charges` ledger entry referencing that `entity_id`." Every family-3 case satisfies that predicate: the merchant booked the net bank credit as revenue, so fee and GST were never recognised anywhere and no `Payment Gateway Charges` entry exists. `T-03` adds a conjunct, but `T-01` excluded nothing, so both predicates fired on all 10 family-3 cases. Template selection is deterministic by design, so a double fire has no defined resolution — and if `T-01` won, the entry would credit `Razorpay Clearing` where the correct credit leg is `Sales Revenue`. That instantiation *balances*, so invariant 1.7.5's debit-equals-credit check passes and only the post-adjustment residual catches it. §3.2's own standard rules this out: a family that relies on failing validation for a share of its instances is not correctly scoped.

**Change.** `T-01`'s predicate gains a positive conjunct: a `Sales Revenue` credit referencing the payment whose amount equals **gross** `amount`. `T-01` and `T-03` are now distinguished solely by whether the booked revenue is gross or net, which makes them mutually exclusive per record. A mutual-exclusivity note is added to §3.4 requiring the pipeline to assert at instantiation time that at most one template predicate fires per `(case_id, entity_id)`, treating a double fire as a hard error rather than a precedence question.

### REV-17 — §2.2 bank-line decomposition corrected *(v0.5, Aug 27, 2026 — revises locked §2.2 / FR-01)*

**Issue.** FR-01's table described ~175 bank statement lines as "125 settlement credits + ~50 non-settlement noise." Twenty-seven settlement-anchored cases require by construction that no matching bank credit exists at the batch snapshot: the 10 family-4 cases, whose hard precondition (REV-05, REV-15) is exactly that; the 12 family-4 no-ops, still inside the T+2 window; and the 5 `BANK_CREDIT_OVERDUE` cases. Generating a credit for any of them would destroy the population. The decomposition is the generator's contract, so a wrong one is a build defect, not a documentation defect. The table also had no line for the ~28 bank lines the §3.6 orphan populations require.

**Change.** The decomposition is restated as ~98 settlement credits, ~28 orphan-case lines, and ~50 non-settlement noise. The ~175 total is unchanged. A note recording which populations must have no credit is added to §2.2.

### REV-18 — §3.6 granularity exception corrected *(v0.5, Aug 27, 2026 — revises locked §3.6)*

**Issue.** §3.6 read "one case per bank line, except that a reversal and its original credit form a single case." The `REVERSAL_UNMATCHED` population is defined as a bank reversal with **no** matching prior credit in the batch, so the stated exception covered an empty set. Meanwhile the exception that is actually required went unstated: `DUPLICATE_CREDIT` is 3 cases spanning 6 bank lines, which the one-case-per-line rule would have split into 6, contradicting the §3.6 population table and the 150-case total.

**Change.** The exception is restated: a duplicate credit and the original credit carrying the same UTR form a single case, so the three `DUPLICATE_CREDIT` cases span six bank lines. No change to any case count.

### REV-19 — `MISPOSTING` definition widened to cover period *(v0.5, Aug 27, 2026 — revises locked §3.3)*

**Issue.** §3.3 defined `MISPOSTING` as "an event was posted to the wrong account or the wrong amount," and the population map labels the family-4 date-error variant `ACCOUNTING_CORRECTION` / `MISPOSTING`. REV-15's entire basis for that variant is that its accounts and amount are both correct and only the date is wrong. The label contradicted the definition, and both feed ground-truth labels and therefore grading.

**Change.** `MISPOSTING` is defined as an event posted to the wrong account, the wrong amount, **or the wrong period**. The subtype count under `ACCOUNTING_CORRECTION` stays at two, and no case's label changes.

### REV-20 — `exception_classification_accuracy` renamed and its counterpart added *(v0.5, Aug 27, 2026 — revises locked §1.6)*

**Issue.** Two problems in one metric. First, the name violated the convention REV-09 itself established: a metric whose denominator is the population the system *predicted* is a `*_precision`, and `exception_classification_accuracy` used exactly that denominator while being named an accuracy. Second, §2.11 had already flagged that it is blind to cases that should have landed in `EXTERNAL_ACTION_REQUIRED` and did not, and asked for a recall-side counterpart. Both are resolved in the same change rather than separately.

**Change.** `exception_classification_accuracy` is renamed `exception_subtype_precision`. `exception_subtype_recall` is added. Both are computed per subtype with denominators visible and reported alongside a macro average across the seven subtypes, per §5.2. §2.11's deferred gap is closed.

### REV-21 — session decomposition added as §6.3 *(v0.6, Aug 27, 2026 — revises locked §6)*

**Issue.** Section 6 planned the build in seven six-hour days and treated each day as one unit of work. Implementation runs in agent sessions, not days, and the two are not the same unit: a session has a bounded context, no memory of the session before it, and degrades if run long. Phase 4 as written spans predicates, template instantiation, the validator chain, ledger apply and re-reconciliation in one block — roughly four sessions handed over as one prompt, at the exact point in the build where invariant discipline matters most. Section 6 also gave no rule for where a session may stop, and no basis for choosing a model per session.

**Change.** §6.3 is added: a nineteen-session decomposition with a per-session artifact, checkpoint and model assignment; a session boundary rule (one artifact, one passing checkpoint, one commit); an explicit statement that sessions are stateless and are handed over through `AGENT.md` plus the previous entry's **Next** field; and a model-selection criterion based on how silent a failure mode is rather than how difficult the work looks. §6.2 is amended to make the **Next** field mandatory and specific, and to record the handoff as its third job. The day-level phases and their checkpoints are unchanged — §6.3 subdivides them, it does not replace them. A note records that the plan holds no slack for sessions that need redoing.

### REV-22 — version control protocol added as §6.4 *(v0.6, Aug 27, 2026 — revises locked §6)*

**Issue.** FR-12 fixes what the repository must contain, FR-13 fixes what the pinned run records, and NFR-01 and NFR-06 fix what a clean clone must reproduce. None of them says how often to commit, how to recover from a failed phase, where the Fireworks API key lives, or which generated artifacts belong in the repository and which do not. Three of those four change what Session 1.1 builds — `.gitignore` and repository initialisation happen on Day 1, and the key that must never be committed does not exist until Day 5, by which point `.gitignore` has been untouched for four days.

**Change.** §6.4 is added: commit-per-checkpoint cadence tied to §6.3's session boundary, `phase-1` through `phase-7` tags as the recovery points the §2.0 cut order already assumes, a commit-message convention, a stage-and-propose rule for implementation sessions, the committed/gitignored/never-committed split, and a decision to make the repository public from Session 1.1.

**Correction recorded.** In discussion preceding this revision, the generated reference dataset was proposed for `.gitignore` on the ground that NFR-01 makes it regenerable. That contradicts FR-12, which requires the seeded reference dataset checked in. FR-12 is correct on the merits: §2.7's rationale is a reviewer who never runs the code, and such a reviewer cannot regenerate anything. Regenerability is an argument for excluding the SQLite ledger, not the dataset. §6.4 follows FR-12; no change to FR-12.

### REV-23 — ledger-entry count corrected to match §3.2's 4-leg posting *(v0.7, Aug 27, 2026 — revises locked §2.2 / FR-01)*

**Issue.** REV-13 re-derived FR-01's ledger-entry estimate as ~3,540, stated as following from "the payments-per-settlement distribution." But §3.2's family-3 worked example fixes the correct clean posting at **four** ledger legs per payment — `Dr Razorpay Clearing, Dr Payment Gateway Charges, Dr GST on Gateway Charges / Cr Sales Revenue` — and that is the exact posting session 1.3's generator implements (verified balanced by session 1.3's and 2.1's tests) and session 2.2 extends unchanged to every remaining settlement-anchored population. At §3.5's stated ~11 payments per settlement across 125 settlements (≈1,375–1,450 payments), four legs per payment yields roughly 5,500–5,800 ledger entries, not ~3,540 — REV-13's re-derivation implicitly assumed under half that many legs per payment, which is not reconcilable with §3.2's locked posting. Session 2.2's actual generator run measured this directly: 1,449 payment-type recon lines produced 5,774 ledger entries, 3.98 legs/payment, confirming the four-leg rule and disconfirming REV-13's total. This was caught only now because no session before 2.2 generated the full 125-settlement population at once; sessions 1.3 and 2.1 each ran a subset (18 and 50 settlements respectively) and no checkpoint before 2.2 asserted the raw ledger-entry total.

**Change.** §2.2's `Ledger entries` estimate is corrected from ~3,540 to **~5,800**, and `Raw records, total` from ~5,340 to **~7,600** (1,500 + 125 + 175 + 5,800, restated the same way REV-13 summed it). No other row changes. No case count, template, chart-of-accounts, or invariant is affected — this is a record-count estimate only, and no session checkpoint through 2.2 asserted the prior figure.