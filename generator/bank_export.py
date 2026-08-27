"""Renders canonical `bank_line` records as raw per-profile bank-statement
exports — the input shape `pipeline/adapters/bank_adapter.py` parses.

This is the mirror half of that adapter, per the session 2.2 Next field:
"a generator-side writer that renders those canonical lines out as
per-profile CSV/XLSX carrying the junk and formatting quirks... and the
pipeline-side adapter that reads them back." Both halves load the same
`pipeline/adapters/profiles/*.yaml` declaration, so the header text, date
format, and column-to-field mapping can never drift apart between writer
and parser.

Not wired into `uv run generate` — nothing in the reference batch's JSONL
output needs a raw bank export; this module exists for the session 3.1
round-trip checkpoint and any later session that wants one (a sample
report artifact, an adversarial-set fixture).
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from pipeline.adapters.profiles import BankProfileConfig, load_profile
from pipeline.money import paise_to_rupees_string
from pipeline.schemas import BankLine

_HEADER_JUNK: dict[str, list[str]] = {
    "hdfc": [
        "HDFC BANK LIMITED",
        "Statement of Account",
        "Account No : 50100123456789",
        "Statement From : 01/07/2026 To : 27/08/2026",
        "",
    ],
    "icici": [
        "ICICI Bank Limited",
        "Account Statement",
        "Account No. 000401234567",
        "",
    ],
    "axis": [
        "AXIS BANK LTD",
        "Statement of Transactions",
        "A/c No : 91020012345678",
        "",
    ],
}

_FOOTER_JUNK: dict[str, list[str]] = {
    "hdfc": ["", "STATEMENT SUMMARY", "*** END OF STATEMENT ***"],
    "icici": ["", "This is a computer generated statement", "*** END OF STATEMENT ***"],
    "axis": ["", "Statement Summary", "*** END OF STATEMENT ***"],
}


def write_bank_statement(records: list[BankLine], *, profile: str, path: str | Path) -> None:
    """Write `records`, in the given order, as a raw `profile`-shaped export at `path`.

    File format is chosen by `path`'s suffix — `.xlsx` or anything else
    (`.csv`) — the same rule the adapter's `_read_grid` uses to read it
    back, per §2.6's "CSV and XLSX only."
    """
    config = load_profile(profile)
    path = Path(path)
    rows = [_row_for(config, record) for record in records]
    if path.suffix.lower() == ".xlsx":
        _write_xlsx(config, rows, path, profile)
    else:
        _write_csv(config, rows, path, profile)


def _row_for(config: BankProfileConfig, record: BankLine) -> list[str]:
    date_text = record.value_date.strftime(config.date_format)
    cell_by_column: dict[str, str] = {
        config.value_date_column: date_text,
        config.narration_column: record.narration,
        config.withdrawal_column: paise_to_rupees_string(record.withdrawal_paise) if record.withdrawal_paise else "",
        config.deposit_column: paise_to_rupees_string(record.deposit_paise) if record.deposit_paise else "",
        config.balance_column: paise_to_rupees_string(record.closing_balance_paise),
    }
    if config.transaction_date_column is not None:
        cell_by_column[config.transaction_date_column] = date_text
    if config.ref_no_column is not None:
        cell_by_column[config.ref_no_column] = record.bank_ref_no or ""
    return [cell_by_column.get(column, "") for column in config.header]


def _junk_row(text: str, width: int) -> list[str]:
    return [text] + [""] * (width - 1)


def _write_csv(config: BankProfileConfig, rows: list[list[str]], path: Path, profile: str) -> None:
    import csv

    width = len(config.header)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        for line in _HEADER_JUNK[profile]:
            writer.writerow(_junk_row(line, width))
        writer.writerow(list(config.header))
        for row in rows:
            writer.writerow(row)
        for line in _FOOTER_JUNK[profile]:
            writer.writerow(_junk_row(line, width))


def _write_xlsx(config: BankProfileConfig, rows: list[list[str]], path: Path, profile: str) -> None:
    width = len(config.header)
    workbook = Workbook()
    sheet = workbook.active
    for line in _HEADER_JUNK[profile]:
        sheet.append(_junk_row(line, width))
    sheet.append(list(config.header))
    for row in rows:
        sheet.append(row)
    for line in _FOOTER_JUNK[profile]:
        sheet.append(_junk_row(line, width))
    workbook.save(path)
