"""Agentic bank-profile inference (`pipeline/adapters/inference.py`): the
propose -> verify -> repair loop over a bank the bank adapter has no hand-written profile for.

Two things are being tested here, and they are tested by different means on purpose.

**The loop's control flow** — accept on attempt 1, repair on a named failure and
accept on attempt 2, exhaust the budget and give up cleanly on attempt 3 — is driven
by `_ScriptedClient`, a stub `LLMClient` that never touches a socket. Every branch
including the ones a real model happens not to take today is reachable and pinned,
and the suite stays network-free and credential-free, the same rule
`tests/test_llm_slot_a.py` sets for Slot A.

**The loop's actual result against a real model** is tested by replaying
`data/adapter_cache.json`, the committed responses from a real
`pipeline.llm_client.FIREWORKS_MODEL_ID` refresh run, under `CacheMode.STRICT` with
`client=None` — so the checks below assert what Fireworks really proposed for these
two files, not what a stub was told to say, and still never open a connection
(the determinism layers: strict mode is a hard error on a miss, never a fallthrough to the API).

The two committed fixtures are chosen to be opposite results, because only reporting
the one that works would be dressing up the measurement:

- `data/unseen_bank/kotak_statement.csv` — a Kotak-shaped export (junk header block,
  `Sl No` serial column, `DD/MM/YYYY` dates, comma-grouped amounts, separate
  `Withdrawal (Dr)`/`Deposit (Cr)` columns, a trailing summary block). Nothing in
  `profiles/` describes it. The loop infers a working profile.
- `data/unseen_bank/yesbank_statement.csv` — a single `Amount (INR)` column plus a
  separate `Dr/Cr` indicator. The bank adapter's profile schema has two money columns and no
  direction flag, so **no** column map can express this file, and the honest
  outcome is a bounded give-up rather than a plausible-looking profile that books
  every debit as a credit. The loop gives up.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from pipeline.adapters import profiles as profiles_module
from pipeline.adapters.bank_adapter import _find_header_row, _read_grid
from pipeline.adapters.inference import (
    PROPOSAL_JSON_SCHEMA,
    InferenceResult,
    ProfileProposal,
    ProposalResponseError,
    _find_declared_header_row,
    build_proposal_prompt,
    infer_bank_profile,
    parse_proposal_response,
    read_statement_grid,
    verify_proposal,
)
from pipeline.llm_cache import CacheMissError, CacheMode, PromptCache
from pipeline.money import rupees_string_to_paise
from pipeline.schemas import BankProfile

REPO_ROOT = Path(__file__).resolve().parent.parent
KOTAK = REPO_ROOT / "data" / "unseen_bank" / "kotak_statement.csv"
YESBANK = REPO_ROOT / "data" / "unseen_bank" / "yesbank_statement.csv"
COMMITTED_CACHE = REPO_ROOT / "data" / "adapter_cache.json"

KOTAK_HEADER = (
    "Sl No",
    "Txn Date",
    "Value Date",
    "Transaction Details",
    "Chq/Ref Number",
    "Withdrawal (Dr)",
    "Deposit (Cr)",
    "Balance (INR)",
)
KOTAK_ROWS = 12


def _correct_kotak_proposal(**overrides) -> ProfileProposal:
    """The mapping a human would hand-write for `kotak_statement.csv`."""
    fields = dict(
        date_format="%d/%m/%Y",
        header=KOTAK_HEADER,
        value_date_column="Value Date",
        transaction_date_column="Txn Date",
        narration_column="Transaction Details",
        ref_no_column="Chq/Ref Number",
        withdrawal_column="Withdrawal (Dr)",
        deposit_column="Deposit (Cr)",
        balance_column="Balance (INR)",
    )
    fields.update(overrides)
    return ProfileProposal(**fields)


class _ScriptedClient:
    """A fake `LLMClient` that answers with a fixed script of proposals, in order.

    Records every prompt it was handed, so a test can assert that attempt *n*'s
    prompt actually carried attempt *n-1*'s verification error — the feedback edge is
    the thing that makes this loop agentic rather than a retry, and asserting only on
    the final result would not distinguish the two.
    """

    def __init__(self, *proposals: ProfileProposal) -> None:
        self._script = list(proposals)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    def complete(self, prompt: str, *, response_schema: dict) -> str:
        self.prompts.append(prompt)
        self.schemas.append(response_schema)
        proposal = self._script[min(len(self.prompts) - 1, len(self._script) - 1)]
        return json.dumps(proposal.to_payload())


def _run(client, tmp_path: Path, *, max_attempts: int = 3, statement: Path = KOTAK) -> InferenceResult:
    return infer_bank_profile(
        statement,
        PromptCache(tmp_path / "cache.json"),
        mode=CacheMode.REFRESH,
        client=client,
        max_attempts=max_attempts,
    )


# --- The loop's three terminal shapes. ---


def test_a_correct_proposal_is_accepted_on_the_first_attempt(tmp_path: Path) -> None:
    client = _ScriptedClient(_correct_kotak_proposal())
    result = _run(client, tmp_path)

    assert result.accepted
    assert result.accepted_on_attempt == 1
    assert len(client.prompts) == 1
    assert result.verification is not None and result.verification.ok
    assert result.verification.row_count == KOTAK_ROWS
    assert result.verification.checks_passed[-1] == "balance_continuity"


def test_a_wrong_date_format_is_repaired_and_accepted_on_the_second_attempt(tmp_path: Path) -> None:
    """The repair edge itself: attempt 1 reads DD/MM as MM/DD, the adapter silently
    truncates the table at the first day-of-month above 12, and the verifier hands
    the model that exact cell back."""
    client = _ScriptedClient(_correct_kotak_proposal(date_format="%m/%d/%Y"), _correct_kotak_proposal())
    result = _run(client, tmp_path)

    assert result.accepted
    assert result.accepted_on_attempt == 2
    assert result.attempts[0].verification.failed_check == "all_rows_parsed"

    repair_prompt = client.prompts[1]
    assert "all_rows_parsed" in repair_prompt
    assert "'14/08/2026'" in repair_prompt  # the row that stopped the parse, quoted verbatim
    assert "%m/%d/%Y" in repair_prompt  # the model's own failed proposal, quoted back
    assert client.prompts[0] in repair_prompt  # attempt 2 is a strictly more-informed question


def test_three_failures_terminate_with_a_clean_give_up_not_an_exception(tmp_path: Path) -> None:
    swapped = _correct_kotak_proposal(
        withdrawal_column="Deposit (Cr)", deposit_column="Withdrawal (Dr)"
    )
    client = _ScriptedClient(swapped)
    result = _run(client, tmp_path)

    assert not result.accepted
    assert result.accepted_on_attempt is None
    assert result.profile_yaml is None
    assert len(result.attempts) == 3 == len(client.prompts)
    assert {a.verification.failed_check for a in result.attempts} == {"balance_continuity"}
    assert result.give_up_reason is not None
    assert "balance_continuity" in result.give_up_reason
    # Every attempt is retained, so a give-up is auditable rather than opaque.
    assert all(a.proposal.withdrawal_column == "Deposit (Cr)" for a in result.attempts)


@pytest.mark.parametrize("max_attempts", [1, 2, 3, 5])
def test_the_attempt_budget_bounds_the_number_of_model_calls(tmp_path: Path, max_attempts: int) -> None:
    """Termination is by budget, not by convergence — the loop cannot run away."""
    client = _ScriptedClient(
        _correct_kotak_proposal(withdrawal_column="Deposit (Cr)", deposit_column="Withdrawal (Dr)")
    )
    result = _run(client, tmp_path, max_attempts=max_attempts)
    assert not result.accepted
    assert len(client.prompts) == max_attempts == len(result.attempts)


# --- The verifier: every check, and what it catches. ---


@pytest.mark.parametrize(
    "overrides,expected_check",
    [
        ({"date_format": "DD/MM/YYYY"}, "date_format_is_strftime"),
        ({"date_format": "%d/%m/%Q"}, "date_format_is_strftime"),
        ({"narration_column": "Description"}, "mapped_columns_exist"),
        ({"header": ("Sl No", "Txn Date")}, "mapped_columns_exist"),
        ({"header": KOTAK_HEADER[::-1]}, "header_row_found"),
        ({"withdrawal_column": "Transaction Details"}, "adapter_parses"),
        ({"date_format": "%Y-%m-%d"}, "rows_parsed"),
        ({"date_format": "%m/%d/%Y"}, "all_rows_parsed"),
        ({"narration_column": "Chq/Ref Number"}, "narrations_non_empty"),
        ({"deposit_column": "Withdrawal (Dr)"}, "direction_coherent"),
        ({"withdrawal_column": "Deposit (Cr)", "deposit_column": "Withdrawal (Dr)"}, "balance_continuity"),
        ({"balance_column": "Sl No"}, "balance_continuity"),
    ],
)
def test_each_check_rejects_the_mis_mapping_it_exists_for(overrides: dict, expected_check: str) -> None:
    outcome = verify_proposal(KOTAK, _correct_kotak_proposal(**overrides))
    assert not outcome.ok
    assert outcome.failed_check == expected_check
    assert outcome.error and expected_check not in ("", None)
    # The error is a repair signal for the next prompt, so it must be specific enough
    # to act on: never a bare "invalid profile".
    assert len(outcome.error) > 60


def test_the_correct_mapping_passes_every_check() -> None:
    outcome = verify_proposal(KOTAK, _correct_kotak_proposal())
    assert outcome.ok
    assert outcome.checks_passed == (
        "date_format_is_strftime",
        "mapped_columns_exist",
        "header_row_found",
        "adapter_parses",
        "rows_parsed",
        "all_rows_parsed",
        "narrations_non_empty",
        "direction_coherent",
        "balance_continuity",
    )


def test_a_swapped_debit_credit_mapping_is_caught_only_by_balance_continuity() -> None:
    """The check that earns its keep: a debit/credit swap parses cleanly, yields the
    right row count, non-empty narrations and one direction per row — and books every
    payment backwards. Only the file's own running balance disagrees."""
    swapped = _correct_kotak_proposal(withdrawal_column="Deposit (Cr)", deposit_column="Withdrawal (Dr)")
    outcome = verify_proposal(KOTAK, swapped)
    assert outcome.failed_check == "balance_continuity"
    assert "direction_coherent" in outcome.checks_passed
    assert "all_rows_parsed" in outcome.checks_passed


