"""FR-08's bank statement adapter: parses a CSV or XLSX export in any of
the three declared profiles (`profiles/*.yaml`) into canonical `bank_line`
records (`pipeline.schemas.BankLine`).

The table's boundaries are found rather than configured, because a real
export's junk-header-row count and trailing-summary-block shape are not
knowable ahead of time (§2.6's own list of quirks the adapter MUST
handle): the header row is the first row whose non-empty cells equal the
profile's declared header exactly; the table ends at the first row below
it whose value-date cell fails to parse under the profile's date format —
which a blank separator row and a summary-block heading both do, so one
rule covers both trailing-block shapes without special-casing either.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pandas as pd

from pipeline.adapters.profiles import load_profile
from pipeline.money import Paise, rupees_string_to_paise
from pipeline.schemas import BankLine


def parse_bank_statement(path: str | Path, *, profile: str) -> list[BankLine]:
    """Parse `path` (CSV or XLSX, by extension) under the named profile into canonical `BankLine`s."""
    config = load_profile(profile)
    grid = _read_grid(path)
    header_row = _find_header_row(grid, config.header)
    column_index = {name: index for index, name in enumerate(grid[header_row])}

    lines: list[BankLine] = []
    row_index = 0
    for raw_row in grid[header_row + 1 :]:
        value_date = _try_parse_date(raw_row, column_index[config.value_date_column], config.date_format)
        if value_date is None:
            break

        narration = _cell(raw_row, column_index[config.narration_column])
        withdrawal = rupees_string_to_paise(_cell(raw_row, column_index[config.withdrawal_column]))
        deposit = rupees_string_to_paise(_cell(raw_row, column_index[config.deposit_column]))
        balance = rupees_string_to_paise(_cell(raw_row, column_index[config.balance_column]))
        ref_no = None
        if config.ref_no_column is not None:
            ref_no = _cell(raw_row, column_index[config.ref_no_column]) or None

        lines.append(
            BankLine(
                line_id=_synthetic_line_id(row_index, narration, value_date, withdrawal, deposit),
                value_date=value_date,
                narration=narration,
                bank_ref_no=ref_no,
                withdrawal_paise=Paise(withdrawal),
                deposit_paise=Paise(deposit),
                closing_balance_paise=Paise(balance),
                bank_profile=config.bank_profile,
            )
        )
        row_index += 1
    return lines


def _read_grid(path: str | Path) -> list[list[str]]:
    """The whole file as a grid of stripped strings, header-agnostic — junk rows and all."""
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        frame = pd.read_excel(path, header=None, dtype=str, engine="openpyxl")
    else:
        frame = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    frame = frame.fillna("")
    return [[("" if cell is None else str(cell)).strip() for cell in row] for row in frame.itertuples(index=False)]


def _find_header_row(grid: list[list[str]], header: tuple[str, ...]) -> int:
    target = list(header)
    for index, row in enumerate(grid):
        if _trim_trailing_blanks(row) == target:
            return index
    raise ValueError(f"header row {target!r} not found in statement")


def _trim_trailing_blanks(row: list[str]) -> list[str]:
    trimmed = list(row)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return trimmed


def _cell(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _try_parse_date(row: list[str], index: int, date_format: str) -> dt.date | None:
    text = _cell(row, index)
    if not text:
        return None
    try:
        return dt.datetime.strptime(text, date_format).date()
    except ValueError:
        return None


def _synthetic_line_id(row_index: int, narration: str, value_date: dt.date, withdrawal: int, deposit: int) -> str:
    """`line_id` is "string, unique, synthetic" (§3.1) — a real bank export carries no id column.

    Derived from row position plus content rather than drawn at random:
    the adapter has no RNG (parsing is deterministic by construction, not
    by seeding), and re-parsing the same file must yield the same ids.
    Position alone would collide across files; content alone would
    collide on a genuine `DUPLICATE_CREDIT` pair. Excluding the profile
    tag from the hash is deliberate — it is what lets the same underlying
    row parsed from three different profile-shaped files land on the same
    canonical id (spec.md §6.3's session-3.1 checkpoint).
    """
    digest = hashlib.sha1(
        f"{row_index}|{narration}|{value_date.isoformat()}|{withdrawal}|{deposit}".encode()
    ).hexdigest()
    return f"bank_{digest[:8]}"
