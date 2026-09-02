"""The FR-11 report, per spec.md §1.8, §4.5 and session 6.3 (§6.3).

> **FR-11.** The report is **one self-contained static HTML file** (no
> server, no build step, no external asset fetch) containing all five
> artifacts from 1.8: metrics table, filterable case log, per-case
> evidence and audit-trail drill-down, categorized exception list, and
> reconciled-ledger diff.

> **Report:** Jinja2 rendering one HTML file with inlined CSS and a JSON
> blob in a `<script>` tag, filtered by vanilla JS. No CDN, no build step,
> no external fetch (§4.5).

Component 9's second half. `pipeline/metrics.py` computes the §1.6 surface
and `pipeline/eval_report.py` (session 6.2) turns it into §5.2's matrices,
the per-subtype breakdown and the §5.5 threshold review — this module
renders that, plus the other four §1.8 artifacts, into the one file FR-11
asks for. It recomputes nothing: every number here comes off an
already-built `EvalReport` or an already-finished `RunResult`.

**Rendering is server-side; JavaScript only filters.** The case log, the
exception report, the audit trail and the ledger diff are full Jinja loops
over `CaseRecord`/`LedgerEntry` objects, not client-side templates built
from the embedded JSON. Two reasons. First, "filtered by vanilla JS" is a
requirement about *filtering*, not about *rendering* — a table the browser
already has can be filtered by toggling `hidden` on its rows, which needs
far less script than re-building the table from JSON. Second, server-side
rendering is what `pytest` can assert against directly: the checkpoint
("opens from `file://` with networking off") needs the content to be
right with no browser in the loop, and a table built by client JS is
invisible to a test that only reads the file. The JSON blob is still
embedded, in full, in its own `<script type="application/json">` tag —
FR-11 names it as part of the tech stack, and it is what makes the run's
own data programmatically inspectable by a reader who opens the console
rather than reading the rendered tables.

**Per-case drill-down uses `<details>`/`<summary>`, not JavaScript.** A
native disclosure widget needs no script to open or close, cannot silently
fail if a browser blocks scripts, and is exactly the shape "drill-down"
asks for — collapsed by default, the full evidence one click away.

**Model-generated text is never trusted with `innerHTML`.** Slot B's
narrations are model output; Jinja's autoescaping (the default here) is
what stands between that text and the surrounding markup, so a narration
that happened to contain `</td>` cannot break the table it sits in. The
two disclosure constants below are the one exception — fixed strings this
module owns, rendered with `| safe` so the apostrophe in "merchant's" (§5.3)
does not turn into `&#39;` in a report a judge is meant to read as prose.

**No `datetime.now()` anywhere in this module**, per `AGENT.md`'s
determinism rule — there is no "generated at" timestamp. The report is a
pure function of the run's own data (`snapshot_date`, a parameter,
appears; the wall clock never does), so building it twice from the same
run produces the same file.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from jinja2 import Environment
from markupsafe import Markup

from pipeline.apply import CaseOutcome
from pipeline.case_assembly import Case, CaseKind
from pipeline.eval_report import EvalReport, EXCEPTION_CLASS_LABELS, STATE_LABELS, _ratio, named_rates
from pipeline.ground_truth import DeclineReason, ExceptionClass, ExceptionSubtype, OutcomeState
from pipeline.instantiator import CandidateJournalEntry
from pipeline.narration import CaseNarration, NarrationKind
from pipeline.policy import PolicyDecision
from pipeline.schemas import LedgerEntry, LedgerSource
from pipeline.subtype_label import SubtypeLabel
from pipeline.validator import ValidationReport

from pydantic import BaseModel, ConfigDict

__all__ = [
    "SYNTHETIC_EVAL_DISCLOSURE",
    "ANOMALY_ENRICHMENT_DISCLOSURE",
    "CaseRecord",
    "ReportContext",
    "build_case_record",
    "build_case_records",
    "ledger_diff",
    "build_report_context",
    "case_reasoning",
    "format_paise",
    "render_report_html",
]

# --- The two disclosures FR-11's report header MUST carry (§5.3, §3.5). ---

SYNTHETIC_EVAL_DISCLOSURE = (
    "Ground-truth labels and the records being graded come from one generator. "
    "The evaluation measures whether the pipeline recovers the injected intent; "
    "it does not establish that the injected intent resembles a real merchant's books."
)
"""§5.3, verbatim in substance: "required in the README, the FR-11 report header, and
the pitch video, alongside the anomaly-enrichment disclosure from §3.5." Markdown
emphasis (`` ` ``, `**`) is dropped — this is HTML, not the spec document, and the
constant is rendered with `| safe` specifically so its apostrophe survives intact."""

ANOMALY_ENRICHMENT_DISCLOSURE = (
    "match_rate on this batch is not comparable to any industry figure. The batch is "
    "deliberately anomaly-enriched for metric legibility. The enrichment factor MUST be "
    "stated in the README, in the report header of the FR-11 HTML artifact, and in the "
    "pitch video, alongside the observation that EXTERNAL_ACTION_REQUIRED runs high "
    "(roughly 21%) because orphan cases are unresolvable by construction."
)
"""§3.5's "Anomaly enrichment — disclosure requirement" blockquote, verbatim in
substance (markdown stripped, per `SYNTHETIC_EVAL_DISCLOSURE`'s note)."""


# --- Money, rendered without ever dividing with a float. ---


def format_paise(amount: int) -> str:
    """Integer paise as a signed rupee string — `divmod`, never `/100.0`.

    NFR-04 is about the computation path, not a report's display strings,
    but there is no reason to reach for a float here either when integer
    `divmod` gives the exact rupees-and-paise split directly.
    """
    sign = "-" if amount < 0 else ""
    rupees, paise = divmod(abs(int(amount)), 100)
    return f"{sign}₹{rupees:,}.{paise:02d}"


# --- Per-case record: artifacts 2, 3 and 5 are all views of this. ---


def _linked_records(case: Case) -> tuple[str, ...]:
    """Every source-record ID this case's evidence touches — recon lines then bank
    lines, in the order `Case` already carries them."""
    return tuple(line.entity_id for line in case.recon_lines) + tuple(
        line.line_id for line in case.bank_lines
    )


class CaseRecord(BaseModel):
    """One case, joined from `pipeline.case_assembly.Case` and `pipeline.apply.CaseOutcome`
    (plus, where one exists, its `pipeline.narration.CaseNarration`) into the single shape
    every report table reads from. Built once per case so the case log, the exception
    report and the audit trail can never disagree about what one case's outcome was.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str
    kind: CaseKind
    state: OutcomeState
    decline_reason: DeclineReason | None
    exception_class: ExceptionClass | None
    classified_subtype: SubtypeLabel | None
    triggered_subtypes: tuple[ExceptionSubtype, ...]
    match_tier: int | None
    in_settlement_window: bool | None
    residual_paise: int
    linked_records: tuple[str, ...]
    applied_entries: tuple[CandidateJournalEntry, ...]
    replayed_entries: tuple[CandidateJournalEntry, ...]
    proposed_entries: tuple[CandidateJournalEntry, ...]
    policy_decisions: tuple[PolicyDecision, ...]
    validations: tuple[ValidationReport, ...]
    narration_kind: NarrationKind | None
    narration_text: str | None
    narration_model_generated: bool | None
    """FR-11's labelling obligation, carried as data (§4.2): `True` whenever
    `narration_text` is not `None` — every Slot B string is model-generated by
    construction (`pipeline.narration.CaseNarration`) — and `None` when there is
    no narration to label at all."""

    @property
    def is_exception(self) -> bool:
        """Artifact 3's population: every non-`AUTO_MATCHED`, non-`AUTO_CLOSED` case —
        the three states with something for a human to read and act on."""
        return self.state not in (OutcomeState.AUTO_MATCHED, OutcomeState.AUTO_CLOSED)

    @property
    def entries_to_audit(self) -> tuple[CandidateJournalEntry, ...]:
        """Artifact 5's population, for one `AUTO_CLOSED` case: what this run actually
        posted, plus what an earlier run posted and this one recognised as a replay
        (§1.7.4) — both are the audit trail for why the case is closed."""
        return self.applied_entries + self.replayed_entries


def build_case_record(
    case: Case, outcome: CaseOutcome, narration: CaseNarration | None = None
) -> CaseRecord:
    """One case's `CaseRecord`, from a finished `pipeline.run.RunResult`'s own
    `cases`/`outcome.outcomes`, plus its `CaseNarration` when Slot B produced one."""
    return CaseRecord(
        case_id=case.case_id,
        kind=case.kind,
        state=outcome.state,
        decline_reason=outcome.decline_reason,
        exception_class=outcome.exception_class,
        classified_subtype=outcome.classified_subtype,
        triggered_subtypes=outcome.triggered_subtypes,
        match_tier=case.match_tier,
        in_settlement_window=case.in_settlement_window,
        residual_paise=outcome.residual_paise,
        linked_records=_linked_records(case),
        applied_entries=outcome.applied_entries,
        replayed_entries=outcome.replayed_entries,
        proposed_entries=outcome.proposed_entries,
        policy_decisions=outcome.policy_decisions,
        validations=outcome.validations,
        narration_kind=narration.kind if narration is not None else None,
        narration_text=narration.text if narration is not None else None,
        narration_model_generated=narration.model_generated if narration is not None else None,
    )


def build_case_records(
    cases: Sequence[Case],
    outcomes: Sequence[CaseOutcome],
    narrations: Sequence[CaseNarration] = (),
) -> list[CaseRecord]:
    """Every case in a run, in `cases`' own order, joined to its outcome and (where
    one exists) its Slot B narration — `pipeline.narration.build_narration_bundles`'
    own restriction to `EXTERNAL_ACTION_REQUIRED`/`ABSTAINED` means most cases have
    none, which is exactly what `build_case_record`'s default handles."""
    outcome_by_case = {outcome.case_id: outcome for outcome in outcomes}
    narration_by_case = {narration.case_id: narration for narration in narrations}
    records = []
    for case in cases:
        outcome = outcome_by_case.get(case.case_id)
        if outcome is None:
            continue
        records.append(build_case_record(case, outcome, narration_by_case.get(case.case_id)))
    return records


def ledger_diff(ledger_entries: Sequence[LedgerEntry]) -> tuple[LedgerEntry, ...]:
    """Artifact 1, as FR-11 names it — a **diff**, not the whole ~5,800-row ledger.

    `pipeline.apply.seed_ledger` loads the merchant's own bookkeeping unchanged;
    every row this run adds is a `CONTROLLER_ADJUSTMENT` leg, and nothing else in
    the ledger moves. Filtering to that source is therefore exactly the
    before/after delta a reviewer wants to see, with no separate "before" copy of
    ~5,800 rows needed to compute it.
    """
    return tuple(entry for entry in ledger_entries if entry.source is LedgerSource.CONTROLLER_ADJUSTMENT)


def case_reasoning(record: CaseRecord) -> str:
    """The "per-case reasoning" artifact 3 asks for, for one case.

    Model-generated prose first, when Slot B produced one — it is already exactly
    this. Otherwise the reasoning is assembled from facts the deterministic path
    already computed (never invented here): which policy exclusion fired and why,
    which safety validation a declined candidate failed, or which §3.3 trigger is
    still open. A case with none of those (an `ABSTAINED` case Slot B never saw)
    falls back to the one true statement the pipeline can make about it.
    """
    if record.narration_text:
        return record.narration_text
    parts: list[str] = []
    if record.decline_reason is DeclineReason.POLICY:
        parts += [f"{decision.exclusion.value}: {decision.detail}" for decision in record.policy_decisions]
    elif record.decline_reason is DeclineReason.CONFIDENCE:
        for report in record.validations:
            parts += [
                f"{check.check.value} failed: {check.detail}"
                for check in report.results
                if not check.passed
            ]
    if record.triggered_subtypes:
        parts.append("triggered: " + ", ".join(subtype.value for subtype in record.triggered_subtypes))
    if not parts:
        parts.append("no defensible candidate: a required piece of evidence is absent or ambiguous")
    return "; ".join(parts)


# --- The whole report's context. ---


class ReportContext(BaseModel):
    """Everything one `<report>...</report>` needs — one batch run's full §1.8
    surface, ready for `render_report_html`."""

    model_config = ConfigDict(frozen=True)

    arm: str
    seed: int | None
    snapshot_date: str
    eval_report: EvalReport
    cases: tuple[CaseRecord, ...]
    ledger_diff: tuple[LedgerEntry, ...]


def build_report_context(
    cases: Sequence[Case],
    outcomes: Sequence[CaseOutcome],
    ledger_entries: Sequence[LedgerEntry],
    eval_report: EvalReport,
    *,
    arm: str,
    snapshot_date: str,
    seed: int | None = None,
    narrations: Sequence[CaseNarration] = (),
) -> ReportContext:
    """Assemble one run's `ReportContext`. `eval_report` is `pipeline.eval_report.build_eval_report`'s
    own output — this module never recomputes a metric, per its docstring's first refusal."""
    return ReportContext(
        arm=arm,
        seed=seed,
        snapshot_date=snapshot_date,
        eval_report=eval_report,
        cases=tuple(build_case_records(cases, outcomes, narrations)),
        ledger_diff=ledger_diff(ledger_entries),
    )


# --- Rendering. ---

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Settlement Close Controller — report ({{ context.arm }}{% if context.seed is not none %}, seed {{ context.seed }}{% endif %})</title>
<style>
:root {
  color-scheme: light;
  --bg: #f7f8fa; --panel: #ffffff; --ink: #1b1f24; --muted: #5b6472;
  --border: #e1e4e8; --accent: #2b5fd9;
  --auto-matched: #1e8e5a; --auto-closed: #2b5fd9; --review: #b8720a;
  --external: #8b3fc9; --abstained: #6b7280; --bad: #c0392b;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 0 0 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
header.report-header { background: var(--panel); border-bottom: 1px solid var(--border);
  padding: 1.5rem 2rem; }
h1 { margin: 0 0 0.25rem; font-size: 1.4rem; }
.run-meta { color: var(--muted); font-size: 0.9rem; }
.disclosure { background: #fff8e6; border: 1px solid #e8c766; border-radius: 6px;
  padding: 0.75rem 1rem; margin: 0.75rem 0 0; font-size: 0.9rem; }
main { max-width: 1200px; margin: 0 auto; padding: 0 2rem; }
section { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 1.25rem 1.5rem; margin-top: 1.5rem; }
section > h2 { margin-top: 0; font-size: 1.1rem; }
section > p.section-note { color: var(--muted); font-size: 0.88rem; }
.table-scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 0.87rem; }
th, td { border-bottom: 1px solid var(--border); padding: 0.4rem 0.6rem; text-align: left;
  white-space: nowrap; }
th { background: #f0f2f5; position: sticky; top: 0; }
td.wrap, th.wrap { white-space: normal; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.78rem;
  color: #fff; }
.badge.AUTO_MATCHED { background: var(--auto-matched); }
.badge.AUTO_CLOSED { background: var(--auto-closed); }
.badge.REVIEW_REQUIRED { background: var(--review); }
.badge.EXTERNAL_ACTION_REQUIRED { background: var(--external); }
.badge.ABSTAINED { background: var(--abstained); }
.badge.pass { background: var(--auto-matched); }
.badge.fail { background: var(--bad); }
.ai-badge { display: inline-block; margin-left: 0.4rem; padding: 0.05rem 0.4rem;
  border-radius: 4px; background: #eef1ff; color: var(--accent); font-size: 0.72rem;
  border: 1px solid #c7d0f5; }
.controls { display: flex; gap: 0.75rem; margin-bottom: 0.75rem; flex-wrap: wrap; }
.controls input, .controls select { padding: 0.35rem 0.5rem; border: 1px solid var(--border);
  border-radius: 4px; font-size: 0.87rem; }
details { margin: 0.15rem 0; }
details > summary { cursor: pointer; color: var(--accent); font-size: 0.85rem; }
details .drill { margin-top: 0.5rem; padding: 0.6rem 0.8rem; background: #f7f8fa;
  border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; }
.legs { margin: 0.35rem 0; }
.legs table { width: auto; }
.checklist { list-style: none; padding: 0; margin: 0.25rem 0; }
.checklist li { padding: 0.1rem 0; }
tr[hidden] { display: none; }
footer.report-footer { max-width: 1200px; margin: 1.5rem auto 0; padding: 0 2rem;
  color: var(--muted); font-size: 0.8rem; }
/* The headline band: the four figures §1.6 ranks first, above the tables that
   derive them. One grouped object with hairline internal rules rather than four
   floating cards — these four belong together, and the borders should say so. */
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1px; background: var(--border); border: 1px solid var(--border);
  border-radius: 8px; overflow: hidden; margin: 0 0 0.6rem; }
.summary .tile { background: var(--panel); padding: 0.95rem 1.15rem;
  display: flex; flex-direction: column; gap: 0.2rem; }
.summary .stat { font-size: 1.7rem; font-weight: 600; line-height: 1.1;
  font-variant-numeric: tabular-nums; letter-spacing: -0.015em; }
.summary .tile-label { font-size: 0.82rem; color: var(--ink); }
.summary .tile-note { font-size: 0.75rem; color: var(--muted); }
.summary .tile.primary .stat { color: var(--accent); }
.summary .tile.good .stat { color: var(--auto-matched); }
.summary .tile.bad .stat { color: var(--bad); }
.summary .tile.neutral .stat { color: var(--abstained); }
p.summary-note { max-width: 90ch; color: var(--muted); font-size: 0.83rem;
  margin: 0 0 1.25rem; }
</style>
</head>
<body>

<header class="report-header">
  <h1>AI Settlement Close Controller — batch report</h1>
  <div class="run-meta">
    arm: <strong>{{ context.arm }}</strong>
    &middot; seed: <strong>{{ context.seed if context.seed is not none else "n/a" }}</strong>
    &middot; snapshot date: <strong>{{ context.snapshot_date }}</strong>
    &middot; cases: <strong>{{ context.eval_report.metrics.total_cases }}</strong>
  </div>
  <div class="disclosure">{{ synthetic_eval_disclosure | safe }}</div>
  <div class="disclosure">
    {{ anomaly_enrichment_disclosure | safe }}
    This run: {{ external_action_count }} of {{ total_cases }} cases
    ({{ "%.1f"|format(external_action_share * 100) }}%) are ground-truth
    <span class="badge EXTERNAL_ACTION_REQUIRED">EXTERNAL_ACTION_REQUIRED</span>;
    {{ no_action_count }} of {{ total_cases }} ({{ "%.1f"|format(no_action_share * 100) }}%)
    require no action at all
    (<span class="badge AUTO_MATCHED">AUTO_MATCHED</span>).
  </div>
</header>

<main>

{% set m = context.eval_report.metrics %}
<div class="summary">
  <div class="tile primary">
    <div class="stat">{{ auto_resolved_count }} / {{ total_cases }}</div>
    <div class="tile-label">closed automatically</div>
    <div class="tile-note">AUTO_MATCHED + AUTO_CLOSED</div>
  </div>
  <div class="tile {{ 'good' if m.false_match_rate.numerator == 0 else 'bad' }}">
    <div class="stat">{{ m.false_match_rate.numerator }} / {{ m.false_match_rate.denominator }}</div>
    <div class="tile-label">false matches</div>
    <div class="tile-note">§1.6 primary safety metric &middot; target 0</div>
  </div>
  <div class="tile {{ 'good' if m.auto_close_precision.value == 1.0 else 'primary' }}">
    <div class="stat">{{ m.auto_close_precision.numerator }} / {{ m.auto_close_precision.denominator }}</div>
    <div class="tile-label">auto-close precision</div>
    <div class="tile-note">auto-applied entries, not cases (REV-10)</div>
  </div>
  <div class="tile neutral">
    <div class="stat">{{ m.abstention_rate.numerator }} / {{ m.abstention_rate.denominator }}</div>
    <div class="tile-label">abstained</div>
    <div class="tile-note">a designed outcome (§1.3), not a failure</div>
  </div>
</div>
<p class="summary-note">Read these four before the tables that derive them. Every figure
is a numerator over its own denominator, and the denominators differ on purpose —
<code>false_match_rate</code> is over total cases while <code>auto_close_precision</code>
is over auto-applied entries, so the two are not complements of one another (§1.6).</p>

<section id="metrics">
  <h2>1. Metrics report</h2>
  <p class="section-note">Every figure below is a numerator and a denominator (§5.2) — never a bare
  float. Every §5.5 target is provisional (§5.5).</p>

  <div class="table-scroll">
  <table>
    <thead><tr><th>metric</th><th>value</th></tr></thead>
    <tbody>
    {% for label, r in headline_rates %}
      <tr><td>{{ label }}</td><td>{{ ratio(r) }}</td></tr>
    {% endfor %}
    </tbody>
  </table>
  </div>

  <h3>Outcome-state confusion matrix (rows = ground truth, columns = predicted)</h3>
  {{ render_matrix(context.eval_report.state_matrix) }}

  <h3>Exception-class confusion matrix (rows = ground truth, columns = predicted)</h3>
  {{ render_matrix(context.eval_report.exception_class_matrix) }}

  <h3>Exception subtype precision / recall (macro over seven subtypes, REV-25)</h3>
  <div class="table-scroll">
  <table>
    <thead><tr><th>subtype</th><th>precision</th><th>recall</th></tr></thead>
    <tbody>
    {% for m in context.eval_report.metrics.subtype_metrics %}
      <tr><td>{{ m.subtype.value }}</td><td>{{ ratio(m.precision) }}</td><td>{{ ratio(m.recall) }}</td></tr>
    {% endfor %}
      <tr><td><strong>macro average</strong></td>
        <td><strong>{{ ratio(context.eval_report.metrics.exception_subtype_precision_macro) }}</strong></td>
        <td><strong>{{ ratio(context.eval_report.metrics.exception_subtype_recall_macro) }}</strong></td></tr>
    </tbody>
  </table>
  </div>

  <h3>§5.5 threshold review (provisional)</h3>
  <div class="table-scroll">
  <table>
    <thead><tr><th>metric</th><th>target</th><th>measured</th><th>verdict</th><th class="wrap">detail</th></tr></thead>
    <tbody>
    {% for check in context.eval_report.threshold_review %}
      <tr>
        <td>{{ check.target.metric }}</td>
        <td>{{ check.target.target_text }}</td>
        <td>{{ ratio(check.measured) }}</td>
        <td>{{ check.verdict.value }}</td>
        <td class="wrap">{{ check.detail }}{% if check.target.note %} &mdash; {{ check.target.note }}{% endif %}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</section>

<section id="case-log">
  <h2>2. Case log</h2>
  <p class="section-note">Every reconciliation case, its terminal state, and (click "evidence") the
  linked source records and, for a closed case, the applied entry.</p>
  <div class="controls">
    <label>state
      <select id="state-filter">
        <option value="">all</option>
        {% for label in state_labels %}<option value="{{ label }}">{{ label }}</option>{% endfor %}
      </select>
    </label>
    <input id="search-box" type="search" placeholder="filter by case ID, class, subtype&hellip;">
  </div>
  <div class="table-scroll">
  <table id="case-log-table">
    <thead><tr><th>case ID</th><th>kind</th><th>state</th><th>class</th><th>subtype</th>
      <th>match tier</th><th>residual</th><th>evidence</th></tr></thead>
    <tbody id="case-log-body">
    {% for c in context.cases %}
      <tr data-state="{{ c.state.value }}" data-search="{{ c.case_id }} {{ c.exception_class.value if c.exception_class else '' }} {{ c.classified_subtype.value if c.classified_subtype else '' }}">
        <td>{{ c.case_id }}</td>
        <td>{{ c.kind.value }}</td>
        <td><span class="badge {{ c.state.value }}">{{ c.state.value }}</span></td>
        <td>{{ c.exception_class.value if c.exception_class else "—" }}</td>
        <td>{{ c.classified_subtype.value if c.classified_subtype else "—" }}</td>
        <td>{{ c.match_tier if c.match_tier is not none else "—" }}</td>
        <td>{{ paise(c.residual_paise) }}</td>
        <td>
          <details><summary>evidence</summary>
            <div class="drill">
              <strong>linked records:</strong> {{ c.linked_records | join(", ") if c.linked_records else "none" }}<br>
              {% if c.triggered_subtypes %}<strong>triggered:</strong> {{ c.triggered_subtypes | map(attribute="value") | join(", ") }}<br>{% endif %}
              {% if c.decline_reason %}<strong>decline reason:</strong> {{ c.decline_reason.value }}<br>{% endif %}
              {{ render_entries(c.entries_to_audit, "applied/replayed entries") }}
              {{ render_entries(c.proposed_entries, "proposed entries (not applied)") }}
              {{ render_validations(c.validations) }}
            </div>
          </details>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</section>

<section id="audit-trail">
  <h2>3. Audit trail — AUTO_CLOSED cases</h2>
  <p class="section-note">Invariant 1.7.3: every automatic decision cites the source records and
  deterministic calculation that justify it, and the specific safety validations passed (§1.7.5).</p>
  <div class="table-scroll">
  <table>
    <thead><tr><th>case ID</th><th>template(s)</th><th>cited records</th><th>legs (deterministic)</th><th>validations</th></tr></thead>
    <tbody>
    {% for c in context.cases if c.state.value == "AUTO_CLOSED" %}
      <tr>
        <td>{{ c.case_id }}</td>
        <td>{{ c.entries_to_audit | map(attribute="template_id") | map("string") | unique | join(", ") }}</td>
        <td class="wrap">{{ c.entries_to_audit | map(attribute="cited_record_ids") | sum(start=()) | unique | join(", ") }}</td>
        <td>
          {% for entry in c.entries_to_audit %}
            <div class="legs">
            <div class="table-scroll">
            <table><thead><tr><th>account</th><th>debit</th><th>credit</th></tr></thead><tbody>
              {% for leg in entry.legs %}
                <tr><td>{{ leg.account_name }} ({{ leg.account_code }})</td>
                  <td>{{ paise(leg.debit) if leg.debit else "" }}</td>
                  <td>{{ paise(leg.credit) if leg.credit else "" }}</td></tr>
              {% endfor %}
            </tbody></table>
            </div>
            </div>
          {% endfor %}
        </td>
        <td>
          <ul class="checklist">
          {% for report in c.validations %}
            {% for check in report.results %}
              <li><span class="badge {{ "pass" if check.passed else "fail" }}">{{ "PASS" if check.passed else "FAIL" }}</span>
                {{ check.check.value }}</li>
            {% endfor %}
          {% endfor %}
          </ul>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</section>

<section id="exceptions">
  <h2>4. Exception report</h2>
  <p class="section-note">Every non-AUTO_MATCHED, non-AUTO_CLOSED case, categorized, with a
  recommended next step or an explicit abstention rationale. Text marked
  <span class="ai-badge">model-generated</span> is Slot B prose over deterministic facts (§4.2) —
  never evidence in its own right.</p>
  <div class="table-scroll">
  <table>
    <thead><tr><th>case ID</th><th>state</th><th>class</th><th>subtype</th><th class="wrap">reasoning</th></tr></thead>
    <tbody>
    {% for c in context.cases if c.is_exception %}
      <tr>
        <td>{{ c.case_id }}</td>
        <td><span class="badge {{ c.state.value }}">{{ c.state.value }}</span></td>
        <td>{{ c.exception_class.value if c.exception_class else "—" }}</td>
        <td>{{ c.classified_subtype.value if c.classified_subtype else "—" }}</td>
        <td class="wrap">{{ reasoning(c) }}
          {% if c.narration_model_generated %}<span class="ai-badge">model-generated</span>{% endif %}
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  </div>
</section>

<section id="ledger-diff">
  <h2>5. Reconciled-ledger diff</h2>
  <p class="section-note">Every row this run added to the merchant's ledger — the merchant's own
  bookkeeping is loaded unchanged, so this is the full before/after delta.</p>
  <div class="table-scroll">
  <table>
    <thead><tr><th>journal entry ID</th><th>date</th><th>case ID</th><th>resolution ID</th>
      <th>account</th><th>debit</th><th>credit</th><th>reference</th></tr></thead>
    <tbody>
    {% for e in context.ledger_diff %}
      <tr>
        <td>{{ e.journal_entry_id }}</td>
        <td>{{ e.date.isoformat() }}</td>
        <td>{{ e.case_id }}</td>
        <td>{{ e.resolution_id }}</td>
        <td>{{ e.account_name }} ({{ e.account_code }})</td>
        <td>{{ paise(e.debit) if e.debit else "" }}</td>
        <td>{{ paise(e.credit) if e.credit else "" }}</td>
        <td>{{ e.reference }}</td>
      </tr>
    {% endfor %}
    {% if not context.ledger_diff %}
      <tr><td colspan="8">No entries were posted this run.</td></tr>
    {% endif %}
    </tbody>
  </table>
  </div>
</section>

</main>

<footer class="report-footer">
  Generated by the AI Settlement Close Controller pipeline (spec.md, FR-11). All input data is
  synthetic (FR-01a). No network dependency other than the LLM inference endpoint, and this file
  itself fetches nothing (NFR-05).
</footer>

<script type="application/json" id="report-data">{{ data_json | safe }}</script>
<script>
(function () {
  "use strict";
  var stateFilter = document.getElementById("state-filter");
  var searchBox = document.getElementById("search-box");
  var rows = document.querySelectorAll("#case-log-body tr");

  function applyFilter() {
    var state = stateFilter.value;
    var query = searchBox.value.trim().toUpperCase();
    rows.forEach(function (row) {
      var matchesState = !state || row.dataset.state === state;
      var matchesQuery = !query || row.dataset.search.toUpperCase().indexOf(query) !== -1;
      row.hidden = !(matchesState && matchesQuery);
    });
  }

  if (stateFilter && searchBox) {
    stateFilter.addEventListener("change", applyFilter);
    searchBox.addEventListener("input", applyFilter);
  }
})();
</script>

</body>
</html>
"""


def _render_matrix_html(matrix) -> str:
    """One §5.2 confusion matrix as an HTML table — the same content
    `pipeline.eval_report.render_confusion_matrix` prints as text, in markup."""
    row_totals = matrix.row_totals()
    column_totals = matrix.column_totals()
    header_cells = "".join(f"<th>{i + 1}</th>" for i in range(len(matrix.labels)))
    rows_html = []
    for i, label in enumerate(matrix.labels):
        cells = "".join(f"<td>{value if value else ''}</td>" for value in matrix.counts[i])
        rows_html.append(f"<tr><td>{i + 1}. {label}</td>{cells}<td>{row_totals[label]}</td></tr>")
    footer_cells = "".join(f"<td>{column_totals[label]}</td>" for label in matrix.labels)
    confusions = "".join(
        f"<li>{count} &times; ground truth {truth} &rarr; predicted {predicted}</li>"
        for truth, predicted, count in matrix.confusions()
    )
    return (
        '<div class="table-scroll"><table><thead><tr><th></th>'
        f"{header_cells}<th>total</th></tr></thead><tbody>"
        f"{''.join(rows_html)}"
        f'<tr><td>predicted total</td>{footer_cells}<td></td></tr>'
        "</tbody></table></div>"
        f"<p>agreement: {_ratio(matrix.accuracy)}</p>"
        + (f'<ul class="checklist">{confusions}</ul>' if confusions else "<p>no confusions.</p>")
    )


def _render_entries_html(entries: Sequence[CandidateJournalEntry], title: str) -> str:
    if not entries:
        return ""
    items = []
    for entry in entries:
        legs = "".join(
            f"<li>{leg.account_name} ({leg.account_code}): "
            f"{'Dr ' + format_paise(leg.debit) if leg.debit else 'Cr ' + format_paise(leg.credit)}</li>"
            for leg in entry.legs
        )
        cited = ", ".join(entry.cited_record_ids)
        items.append(
            f"<li><strong>{entry.template_id.value}</strong> (cites: {cited})"
            f'<ul class="checklist">{legs}</ul></li>'
        )
    return f"<strong>{title}:</strong><ul>{''.join(items)}</ul>"


def _render_validations_html(validations: Sequence[ValidationReport]) -> str:
    if not validations:
        return ""
    items = []
    for report in validations:
        checks = "".join(
            f'<li><span class="badge {"pass" if check.passed else "fail"}">'
            f'{"PASS" if check.passed else "FAIL"}</span> {check.check.value}'
            f"{': ' + check.detail if check.detail else ''}</li>"
            for check in report.results
        )
        items.append(f"<li>{report.template_id.value} / {report.resolution_id}<ul class=\"checklist\">{checks}</ul></li>")
    return f'<strong>validations:</strong><ul>{"".join(items)}</ul>'


def render_report_html(context: ReportContext) -> str:
    """The one self-contained FR-11 HTML file for `context`.

    No CDN, no build step, no external fetch: every byte of CSS and JS in
    `_TEMPLATE` is inline, and the JSON blob is `context`'s own data —
    nothing this function returns can reach outside the file it is in.
    """
    metrics = context.eval_report.metrics
    ground_truth_state_counts = metrics.ground_truth_state_counts
    total_cases = metrics.total_cases
    external_action_count = ground_truth_state_counts.get(str(OutcomeState.EXTERNAL_ACTION_REQUIRED), 0)
    no_action_count = ground_truth_state_counts.get(str(OutcomeState.AUTO_MATCHED), 0)
    # The headline band's one derived figure. Read off `predicted_state_counts`
    # rather than `total_cases - open_case_rate.numerator` so the tile shows the
    # two states it names and stays auditable against §5.2's own matrix, instead
    # of arriving as the complement of a metric defined somewhere else.
    predicted_state_counts = metrics.predicted_state_counts
    auto_resolved_count = predicted_state_counts.get(str(OutcomeState.AUTO_MATCHED), 0) + (
        predicted_state_counts.get(str(OutcomeState.AUTO_CLOSED), 0)
    )

    env = Environment(autoescape=True)
    env.globals["ratio"] = _ratio
    env.globals["paise"] = format_paise
    env.globals["reasoning"] = case_reasoning
    # Marked `Markup`, not `| safe` in the template: each of these builds raw HTML
    # from data this module fully controls (chart-of-accounts names, enum values,
    # our own generated IDs, validator-produced check text) — never a narration or
    # other model-generated string, which stays auto-escaped by not passing through
    # here. Without `Markup`, autoescape would double-encode the tags themselves.
    env.globals["render_matrix"] = lambda matrix: Markup(_render_matrix_html(matrix))
    env.globals["render_entries"] = lambda entries, title: Markup(_render_entries_html(entries, title))
    env.globals["render_validations"] = lambda validations: Markup(_render_validations_html(validations))
    template = env.from_string(_TEMPLATE)

    data_json = json.dumps(context.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    # A narration or narration-derived string could in principle contain the
    # literal three characters "</script>"; escaping the slash keeps the JSON
    # blob's own <script> tag from being closed early by data inside it.
    data_json = data_json.replace("</", "<\\/")

    return template.render(
        context=context,
        headline_rates=list(named_rates(metrics).items()),
        state_labels=STATE_LABELS,
        class_labels=EXCEPTION_CLASS_LABELS,
        synthetic_eval_disclosure=SYNTHETIC_EVAL_DISCLOSURE,
        anomaly_enrichment_disclosure=ANOMALY_ENRICHMENT_DISCLOSURE,
        external_action_count=external_action_count,
        no_action_count=no_action_count,
        auto_resolved_count=auto_resolved_count,
        total_cases=total_cases,
        external_action_share=(external_action_count / total_cases) if total_cases else 0.0,
        no_action_share=(no_action_count / total_cases) if total_cases else 0.0,
        data_json=data_json,
    )