def test_verification_failure_is_data_not_an_exception() -> None:
    """Every rejection path returns a `VerificationOutcome`; none raises. A raised
    exception would end the loop instead of feeding the next attempt."""
    for overrides in ({"withdrawal_column": "Transaction Details"}, {"header": ("nope",)}):
        outcome = verify_proposal(KOTAK, _correct_kotak_proposal(**overrides))
        assert outcome.ok is False


# --- What the accepted profile actually produces. ---


def test_the_accepted_profile_parses_the_file_to_the_figures_the_file_itself_states(tmp_path: Path) -> None:
    """Not "it parsed" — it parsed *correctly*, against the statement's own printed
    summary block: 12 rows, 9,90,540.00 out, 30,24,871.90 in, closing 38,76,642.45."""
    result = _run(_ScriptedClient(_correct_kotak_proposal()), tmp_path)
    verification = result.verification
    assert verification is not None
    assert verification.row_count == KOTAK_ROWS
    assert verification.first_value_date == dt.date(2026, 8, 5)
    assert verification.last_value_date == dt.date(2026, 8, 28)
    assert verification.total_withdrawal_paise == rupees_string_to_paise("9,90,540.00")
    assert verification.total_deposit_paise == rupees_string_to_paise("30,24,871.90")

    raw = KOTAK.read_text(encoding="utf-8")
    assert "Total Withdrawals : 9,90,540.00" in raw
    assert "Total Deposits : 30,24,871.90" in raw


