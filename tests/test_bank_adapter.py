"""Session 3.1 checkpoint (spec.md §6.3): "All three profiles parse to an
identical canonical `bank_line`."

Exercises both halves the session built: `generator/bank_export.py`'s
per-profile writer (junk headers, trailing summary blocks, `DD/MM/YY` and
`DD-MM-YYYY` dates, comma-grouped amounts, withdrawal/deposit versus
debit/credit naming) and `pipeline/adapters/bank_adapter.py`'s parser that
reads any of the three profiles back into `pipeline.schemas.BankLine`.
"""

from __future__ import annotations

import datetime as dt
import random
from pathlib import Path

import pytest

from generator.bank_export import write_bank_statement
from generator.cli import generate_reference_batch
from pipeline.adapters.bank_adapter import parse_bank_statement
from pipeline.adapters.profiles import all_profile_names, load_profile
from pipeline.money import paise_to_rupees_string, rupees_string_to_paise
from pipeline.schemas import BankLine, BankProfile

PROFILES = ("hdfc", "icici", "axis")


def _sample_records() -> list[BankLine]:
    """A handful of hand-built lines exercising the quirks §2.6 names explicitly.

    `bank_ref_no` is left `None` throughout — ICICI-shape has no ref/cheque
    column at all (§2.6), so a non-null value could never round-trip
    identically across all three profiles. HDFC's and Axis's own ref-number
    columns are covered separately in
    `test_ref_no_is_profile_specific_not_cross_profile_identical`.
    """
    return [
        BankLine(
            line_id="bank_00000001",
            value_date=dt.date(2026, 8, 20),
            narration="NEFT IN/UTR/HDFC0001234ABCDEF/ABC MERCHANT PVT LTD",
            bank_ref_no=None,
            withdrawal_paise=0,
            deposit_paise=123_456_789,
            closing_balance_paise=987_654_321,
            bank_profile=BankProfile.HDFC,
        ),
        BankLine(
            line_id="bank_00000002",
            value_date=dt.date(2026, 8, 21),
            narration="CHEQUE BOOK CHARGES",
            bank_ref_no=None,
            withdrawal_paise=50_000,
            deposit_paise=0,
            closing_balance_paise=50_000,
            bank_profile=BankProfile.HDFC,
        ),
        BankLine(
            line_id="bank_00000003",
            value_date=dt.date(2026, 8, 28),
            narration="BY TRANSFER-NEFT*RCHI786303966501*RAZORPAY SOFTWARE PVT LTD",
            bank_ref_no=None,
            withdrawal_paise=0,
            deposit_paise=1_00_00_000,
            closing_balance_paise=5_43_21_000,
            bank_profile=BankProfile.HDFC,
        ),
    ]


def _content_fields(line: BankLine) -> tuple:
    """Every field except `bank_profile`, which is expected to differ (it *is* the profile tag)."""
    return (
        line.line_id,
        line.value_date,
        line.narration,
        line.bank_ref_no,
        line.withdrawal_paise,
        line.deposit_paise,
        line.closing_balance_paise,
    )


# --- The checkpoint itself. ---


@pytest.mark.parametrize("suffix", ["csv", "xlsx"])
def test_all_three_profiles_parse_to_an_identical_canonical_bank_line(tmp_path: Path, suffix: str) -> None:
    records = _sample_records()
    parsed_by_profile: dict[str, list[BankLine]] = {}
    for profile in PROFILES:
        path = tmp_path / f"{profile}.{suffix}"
        write_bank_statement(records, profile=profile, path=path)
        parsed_by_profile[profile] = parse_bank_statement(path, profile=profile)

    hdfc, icici, axis = (parsed_by_profile[p] for p in PROFILES)
    assert len(hdfc) == len(icici) == len(axis) == len(records)
    for h, i, a in zip(hdfc, icici, axis):
        assert _content_fields(h) == _content_fields(i) == _content_fields(a)
        assert h.bank_profile == BankProfile.HDFC
        assert i.bank_profile == BankProfile.ICICI
        assert a.bank_profile == BankProfile.AXIS


def test_the_full_reference_batchs_bank_lines_round_trip_on_every_profile(tmp_path: Path) -> None:
    """Not just a handful of hand-picked lines — every bank_line the generator actually produces,
    including embedded/truncated/absent UTR narrations and comma-grouped amounts in the thousands."""
    batch = generate_reference_batch(random.Random(1), dt.date(2026, 8, 28))
    neutral = [line.model_copy(update={"bank_ref_no": None}) for line in batch.bank_lines]
    assert len(neutral) > 100

    for profile in PROFILES:
        path = tmp_path / f"reference_{profile}.csv"
        write_bank_statement(neutral, profile=profile, path=path)
        parsed = parse_bank_statement(path, profile=profile)
        assert len(parsed) == len(neutral)
        for original, got in zip(neutral, parsed):
            assert (original.value_date, original.narration, original.withdrawal_paise, original.deposit_paise, original.closing_balance_paise) == (
                got.value_date,
                got.narration,
                got.withdrawal_paise,
                got.deposit_paise,
                got.closing_balance_paise,
            )


# --- The individual quirks §2.6 names. ---


