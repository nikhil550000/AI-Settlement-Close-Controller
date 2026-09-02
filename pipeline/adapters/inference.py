"""Agentic bank-profile inference: propose -> verify -> repair, for a bank
FR-08 has no hand-written profile for.

## Why this exists at all

§2.6 fixes the adapter's contract as *declarative*: "The three FR-08
profiles are YAML column maps, not code." That is a deliberate design
choice with an equally deliberate cost — onboarding a fourth bank means a
human reads a sample export, transcribes eight column names and a
`strftime` pattern into `profiles/<bank>.yaml`, and iterates until the
parse comes out clean. `pipeline/adapters/profiles.py` and
`pipeline/adapters/bank_adapter.py` already do everything downstream of
that transcription. The transcription itself is the only step in FR-08
that is neither arithmetic nor policy: it is reading an unfamiliar
document and guessing what its columns mean.

That is exactly the shape of task §4.2 reserves for a model — "no
residual computation decides it" — and it is the one genuinely *agentic*
surface in this project, in the strict sense: the model does not answer
once and get believed, it proposes an artifact, deterministic code runs
that artifact against reality, and the failure is handed back for repair
until a bounded attempt budget runs out. Slot A (`pipeline/classifier.py`)
and Slot B (`pipeline/narration.py`) are single-shot graded calls with no
feedback edge. This module is the loop.

## The invariant that makes the loop safe

Invariant 1.7.2 and §4.2's boundary both say the same thing in different
words: **the model never puts a value into the ledger.** Everything here
is built to keep that true even though the model is now writing a
*parser configuration*, which is a strictly more powerful thing to hand a
model than an eight-value enum:

1. **The model's entire output space is column names and a date pattern.**
   `PROPOSAL_JSON_SCHEMA` is the constrained-decoding contract (§4.3
   layer 1, the same mechanism Slot A uses), and it has no field for an
   amount, an account, a paise figure, or a `bank_profile` tag. It cannot
   emit a number that reaches a `BankLine`; it can only nominate which
   *column of the bank's own file* a number is read from.
2. **Every value in the resulting `BankLine` is read out of the file by
   `bank_adapter.parse_bank_statement`, unmodified.** This module does not
   re-implement one line of parsing — see `_candidate_profile_dir` for why
   it goes to some trouble not to. A verifier that re-implements the
   parser verifies the wrong program.
3. **A proposal is not believed; it is executed and checked.**
   `verify_proposal` runs eight deterministic checks (below), including
   double-entry balance continuity — arithmetic the model cannot see the
   answer to and cannot talk its way past. An unverified proposal never
   becomes a profile, and `InferenceResult.accepted` is set by
   `VerificationOutcome.ok`, which is set by that arithmetic, never by
   anything the model said about itself.
4. **The loop terminates.** `max_attempts` (default 3) is a hard budget,
   not a convergence heuristic: three failures produce
   `InferenceResult.accepted == False` with every attempt's proposal and
   error retained, which is a *result*, not an exception. "The model kept
   trying" is not an outcome this function can return.

So the worst case of a completely wrong model is a clean give-up, and the
best case is a YAML file that is byte-for-byte the same kind of artifact a
human would have hand-written — reviewable, diffable, and committable next
to `hdfc.yaml`. Nothing in between reaches the ledger.

## The eight checks, and why each one is there

`verify_proposal` fails on the *first* violated check and reports it in
the model's own vocabulary, because the error string is not for a log —
it is the next prompt's repair signal (`build_proposal_prompt(previous=)`).
A check that fails with "invalid profile" teaches the model nothing; a
check that fails with "row 6's value-date cell '14/08/2026' does not parse
under '%m/%d/%Y'" names the bug and its fix in one line.

1. `date_format_is_strftime` — the format is a `%`-directive pattern, not
   the human description `DD/MM/YYYY`. The single most likely model error,
   and one that would otherwise surface three checks later as a
   content-free "0 rows parsed."
2. `mapped_columns_exist` — every `*_column` names a cell of the declared
   `header`. Without this the adapter dies on a `KeyError` deep in a dict
   lookup, which is a stack trace, not a repair signal.
3. `header_row_found` — the declared header actually occurs in the file.
   `bank_adapter._find_header_row` requires an exact, order-sensitive
   match on the row's non-blank cells (§2.6: the junk-block shape is not
   knowable ahead of time, so the header row is found by content), so a
   header the model paraphrased or re-ordered fails here.
4. `adapter_parses` — `parse_bank_statement` runs to completion. Catches
   the mis-mapping class that only shows up as an exception, e.g. a
   narration column mapped to `withdrawal_column`, where
   `money.rupees_string_to_paise` refuses to turn `"NEFT CR-..."` into
   paise (NFR-04: integer paise, no float, and no silent coercion).
5. `rows_parsed` / 6. `all_rows_parsed` — the parse consumed every data
   row. `bank_adapter` ends the table at the first row whose value-date
   cell fails to parse, which is precisely how a wrong `date_format` or a
   wrong `value_date_column` presents: a *silent truncation*, not an
   error. The expected count comes from `_dense_row_count`, a structural
   count of the file that is independent of the model's mapping, so this
   check cannot be satisfied by the same mistake it is meant to catch.
7. `narrations_non_empty` — every line has free text. `narration` is the
   only evidence the `UNMATCHED_INBOUND_CREDIT` / `AMBIGUOUS_CASE` split
   reads (§4.2), so a blank narration column silently disarms Slot A
   downstream rather than failing here.
8. `direction_coherent` — every row moves money in exactly one direction.
9. `balance_continuity` — for every row after the first,
   `balance[i] - balance[i-1] == deposit[i] - withdrawal[i]`, in integer
   paise. This is the check that does the real work: it is the only one
   that catches `withdrawal_column` and `deposit_column` swapped, which is
   the single most damaging error possible here (every debit booked as a
   credit) and the one that passes every other check unharmed. It is also
   the reason this module is safe to run against a bank nobody has ever
   seen: the file's own running balance is an independent witness to
   whether the mapping is right, supplied by the bank rather than by the
   model.

## What this module deliberately does not decide

`BankLine.bank_profile` (§3.1) is a closed three-value enum, and §3.1's
schemas are not this session's to widen. The inferred profile therefore
carries a `bank_profile` tag the *caller* supplies — provenance metadata
naming which existing slot the unseen bank's lines are booked under — and
the model is never asked for it: it is absent from
`PROPOSAL_JSON_SCHEMA` entirely, on the same principle as point 1 above.
Widening the enum (a `kotak` member, or a free-string bank tag) is a §3.1
schema change with migration consequences for every committed JSONL
fixture, and it is orthogonal to whether the loop works: every check above
is a check on the column map.

Likewise, this module infers a profile; it does not install one. The
accepted YAML is returned as text for a human to review and commit into
`pipeline/adapters/profiles/`. An agent that silently added a parser
configuration to the graded path would be exactly the kind of unreviewed
model output §1.7 exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from pipeline.adapters import profiles as profiles_module
from pipeline.adapters.bank_adapter import _read_grid as read_statement_grid
from pipeline.adapters.bank_adapter import parse_bank_statement
from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache
from pipeline.llm_client import LLMClient
from pipeline.money import paise_to_rupees_string
from pipeline.schemas import BankLine, BankProfile

__all__ = [
    "PROPOSAL_JSON_SCHEMA",
    "ProposalResponseError",
    "ProfileProposal",
    "VerificationOutcome",
    "InferenceAttempt",
    "InferenceResult",
    "read_statement_grid",
    "build_proposal_prompt",
    "parse_proposal_response",
    "verify_proposal",
    "infer_bank_profile",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_SAMPLE_ROWS",
]

DEFAULT_MAX_ATTEMPTS = 3
"""The termination condition, and the whole difference between an agentic loop and
an unbounded one. Three is not a tuned number: attempt 1 is the read, attempt 2 is
the repair of a named error, attempt 3 is the repair of a *second* named error. A
model that has been told twice exactly which check failed and still fails is not
converging, and burning more Fireworks calls to discover that costs money and
proves nothing."""

DEFAULT_SAMPLE_ROWS = 16
"""Rows of the raw file shown to the model: enough to cover the junk header block
(§2.6 names no bound on its size; the fixtures run 4-6 rows), the header row, and
enough data rows that the date format is unambiguous from the day-of-month values
alone. Fixed rather than "until the header row" on purpose — locating the header
row is the model's job, and pre-locating it here would be this module quietly doing
the inference it claims to be verifying."""

_CANDIDATE_PROFILE_NAME = "_candidate"
"""Filename stem the candidate YAML is written under inside a throwaway directory.
Leading underscore so it can never collide with a `BankProfile` value."""

_ALLOWED_STRFTIME_DIRECTIVES = frozenset("dmyYbBeHIMSpj")
"""Directives a bank statement's date cell can plausibly need. Deliberately not the
full `strftime` set: `%c`/`%x`/`%X` are locale-dependent, and a locale-dependent
profile would parse differently on the grader's machine than on this one, which
NFR-01 forbids as squarely as a float in a money path does."""

_DIRECTIVE_RE = re.compile(r"%(.)")


# --- The proposal: what the model is allowed to say. ---

PROPOSAL_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "date_format": {"type": "string"},
        "header": {"type": "array", "items": {"type": "string"}},
        "value_date_column": {"type": "string"},
        "transaction_date_column": {"type": "string"},
        "narration_column": {"type": "string"},
        "ref_no_column": {"type": "string"},
        "withdrawal_column": {"type": "string"},
        "deposit_column": {"type": "string"},
        "balance_column": {"type": "string"},
    },
    "required": [
        "date_format",
        "header",
        "value_date_column",
        "transaction_date_column",
        "narration_column",
        "ref_no_column",
        "withdrawal_column",
        "deposit_column",
        "balance_column",
    ],
    "additionalProperties": False,
}
"""§4.3 layer 1 (constrained decoding), applied to a configuration instead of a label.