def test_the_accepted_yaml_is_a_profile_the_existing_loader_can_read(tmp_path: Path) -> None:
    """The deliverable is a file that belongs beside `hdfc.yaml` — same keys, same
    shape, loadable by the same `profiles.load_profile` with no new code path."""
    result = _run(_ScriptedClient(_correct_kotak_proposal()), tmp_path)
    assert result.profile_yaml is not None

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "inferred.yaml").write_text(result.profile_yaml, encoding="utf-8")
    original = profiles_module._PROFILES_DIR
    profiles_module._PROFILES_DIR = profiles_dir
    try:
        config = profiles_module.load_profile("inferred")
    finally:
        profiles_module._PROFILES_DIR = original

    assert config.header == KOTAK_HEADER
    assert config.date_format == "%d/%m/%Y"
    assert config.narration_column == "Transaction Details"
    assert set(yaml.safe_load(result.profile_yaml)) == {
        "bank_profile",
        "date_format",
        "header",
        "value_date_column",
        "transaction_date_column",
        "narration_column",
        "ref_no_column",
        "withdrawal_column",
        "deposit_column",
        "balance_column",
    }
    # Provenance: a reviewer must not mistake this for a hand-written profile.
    assert result.profile_yaml.startswith("# Column map INFERRED")


def test_an_absent_optional_column_renders_as_yaml_null_like_the_icici_profile(tmp_path: Path) -> None:
    proposal = _correct_kotak_proposal(ref_no_column=None, transaction_date_column=None)
    yaml_text = proposal.to_yaml(bank_profile=BankProfile.ICICI)
    assert "ref_no_column: null" in yaml_text
    assert yaml.safe_load(yaml_text)["ref_no_column"] is None
    assert verify_proposal(KOTAK, proposal).ok


