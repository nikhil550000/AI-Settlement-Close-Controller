"""Loader for the FR-08 declarative column-mapping profiles.

Per §2.6 and §4.5: "The three FR-08 profiles are YAML column maps, not
code." Each `profiles/<name>.yaml` file names its bank-specific header row
verbatim, its date format, and which header text maps to which canonical
`pipeline.schemas.BankLine` field. `generator/bank_export.py`'s writer
loads the same file to render realistic per-profile exports, so the
writer and `bank_adapter.py`'s parser can never drift out of sync with
each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from pipeline.schemas import BankProfile

_PROFILES_DIR = Path(__file__).parent / "profiles"


@dataclass(frozen=True)
class BankProfileConfig:
    bank_profile: BankProfile
    date_format: str
    header: tuple[str, ...]
    value_date_column: str
    transaction_date_column: str | None
    narration_column: str
    ref_no_column: str | None
    withdrawal_column: str
    deposit_column: str
    balance_column: str


def load_profile(name: str) -> BankProfileConfig:
    """`name` is a `BankProfile` value (`hdfc` | `icici` | `axis`)."""
    data = yaml.safe_load((_PROFILES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return BankProfileConfig(
        bank_profile=BankProfile(data["bank_profile"]),
        date_format=data["date_format"],
        header=tuple(data["header"]),
        value_date_column=data["value_date_column"],
        transaction_date_column=data.get("transaction_date_column"),
        narration_column=data["narration_column"],
        ref_no_column=data.get("ref_no_column"),
        withdrawal_column=data["withdrawal_column"],
        deposit_column=data["deposit_column"],
        balance_column=data["balance_column"],
    )


def all_profile_names() -> tuple[str, ...]:
    return tuple(profile.value for profile in BankProfile)