Two shapes are load-bearing here. **`additionalProperties: False` plus no
`bank_profile` key** is what makes "the model cannot tag the ledger" a property of
the decoder rather than a promise in prose — the field does not exist in the space
the model samples from. **The two optional columns are typed `string`, not
`["string", "null"]`**, with `""` meaning "this bank has no such column": a nullable
union is the more natural JSON Schema, but constrained-decoding backends vary in
which subset of JSON Schema they honour (§4.4's own "re-check against the live
catalog" caution applies to schema support as much as to model IDs), and an empty
string is a value every backend can emit. The `"" -> None` normalisation is done in
`parse_proposal_response`, in code, deterministically."""

_PROPOSAL_INSTRUCTIONS = (
    "You are configuring a bank-statement adapter for a settlement-reconciliation "
    "system. Below are the first rows of one real bank export, exactly as they appear "
    "in the file, as a JSON array of rows where each row is a JSON array of cell "
    "strings. The file has a junk header block above the real transaction table (bank "
    "name, account number, statement period) and a summary block below it.\n\n"
    "Propose the column map that lets the adapter read this file's transaction table. "
    "Rules:\n"
    "- `header` MUST be the cells of the one real table-header row, copied verbatim and "
    "in file order: no cell added, dropped, renamed, re-spelled, re-punctuated or "
    "re-ordered. Trailing empty cells may be omitted.\n"
    "- Every `*_column` value MUST be one of the strings in `header`, character for "
    "character.\n"
    "- `date_format` MUST be a Python strftime pattern that parses this file's date "
    "cells - for example `%d/%m/%Y`, `%d-%m-%y`, `%d-%b-%Y`. It is a pattern, never a "
    "description like `DD/MM/YYYY`. Read the day-of-month values across several rows to "
    "decide which component is the day and which is the month.\n"
    "- `withdrawal_column` is the money-out (debit) column and `deposit_column` is the "
    "money-in (credit) column. They are not interchangeable: the running balance in "
    "`balance_column` falls by a withdrawal and rises by a deposit, and that arithmetic "
    "is checked against the file.\n"
    "- `narration_column` is the free-text transaction description.\n"
    "- `value_date_column` is the date the transaction takes value; when the table has "
    "only one date column, name it here.\n"
    "- `transaction_date_column` and `ref_no_column` are optional: return the empty "
    "string \"\" when this table has no such column. Every other field is required and "
    "must name a real column.\n\n"
    "Respond with JSON matching the given schema."
)
"""Fixed across every file and every attempt, so the SHA-256 cache key
(`pipeline.llm_cache.cache_key`) is driven entirely by the file's own rows and, on a
retry, by the previous failure — never by anything that varies between two runs of
the same command. A wording change here invalidates the whole committed cache at
once, which is the intended blast radius for a deliberate prompt edit (§4.3)."""


class ProposalResponseError(RuntimeError):
    """A proposal response did not parse into a `ProfileProposal`.

    Should not happen under constrained decoding — `PROPOSAL_JSON_SCHEMA` fixes the
    shape — and exists for the same reason `classifier.SlotAResponseError` does: the
    cache path can serve a hand-edited or differently-configured entry, and that
    should fail by name rather than as an `AttributeError` three frames away.

    Note this is raised *outside* the verification loop's failure handling on
    purpose: a malformed response is a broken client or a corrupted cache, not a
    wrong answer about a bank's columns, and retrying it with a "your proposal was
    invalid" prompt would be feeding a plumbing fault back to the model as if it were
    a modelling mistake.
    """


class ProfileProposal(BaseModel):
    """One candidate column map — the exact fields `profiles.BankProfileConfig`
    carries, minus `bank_profile` (see this module's docstring)."""

    model_config = ConfigDict(frozen=True)

    date_format: str
    header: tuple[str, ...]
    value_date_column: str
    transaction_date_column: str | None
    narration_column: str
    ref_no_column: str | None
    withdrawal_column: str
    deposit_column: str
    balance_column: str

    def mapped_columns(self) -> tuple[str, ...]:
        """Every column name this proposal claims exists, optional ones included."""
        named = (
            self.value_date_column,
            self.transaction_date_column,
            self.narration_column,
            self.ref_no_column,
            self.withdrawal_column,
            self.deposit_column,
            self.balance_column,
        )
        return tuple(name for name in named if name is not None)

    def to_payload(self) -> dict:
        """The proposal as the model itself emitted it — used to quote a failed
        attempt back in the repair prompt, so the model sees its own words rather
        than a paraphrase of them."""
        return {
            "date_format": self.date_format,
            "header": list(self.header),
            "value_date_column": self.value_date_column,
            "transaction_date_column": self.transaction_date_column or "",
            "narration_column": self.narration_column,
            "ref_no_column": self.ref_no_column or "",
            "withdrawal_column": self.withdrawal_column,
            "deposit_column": self.deposit_column,
            "balance_column": self.balance_column,
        }

    def to_yaml(self, *, bank_profile: BankProfile, comment_lines: Sequence[str] = ()) -> str:
        """Render as a `profiles/*.yaml` file, in the hand-written profiles' own style.

        The output is not "YAML that happens to load" — it is the same flow-sequence
        header, the same `null` for an absent optional column, and the same leading
        comment block as `hdfc.yaml`/`icici.yaml`/`axis.yaml`, because the point of
        the exercise is a file a human reviews and commits beside those three. Values
        go through `json.dumps`, which for these ASCII strings emits valid
        double-quoted YAML scalars and, unlike hand-written quoting, cannot be broken
        by a column name containing a colon or a quote.
        """
        lines = [f"# {line}" for line in comment_lines]
        lines.append(f"bank_profile: {bank_profile.value}")
        lines.append(f"date_format: {json.dumps(self.date_format)}")
        lines.append("header: [" + ", ".join(json.dumps(cell) for cell in self.header) + "]")
        for key, value in (
            ("value_date_column", self.value_date_column),
            ("transaction_date_column", self.transaction_date_column),
            ("narration_column", self.narration_column),
            ("ref_no_column", self.ref_no_column),
            ("withdrawal_column", self.withdrawal_column),
            ("deposit_column", self.deposit_column),
            ("balance_column", self.balance_column),
        ):
            lines.append(f"{key}: {'null' if value is None else json.dumps(value)}")
        return "\n".join(lines) + "\n"


class VerificationOutcome(BaseModel):
    """What deterministic code found when it ran a proposal against the real file.

    `ok` is the only thing that decides acceptance, and nothing the model emits can
    set it. `error` is written to be read by the *model* on the next attempt, not by
    a human tailing a log — see this module's docstring on repair signals.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    checks_passed: tuple[str, ...]
    failed_check: str | None = None
    error: str | None = None
    row_count: int = 0
    expected_row_count: int = 0
    first_value_date: dt.date | None = None
    last_value_date: dt.date | None = None
    total_withdrawal_paise: int = 0
    total_deposit_paise: int = 0

    def summary(self) -> str:
        """One line, for a CLI attempt log."""
        if not self.ok:
            return f"FAILED [{self.failed_check}] {self.error}"
        return (
            f"OK {self.row_count}/{self.expected_row_count} rows, "
            f"{self.first_value_date} .. {self.last_value_date}, "
            f"withdrawals {paise_to_rupees_string(self.total_withdrawal_paise)}, "
            f"deposits {paise_to_rupees_string(self.total_deposit_paise)}, "
            f"checks: {', '.join(self.checks_passed)}"
        )


class InferenceAttempt(BaseModel):
    """One turn of the loop, kept whole so a give-up is auditable (§1.7.3).

    A failed run's value is entirely in this record: three proposals and the three
    specific reasons they were rejected is a report an engineer can act on, whereas
    "inference failed" is not.
    """

    model_config = ConfigDict(frozen=True)

    index: int
    from_cache: bool
    raw_response: str
    proposal: ProfileProposal
    verification: VerificationOutcome


class InferenceResult(BaseModel):
    """The loop's terminal state — accepted, or given up on, never "still trying"."""

    model_config = ConfigDict(frozen=True)

    accepted: bool
    attempts: tuple[InferenceAttempt, ...]
    profile_yaml: str | None = None
    proposal: ProfileProposal | None = None
    verification: VerificationOutcome | None = None
    give_up_reason: str | None = None

    @property
    def accepted_on_attempt(self) -> int | None:
        """1-based attempt number that produced the accepted profile, or `None`."""
        return len(self.attempts) if self.accepted else None


# --- The prompt. ---


def build_proposal_prompt(
    sample_rows: Sequence[Sequence[str]],
    *,
    previous: Sequence[InferenceAttempt] = (),
) -> str:
    """The exact, deterministic prompt string for one attempt — and the exact string
    `pipeline.llm_cache.cache_key` hashes.

    Attempt *n* embeds attempts 1..*n-1*'s proposals and failures verbatim, so each
    attempt in a repair sequence is a *different* prompt and therefore a different
    cache entry. That is what lets a whole multi-attempt run replay from the
    committed cache offline: the second call is not "the same prompt again, hoping
    for a different answer" (which a temperature-0, seed-0 client would answer
    identically anyway, and which would make the loop a no-op), it is a strictly
    more-informed question. The feedback edge and cacheability are the same property.

    The file's path and name are deliberately excluded: they would leak the bank's
    identity, letting the model recall a known statement layout instead of reading
    the rows in front of it, and they would fragment the cache for two copies of the
    same file.
    """
    parts = [
        _PROPOSAL_INSTRUCTIONS,
        "\n\nFile rows:\n"
        + json.dumps([list(row) for row in sample_rows], ensure_ascii=True, indent=1),
    ]
    for attempt in previous:
        parts.append(
            f"\n\nYour attempt {attempt.index} proposed:\n"
            + json.dumps(attempt.proposal.to_payload(), ensure_ascii=True, sort_keys=True, indent=1)
            + "\n\nIt was rejected by the deterministic verifier, check "
            + f"`{attempt.verification.failed_check}`:\n{attempt.verification.error}"
        )
    if previous:
        parts.append(
            "\n\nFix exactly that, keep everything the verifier did not object to, and "
            "propose the corrected column map."
        )
    return "".join(parts)


def parse_proposal_response(raw: str) -> ProfileProposal:
    """Parse one raw Fireworks (or cached) response into a `ProfileProposal`.

    The only normalisation is `"" -> None` on the two optional columns (see
    `PROPOSAL_JSON_SCHEMA`); nothing else about the model's answer is repaired,
    corrected or second-guessed here. Silently fixing a proposal would make the
    verification result a statement about this function rather than about the model,
    and §5.4's ablation arms are only meaningful if what is measured is what the
    model actually said.
    """
    try:
        payload = json.loads(raw)
        return ProfileProposal(
            date_format=payload["date_format"],
            header=tuple(payload["header"]),
            value_date_column=payload["value_date_column"],
            transaction_date_column=payload["transaction_date_column"] or None,
            narration_column=payload["narration_column"],
            ref_no_column=payload["ref_no_column"] or None,
            withdrawal_column=payload["withdrawal_column"],
            deposit_column=payload["deposit_column"],
            balance_column=payload["balance_column"],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ProposalResponseError(
            f"could not parse a bank-profile proposal from response: {raw!r}"
        ) from exc


# --- The verifier. ---


@contextmanager
def _candidate_profile_dir(yaml_text: str) -> Iterator[None]:
    """Make `profiles.load_profile(_CANDIDATE_PROFILE_NAME)` resolve to `yaml_text`,
    for the duration of one verification attempt.

    `bank_adapter.parse_bank_statement` takes a profile *name* and resolves it against
    `profiles._PROFILES_DIR`. A candidate profile has no file there and must not
    acquire one: writing speculative, model-authored YAML into a git-tracked package
    directory risks leaving a half-inferred profile behind on a crash, and a stray
    `_candidate.yaml` in `profiles/` is exactly the kind of unreviewed artifact this
    module's docstring says must never appear.

    So the lookup is redirected at a throwaway temp directory and restored in
    `finally`. Everything else is the production path, untouched: the same
    `parse_bank_statement`, the same `load_profile`, the same YAML deserialisation,
    the same `BankLine` construction with the same pydantic validation. The
    alternative — re-implementing the parse against an in-memory config — would test
    a second parser that no bank statement is ever read by, which is worse than not
    testing at all, because it would report success for a profile the real adapter
    rejects.
    """
    directory = Path(tempfile.mkdtemp(prefix="inferred_bank_profile_"))
    (directory / f"{_CANDIDATE_PROFILE_NAME}.yaml").write_text(yaml_text, encoding="utf-8")
    original = profiles_module._PROFILES_DIR
    profiles_module._PROFILES_DIR = directory
    try:
        yield
    finally:
        profiles_module._PROFILES_DIR = original
        shutil.rmtree(directory, ignore_errors=True)


def _dense_row_count(grid: Sequence[Sequence[str]], header_row: int) -> int:
    """How many transaction rows the file structurally has, decided without reference
    to the proposal.

    A data row fills most of its columns; a junk or summary row ("*** End of
    Statement ***", "Total Deposits : 30,24,871.90") fills one or two, and a
    separator row fills none. So: the run of consecutive rows below the header whose
    non-empty cell count is at least half the header's width.

    This is a *structural* count on purpose. The truncation check it feeds
    (`all_rows_parsed`) exists to catch a wrong date format or a wrong value-date
    column, and both of those are the proposal's own claims — a expected-count that
    consulted the proposal would agree with the mistake it is meant to detect. The
    honest limitation: a bank whose summary block is as densely populated as its
    transaction rows would over-count here and every proposal would be rejected as
    truncated. That fails *closed* (a clean give-up, never a wrongly accepted
    profile), which is the correct direction for §1.3's error preference, and the
    give-up report names this check by name so the cause is legible.
    """
    if not grid:
        return 0
    width = len(grid[header_row])
    threshold = max(2, (width + 1) // 2)
    count = 0
    for row in grid[header_row + 1 :]:
        if sum(1 for cell in row if cell) < threshold:
            break
        count += 1
    return count


def _date_format_error(date_format: str) -> str | None:
    if "%" not in date_format:
        return (
            f"`date_format` was {date_format!r}, which is a human description, not a Python "
            "strftime pattern. Use directives: e.g. `%d/%m/%Y` for 27/08/2026, `%d-%m-%y` "
            "for 27-08-26, `%d-%b-%Y` for 27-Aug-2026."
        )
    unknown = sorted(
        {d for d in _DIRECTIVE_RE.findall(date_format) if d not in _ALLOWED_STRFTIME_DIRECTIVES and d != "%"}
    )
    if unknown:
        return (
            f"`date_format` {date_format!r} uses unsupported directive(s) "
            f"{', '.join('%' + d for d in unknown)}. Allowed: "
            f"{', '.join('%' + d for d in sorted(_ALLOWED_STRFTIME_DIRECTIVES))}."
        )
    return None


def _truncation_error(
    grid: Sequence[Sequence[str]],
    header_row: int,
    proposal: ProfileProposal,
    parsed_count: int,
) -> str:
    """The message for a short parse — which row stopped it, and what its date cell says.

    Rebuilt from the grid rather than guessed, so the model is told the actual
    offending cell text. `bank_adapter` stops at the first unparseable value-date
    cell and says nothing, which is the right behaviour for a production parse of a
    file with a trailing summary block and the wrong behaviour to hand a model with
    no further information.
    """
    stop_row = header_row + 1 + parsed_count
    column_index = {name: index for index, name in enumerate(grid[header_row])}
    index = column_index.get(proposal.value_date_column)
    cell = ""
    if 0 <= stop_row < len(grid) and index is not None and index < len(grid[stop_row]):
        cell = grid[stop_row][index]
    return (
        f"the adapter parsed only {parsed_count} of the {_dense_row_count(grid, header_row)} "
        f"transaction rows in the file. It stops at the first row whose "
        f"`value_date_column` ({proposal.value_date_column!r}) cell does not parse under "
        f"`date_format` ({proposal.date_format!r}); that cell is {cell!r}. Either the date "
        "format or the value-date column is wrong."
    )


def _signed_rupees(paise: int) -> str:
    """A signed rupee string for a *movement* (a balance delta), not a magnitude.

    `money.paise_to_rupees_string` is built for §3.1's money fields, every one of
    which is a non-negative magnitude (see `money.NonNegPaise`), and its `divmod`
    floors — so it would render a negative delta a rupee off. Balance deltas are the
    one place in this codebase where a signed figure is meaningful, and they exist
    only inside an error message, so the sign is split off here rather than by
    widening a shared money primitive to a case it was deliberately not built for.
    """
    return ("-" if paise < 0 else "") + paise_to_rupees_string(abs(paise))


def _direction_error(lines: Sequence[BankLine]) -> str | None:
    for position, line in enumerate(lines, start=1):
        has_withdrawal = line.withdrawal_paise > 0
        has_deposit = line.deposit_paise > 0
        if has_withdrawal == has_deposit:
            both = "both a withdrawal and a deposit" if has_withdrawal else "neither a withdrawal nor a deposit"
            return (
                f"transaction row {position} ({line.narration!r}) parsed with {both}. Every "
                "row of a bank statement moves money in exactly one direction, so "
                f"`withdrawal_column` ({paise_to_rupees_string(line.withdrawal_paise)}) and "
                f"`deposit_column` ({paise_to_rupees_string(line.deposit_paise)}) are not both "
                "pointing at the right columns."
            )
    return None


def _balance_error(lines: Sequence[BankLine]) -> str | None:
    """Double-entry continuity on the file's own running balance. See the module
    docstring: this is the check that catches a debit/credit swap."""
    for position in range(1, len(lines)):
        previous, current = lines[position - 1], lines[position]
        movement = current.deposit_paise - current.withdrawal_paise
        delta = current.closing_balance_paise - previous.closing_balance_paise
        if movement != delta:
            return (
                f"the running balance does not agree with the amounts on transaction row "
                f"{position + 1} ({current.narration!r}): the balance moved by "
                f"{_signed_rupees(delta)} but `deposit_column` minus "
                f"`withdrawal_column` is {_signed_rupees(movement)}. A balance that "
                "moves the opposite way means `withdrawal_column` and `deposit_column` are "
                "swapped; a balance that moves by an unrelated amount means "
                "`balance_column` is the wrong column."
            )
    return None


def verify_proposal(
    path: str | Path,
    proposal: ProfileProposal,
    *,
    bank_profile: BankProfile = BankProfile.HDFC,
) -> VerificationOutcome:
    """Run `proposal` against the real file through the real adapter, and report.

    Nine checks, first failure wins, each reported in terms the next prompt can act
    on. See this module's docstring for what each check is for and why the balance
    continuity one is the important one. Never raises on a bad proposal — a
    verification failure is data (`VerificationOutcome.ok == False`), because it is
    the loop's normal control flow, not an exceptional condition.
    """
    passed: list[str] = []

    def failure(check: str, message: str, **extra) -> VerificationOutcome:
        return VerificationOutcome(
            ok=False, checks_passed=tuple(passed), failed_check=check, error=message, **extra
        )

    date_error = _date_format_error(proposal.date_format)
    if date_error is not None:
        return failure("date_format_is_strftime", date_error)
    passed.append("date_format_is_strftime")

    header = tuple(proposal.header)
    missing = [name for name in proposal.mapped_columns() if name not in header]
    if not header:
        return failure("mapped_columns_exist", "`header` was empty; it must list the table header row's cells.")
    if missing:
        return failure(
            "mapped_columns_exist",
            f"column name(s) {missing!r} do not appear in the `header` you declared "
            f"({list(header)!r}). Every `*_column` must be one of those strings exactly.",
        )
    passed.append("mapped_columns_exist")

    grid = read_statement_grid(path)
    header_row = _find_declared_header_row(grid, header)
    if header_row is None:
        return failure(
            "header_row_found",
            f"no row of the file equals the `header` you declared ({list(header)!r}). The "
            "header row must be copied cell for cell, in file order, from the row that "
            "labels the transaction table.",
        )
    passed.append("header_row_found")

    expected = _dense_row_count(grid, header_row)
    yaml_text = proposal.to_yaml(bank_profile=bank_profile)
    try:
        with _candidate_profile_dir(yaml_text):
            lines = parse_bank_statement(path, profile=_CANDIDATE_PROFILE_NAME)
    except Exception as exc:  # noqa: BLE001 - any adapter failure is a repair signal, not a crash
        return failure(
            "adapter_parses",
            f"running the adapter with this profile raised {type(exc).__name__}: {exc}. A "
            "money column mapped onto a text column does this, because amounts are parsed "
            "as exact integer paise and a narration is not a number.",
            expected_row_count=expected,
        )
    passed.append("adapter_parses")

    if not lines:
        return failure(
            "rows_parsed",
            "the adapter parsed 0 transaction rows. The header row was found, so the "
            f"`value_date_column` ({proposal.value_date_column!r}) or the `date_format` "
            f"({proposal.date_format!r}) does not match this file's date cells.",
            expected_row_count=expected,
        )
    passed.append("rows_parsed")

    if len(lines) != expected:
        return failure(
            "all_rows_parsed",
            _truncation_error(grid, header_row, proposal, len(lines)),
            row_count=len(lines),
            expected_row_count=expected,
        )
    passed.append("all_rows_parsed")

    blank = next((position for position, line in enumerate(lines, start=1) if not line.narration.strip()), None)
    if blank is not None:
        return failure(
            "narrations_non_empty",
            f"transaction row {blank} parsed with an empty narration, so "
            f"`narration_column` ({proposal.narration_column!r}) is not the free-text "
            "description column.",
            row_count=len(lines),
            expected_row_count=expected,
        )
    passed.append("narrations_non_empty")

    direction_error = _direction_error(lines)
    if direction_error is not None:
        return failure(
            "direction_coherent", direction_error, row_count=len(lines), expected_row_count=expected
        )
    passed.append("direction_coherent")

    balance_error = _balance_error(lines)
    if balance_error is not None:
        return failure(
            "balance_continuity", balance_error, row_count=len(lines), expected_row_count=expected
        )
    passed.append("balance_continuity")

    return VerificationOutcome(
        ok=True,
        checks_passed=tuple(passed),
        row_count=len(lines),
        expected_row_count=expected,
        first_value_date=lines[0].value_date,
        last_value_date=lines[-1].value_date,
        total_withdrawal_paise=sum(line.withdrawal_paise for line in lines),
        total_deposit_paise=sum(line.deposit_paise for line in lines),
    )


def _find_declared_header_row(grid: Sequence[Sequence[str]], header: tuple[str, ...]) -> int | None:
    """Where `bank_adapter._find_header_row` would find this header — or `None`.

    Duplicated logic is a real cost, paid here for one reason: the adapter's version
    raises `ValueError` from inside the parse, by which point the failure is
    indistinguishable from a dozen others and carries no diagnosis. Running the same
    match first turns "header row not found in statement" into a check with a name
    and a repair instruction. The matching rule itself is not re-invented — trailing
    blanks trimmed, exact ordered equality, the same as the adapter — and
    `tests/test_adapter_inference.py` pins the two implementations to the same answer
    so they cannot drift.
    """
    target = list(header)
    for index, row in enumerate(grid):
        trimmed = list(row)
        while trimmed and trimmed[-1] == "":
            trimmed.pop()
        if trimmed == target:
            return index
    return None


# --- The loop. ---


def infer_bank_profile(
    path: str | Path,
    cache: PromptCache,
    *,
    mode: CacheMode,
    client: LLMClient | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    bank_profile: BankProfile = BankProfile.HDFC,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    yaml_comment_lines: Sequence[str] = (),
) -> InferenceResult:
    """Propose -> verify -> repair a column map for the statement at `path`.

    The cache/mode/client contract is `pipeline.classifier.classify_case_llm`'s,
    unchanged: `CacheMode.STRICT` never constructs a network path and a miss is
    `CacheMissError` (§4.3 — "a hard error rather than a fallthrough to the API"), so
    a fully-cached run needs no `client` and no `FIREWORKS_API_KEY`;
    `CacheMode.REFRESH` is the only mode that calls Fireworks. Because attempt *n*'s
    prompt embeds attempts 1..*n-1*'s failures, a repair sequence caches as a chain
    of distinct entries and replays offline in the same order, with the same
    verdicts — the loop is reproducible under NFR-01 even though it is adaptive.

    Termination is by attempt budget, and both terminal states are ordinary returns:
    `accepted=True` with a `profile_yaml` to review, or `accepted=False` with
    `give_up_reason` and every attempt's proposal and rejection retained. The only
    exceptions that escape are `CacheMissError` (a run configured offline against an
    unpopulated cache — an operator error, not a model failure) and
    `ProposalResponseError` (a broken client or corrupted cache entry).
    """
    grid = read_statement_grid(path)
    sample = [list(row) for row in grid[:sample_rows]]

    attempts: list[InferenceAttempt] = []
    for index in range(1, max_attempts + 1):
        prompt = build_proposal_prompt(sample, previous=attempts)
        raw = cache.get(prompt)
        from_cache = raw is not None
        if raw is None:
            if mode is CacheMode.STRICT:
                raise CacheMissError(
                    f"no cached profile proposal for attempt {index} on {Path(path).name!r}; "
                    "run with --cache-mode refresh to populate it"
                )
            if client is None:
                raise ValueError("CacheMode.REFRESH on a cache miss requires a client")
            raw = client.complete(prompt, response_schema=PROPOSAL_JSON_SCHEMA)
            cache.put(prompt, raw)

        proposal = parse_proposal_response(raw)
        verification = verify_proposal(path, proposal, bank_profile=bank_profile)
        attempts.append(
            InferenceAttempt(
                index=index,
                from_cache=from_cache,
                raw_response=raw,
                proposal=proposal,
                verification=verification,
            )
        )

        if verification.ok:
            comments = list(yaml_comment_lines) or _default_comment_lines(path, index, max_attempts)
            return InferenceResult(
                accepted=True,
                attempts=tuple(attempts),
                profile_yaml=proposal.to_yaml(bank_profile=bank_profile, comment_lines=comments),
                proposal=proposal,
                verification=verification,
            )

    failed_checks = ", ".join(f"attempt {a.index}: {a.verification.failed_check}" for a in attempts)
    return InferenceResult(
        accepted=False,
        attempts=tuple(attempts),
        give_up_reason=(
            f"no proposal passed verification within {max_attempts} attempts ({failed_checks}). "
            "No profile was written and no bank line was produced: the adapter still has no "
            "mapping for this file, which is the correct state to be in when the mapping is "
            "not known to be right."
        ),
    )


def _default_comment_lines(path: str | Path, attempt: int, max_attempts: int) -> list[str]:
    """The provenance header on an accepted profile. A reviewer opening this file
    beside `hdfc.yaml` must be able to tell at a glance that a model wrote it, which
    file it was inferred from, on which attempt, and that deterministic code — not
    the model — accepted it."""
    return [
        "FR-08 column map INFERRED by pipeline/adapters/inference.py, not hand-written.",
        f"Source export: {Path(path).name}. Accepted on attempt {attempt} of {max_attempts},",
        "after verification against that file by pipeline/adapters/bank_adapter.py:",
        "header located, every transaction row parsed, narrations non-empty, each row",
        "single-direction, and the file's own running balance reconciled against the",
        "withdrawal/deposit columns in integer paise. Review before committing.",
    ]