# --- The boundary: the model configures the parse, it never supplies a value. ---


def test_the_proposal_schema_cannot_express_a_ledger_value(tmp_path: Path) -> None:
    """The model-slot boundary's boundary, enforced by the decoder rather than by prose: there is no
    field in the sampled space for an amount, an account, or a `bank_profile` tag."""
    properties = PROPOSAL_JSON_SCHEMA["properties"]
    assert PROPOSAL_JSON_SCHEMA["additionalProperties"] is False
    assert "bank_profile" not in properties
    assert set(properties) == set(PROPOSAL_JSON_SCHEMA["required"])
    assert all(key.endswith("_column") or key in ("date_format", "header") for key in properties)

    client = _ScriptedClient(_correct_kotak_proposal())
    _run(client, tmp_path)
    assert client.schemas == [PROPOSAL_JSON_SCHEMA]  # constrained decoding on every call


def test_the_bank_profile_tag_comes_from_the_caller_not_the_model(tmp_path: Path) -> None:
    for tag in BankProfile:
        result = infer_bank_profile(
            KOTAK,
            PromptCache(tmp_path / f"cache_{tag.value}.json"),
            mode=CacheMode.REFRESH,
            client=_ScriptedClient(_correct_kotak_proposal()),
            bank_profile=tag,
        )
        assert result.profile_yaml is not None
        assert yaml.safe_load(result.profile_yaml)["bank_profile"] == tag.value


def test_inference_writes_nothing_into_the_committed_profiles_directory(tmp_path: Path) -> None:
    """A candidate profile is model-authored YAML and must never land in the graded
    package, not even transiently — and the redirected lookup must be restored even
    when the attempt fails."""
    before = sorted(p.name for p in profiles_module._PROFILES_DIR.iterdir())
    original = profiles_module._PROFILES_DIR

    _run(_ScriptedClient(_correct_kotak_proposal()), tmp_path)
    _run(_ScriptedClient(_correct_kotak_proposal(withdrawal_column="Transaction Details")), tmp_path)

    assert profiles_module._PROFILES_DIR == original
    assert sorted(p.name for p in profiles_module._PROFILES_DIR.iterdir()) == before
    assert profiles_module.load_profile("hdfc").bank_profile is BankProfile.HDFC


# --- Prompt and response plumbing. ---


def test_the_prompt_shows_the_raw_file_and_never_its_path() -> None:
    """The model reads rows, not a filename. The bank's name does reach the prompt —
    but only because the file's own junk header block prints it, which is a row the
    model is meant to read past; the *path* is metadata about where the file sits on
    this machine, would differ between two copies of the same export, and so would
    fragment the cache for no inference-relevant reason."""
    grid = read_statement_grid(KOTAK)
    prompt = build_proposal_prompt(grid[:16])
    assert "kotak_statement" not in prompt
    assert str(KOTAK) not in prompt and "data/unseen_bank" not in prompt
    assert "Sl No" in prompt and "KOTAK MAHINDRA BANK LIMITED" in prompt
    assert "Transaction Details" in prompt
    assert build_proposal_prompt(grid[:16]) == prompt  # deterministic


def test_the_prompt_shows_the_junk_header_block_rather_than_a_pre_located_table() -> None:
    prompt = build_proposal_prompt(read_statement_grid(KOTAK)[:16])
    assert "Statement Period : 01/08/2026 to 28/08/2026" in prompt


def test_parse_proposal_response_normalises_empty_strings_to_none() -> None:
    payload = _correct_kotak_proposal().to_payload()
    payload["ref_no_column"] = ""
    payload["transaction_date_column"] = ""
    proposal = parse_proposal_response(json.dumps(payload))
    assert proposal.ref_no_column is None
    assert proposal.transaction_date_column is None


@pytest.mark.parametrize("raw", ["not json", "{}", '{"date_format": "%d/%m/%Y"}', "null"])
def test_a_malformed_response_fails_by_name(raw: str) -> None:
    with pytest.raises(ProposalResponseError):
        parse_proposal_response(raw)