def test_junk_header_rows_and_trailing_summary_block_are_skipped(tmp_path: Path) -> None:
    records = _sample_records()
    for profile in PROFILES:
        path = tmp_path / f"{profile}.csv"
        write_bank_statement(records, profile=profile, path=path)
        raw_line_count = len(path.read_text(encoding="utf-8").splitlines())
        config = load_profile(profile)
        # header row + data rows is strictly fewer than the raw file, which also
        # carries junk header lines above and a trailing summary block below.
        assert raw_line_count > len(records) + 1
        parsed = parse_bank_statement(path, profile=profile)
        assert len(parsed) == len(records)


def test_hdfc_uses_ddmmyy_and_iciciaxis_use_ddmmyyyy() -> None:
    assert load_profile("hdfc").date_format == "%d/%m/%y"
    assert load_profile("icici").date_format == "%d-%m-%Y"
    assert load_profile("axis").date_format == "%d-%m-%Y"


def test_withdrawal_deposit_naming_versus_debit_credit_naming(tmp_path: Path) -> None:
    records = _sample_records()
    hdfc_path = tmp_path / "hdfc.csv"
    axis_path = tmp_path / "axis.csv"
    write_bank_statement(records, profile="hdfc", path=hdfc_path)
    write_bank_statement(records, profile="axis", path=axis_path)

    hdfc_text = hdfc_path.read_text(encoding="utf-8")
    axis_text = axis_path.read_text(encoding="utf-8")
    assert "Withdrawal Amt." in hdfc_text and "Deposit Amt." in hdfc_text
    assert "Debit" in axis_text and "Credit" in axis_text
    assert "Withdrawal" not in axis_text
    assert "Deposit Amt." not in axis_text


def test_ref_no_is_profile_specific_not_cross_profile_identical(tmp_path: Path) -> None:
    """HDFC's `Chq./Ref.No.` and Axis's `Chq No` columns preserve `bank_ref_no`;
    ICICI-shape (§2.6) has no such column at all, so it always parses to `None`."""
    record = _sample_records()[0].model_copy(update={"bank_ref_no": "REFNO12345"})
    for profile, expected in (("hdfc", "REFNO12345"), ("axis", "REFNO12345"), ("icici", None)):
        path = tmp_path / f"{profile}.csv"
        write_bank_statement([record], profile=profile, path=path)
        parsed = parse_bank_statement(path, profile=profile)
        assert parsed[0].bank_ref_no == expected


def test_comma_grouped_amounts_are_present_in_the_raw_export_and_parse_back_exact(tmp_path: Path) -> None:
    record = _sample_records()[0]  # deposit_paise = 123_456_789 -> rupees 12,34,567.89
    path = tmp_path / "hdfc.csv"
    write_bank_statement([record], profile="hdfc", path=path)
    assert "12,34,567.89" in path.read_text(encoding="utf-8")
    parsed = parse_bank_statement(path, profile="hdfc")
    assert parsed[0].deposit_paise == 123_456_789


@pytest.mark.parametrize("profile", PROFILES)
def test_csv_and_xlsx_parse_to_the_same_result(tmp_path: Path, profile: str) -> None:
    records = _sample_records()
    csv_path, xlsx_path = tmp_path / f"{profile}.csv", tmp_path / f"{profile}.xlsx"
    write_bank_statement(records, profile=profile, path=csv_path)
    write_bank_statement(records, profile=profile, path=xlsx_path)
    from_csv = parse_bank_statement(csv_path, profile=profile)
    from_xlsx = parse_bank_statement(xlsx_path, profile=profile)
    assert [_content_fields(line) for line in from_csv] == [_content_fields(line) for line in from_xlsx]


def test_reparsing_the_same_file_is_deterministic(tmp_path: Path) -> None:
    records = _sample_records()
    path = tmp_path / "hdfc.csv"
    write_bank_statement(records, profile="hdfc", path=path)
    first = parse_bank_statement(path, profile="hdfc")
    second = parse_bank_statement(path, profile="hdfc")
    assert [_content_fields(line) for line in first] == [_content_fields(line) for line in second]


def test_all_profile_names_matches_the_bank_profile_enum() -> None:
    assert set(all_profile_names()) == {profile.value for profile in BankProfile}


# --- pipeline.money's rupee<->paise string conversion (no float, no Decimal). ---


@pytest.mark.parametrize(
    "paise,text",
    [
        (0, "0.00"),
        (1, "0.01"),
        (99, "0.99"),
        (100, "1.00"),
        (150_000, "1,500.00"),
        (99_900, "999.00"),
        (100_000, "1,000.00"),
        (123_456_789, "12,34,567.89"),
        (5_43_21_00000, "5,43,21,000.00"),
    ],
)
def test_paise_to_rupees_string_indian_grouping(paise: int, text: str) -> None:
    assert paise_to_rupees_string(paise) == text


@pytest.mark.parametrize(
    "text,paise",
    [
        ("0.00", 0),
        ("", 0),
        ("1,500.00", 150_000),
        ("12,34,567.89", 123_456_789),
        (" 999.00 ", 99_900),
        ("1000", 100_000),
    ],
)
def test_rupees_string_to_paise(text: str, paise: int) -> None:
    assert rupees_string_to_paise(text) == paise


@pytest.mark.parametrize("paise", [0, 1, 99, 100, 999, 1000, 50_000, 123_456_789, 999_999_999_99])
def test_rupees_paise_round_trip(paise: int) -> None:
    assert rupees_string_to_paise(paise_to_rupees_string(paise)) == paise
