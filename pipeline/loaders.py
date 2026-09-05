"""JSONL loaders for the four canonical schemas plus the ground-truth file.

`generator/cli.py` writes each of these as one JSON object per line
(`model_dump_json()` per record). Loading is the inverse: one
`model_validate_json()` per line, in file order. No column mapping, no
adapter logic, because that complexity belongs to `pipeline/adapters/`,
which exists because a *raw* bank export has none of this structure. The
seeded reference batch is already in canonical post-generation shape, so
"loading" it is exactly this and nothing more.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from pipeline.ground_truth import GroundTruthCase
from pipeline.schemas import BankLine, LedgerEntry, ReconLine, Settlement

_M = TypeVar("_M", bound=BaseModel)


def _load_jsonl(path: Path, model: type[_M]) -> list[_M]:
    with path.open("r", encoding="utf-8") as f:
        return [model.model_validate_json(line) for line in f if line.strip()]


def load_settlements(path: Path) -> list[Settlement]:
    return _load_jsonl(path, Settlement)


def load_recon_lines(path: Path) -> list[ReconLine]:
    return _load_jsonl(path, ReconLine)


def load_ledger_entries(path: Path) -> list[LedgerEntry]:
    return _load_jsonl(path, LedgerEntry)


def load_bank_lines(path: Path) -> list[BankLine]:
    return _load_jsonl(path, BankLine)


def load_ground_truth(path: Path) -> list[GroundTruthCase]:
    return _load_jsonl(path, GroundTruthCase)