def test_the_duplicated_header_match_agrees_with_the_adapters_own() -> None:
    """`_find_declared_header_row` re-runs `bank_adapter._find_header_row`'s rule to
    turn its `ValueError` into a named check. Pinned here so the two cannot drift."""
    grid = _read_grid(KOTAK)
    assert _find_declared_header_row(grid, KOTAK_HEADER) == _find_header_row(grid, KOTAK_HEADER)
    assert _find_declared_header_row(grid, ("Not", "A", "Header")) is None
    with pytest.raises(ValueError):
        _find_header_row(grid, ("Not", "A", "Header"))


# --- Cache behaviour: offline by default, network only on refresh. ---


def test_strict_mode_never_reaches_for_a_client(tmp_path: Path) -> None:
    with pytest.raises(CacheMissError):
        infer_bank_profile(KOTAK, PromptCache(tmp_path / "empty.json"), mode=CacheMode.STRICT, client=None)


def test_refresh_mode_on_a_miss_requires_a_client(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        infer_bank_profile(KOTAK, PromptCache(tmp_path / "empty.json"), mode=CacheMode.REFRESH, client=None)


def test_a_repair_sequence_caches_as_a_chain_and_replays_offline(tmp_path: Path) -> None:
    """Each attempt is a different prompt, so a two-attempt run caches two entries and
    replays with the same verdicts and no client at all — an adaptive loop that is
    still reproducible from a clean clone."""
    cache_path = tmp_path / "chain.json"
    live = infer_bank_profile(
        KOTAK,
        PromptCache(cache_path),
        mode=CacheMode.REFRESH,
        client=_ScriptedClient(_correct_kotak_proposal(date_format="%m/%d/%Y"), _correct_kotak_proposal()),
    )
    assert live.accepted_on_attempt == 2
    assert len(PromptCache(cache_path)) == 2

    replayed = infer_bank_profile(KOTAK, PromptCache(cache_path), mode=CacheMode.STRICT, client=None)
    assert replayed.accepted_on_attempt == 2
    assert replayed.profile_yaml == live.profile_yaml
    assert [a.verification.failed_check for a in replayed.attempts] == ["all_rows_parsed", None]
    assert all(a.from_cache for a in replayed.attempts)


# --- The measured result: the committed real Fireworks responses, replayed. ---


def _replay(statement: Path) -> InferenceResult:
    return infer_bank_profile(statement, PromptCache(COMMITTED_CACHE), mode=CacheMode.STRICT, client=None)


def test_the_real_model_inferred_a_working_kotak_profile_on_attempt_one() -> None:
    result = _replay(KOTAK)
    assert result.accepted
    assert result.accepted_on_attempt == 1
    assert result.proposal == _correct_kotak_proposal()
    assert result.verification is not None and result.verification.row_count == KOTAK_ROWS


def test_the_real_model_gave_up_cleanly_on_a_file_the_profile_schema_cannot_express() -> None:
    """The measured negative, kept as a test rather than a footnote: an
    `Amount` + `Dr/Cr` statement has no valid column map under the bank adapter's two-money-column
    schema, the model proposes the only thing it can (both money columns pointing at
    `Amount (INR)`), `direction_coherent` rejects it three times, and the loop stops.
    No profile, no bank line, no exception."""
    result = _replay(YESBANK)
    assert not result.accepted
    assert len(result.attempts) == 3
    assert [a.verification.failed_check for a in result.attempts] == ["direction_coherent"] * 3
    assert result.attempts[0].proposal.date_format == "%d-%b-%Y"  # it did read the date shape correctly
    assert result.attempts[0].proposal.withdrawal_column == result.attempts[0].proposal.deposit_column
    assert result.give_up_reason is not None


def test_replaying_the_committed_cache_twice_gives_an_identical_profile() -> None:
    assert _replay(KOTAK).profile_yaml == _replay(KOTAK).profile_yaml


def test_neither_committed_fixture_matches_a_hand_written_profile() -> None:
    """The premise of the exercise: these two banks are genuinely unseen. If either
    file happened to match `hdfc`/`icici`/`axis`, the loop would be inferring nothing."""
    for statement in (KOTAK, YESBANK):
        grid = _read_grid(statement)
        for name in profiles_module.all_profile_names():
            assert _find_declared_header_row(grid, profiles_module.load_profile(name).header) is None
