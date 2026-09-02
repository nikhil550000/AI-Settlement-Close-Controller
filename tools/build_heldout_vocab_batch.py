"""Rewrite a generated batch onto `tools/heldout_vocabulary.py`'s surface strings.

    uv run python tools/build_heldout_vocab_batch.py data/reference data/heldout_vocab

Structure in, structure out. Every record keeps its ids, amounts, dates,
UTRs, references, directions and linkage exactly; the only fields that change
are the free-text ones — `bank_line.narration`, `recon_line.description`,
`ledger_entry.narration`. `ground_truth.jsonl` is copied byte-for-byte,
because the labels were written from the injection plan (§3.5) and nothing
this script touches can move one.

That is what makes the output a *fair* held-out set rather than a harder one:
the answer key is unchanged and still correct, and the only question put to
the pipeline is whether its decision boundaries were drawn at the concept or
at the literal string. See `tools/heldout_vocabulary.py` for why that question
needed asking.

Rewriting is by template, not by search-and-replace: each narration is matched
against the generator's own `ALL_TEMPLATES` (most specific first, exactly as
`generator.narration.narration_template` does), its `{party}`/`{ref}`/`{method}`
slots are extracted, and the held-out template at the same index is re-rendered
with the same slot values — `{ref}` verbatim, so FR-09's tier cascade sees the
identical token it saw before, and `{party}` through the party map. A narration
that matches no template raises, for the same reason `narration_template` does:
a string from outside the shared pool means the rewrite is incomplete, and
silently passing it through would leave a coupled surface untested.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from generator import narration as g  # noqa: E402
from tools import heldout_vocabulary as h  # noqa: E402

# Index-parallel pool pairs. A length mismatch is a programming error, not a
# data condition, so it is asserted at import rather than handled.
_POOL_PAIRS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (g._LEDGER_NARRATION_TEMPLATES, h.LEDGER_NARRATION_TEMPLATES),
    (g._CLEAN_CREDIT_TEMPLATES, h.CLEAN_CREDIT_TEMPLATES),
    (g._EMBEDDED_CREDIT_TEMPLATES, h.EMBEDDED_CREDIT_TEMPLATES),
    (g._DEBIT_TEMPLATES, h.DEBIT_TEMPLATES),
    (g.REVERSAL_TEMPLATES, h.REVERSAL_TEMPLATES),
    (g._ABSENT_CREDIT_TEMPLATES, h.ABSENT_CREDIT_TEMPLATES),
    (g.OPAQUE_CREDIT_NARRATIONS, h.OPAQUE_CREDIT_NARRATIONS),
    (g._BANK_CHARGE_NARRATIONS, h.BANK_CHARGE_NARRATIONS),
)
for _old, _new in _POOL_PAIRS:
    assert len(_old) == len(_new), (_old, _new)

TEMPLATE_MAP: dict[str, str] = {o: n for old, new in _POOL_PAIRS for o, n in zip(old, new)}

PARTY_MAP: dict[str, str] = {
    **dict(zip(g.SETTLEMENT_PARTIES, h.SETTLEMENT_PARTIES)),
    **dict(zip(g.NAMED_COUNTERPARTIES, h.NAMED_COUNTERPARTIES)),
}

DESCRIPTION_MAP: dict[str, str] = {
    **dict(zip(g.TAX_SIGNATURES, h.TAX_SIGNATURES)),
    **dict(zip(g._NEUTRAL_ADJUSTMENT_DESCRIPTIONS, h.NEUTRAL_ADJUSTMENT_DESCRIPTIONS)),
}

_GROUPED = {"{party}": r"(?P<party>[A-Z ]+)", "{ref}": r"(?P<ref>[A-Z0-9]+)", "{method}": r"(?P<method>[A-Z]+)"}


def _grouped_regex(template: str) -> re.Pattern[str]:
    return re.compile(
        "".join(_GROUPED.get(part, re.escape(part)) for part in g._PLACEHOLDER_SPLIT.split(template))
    )


# Same order as `generator.narration.ALL_TEMPLATES` — load-bearing, since a
# `{ref}`-carrying template must be tried before the otherwise-identical shape
# that drops it.
_ORDERED = tuple((t, _grouped_regex(t)) for t in g.ALL_TEMPLATES)


def rewrite_narration(narration: str) -> str:
    """`narration` re-rendered from the held-out pool, slots preserved."""
    for template, regex in _ORDERED:
        match = regex.fullmatch(narration)
        if match is None:
            continue
        slots = match.groupdict()
        replacement = TEMPLATE_MAP[template]
        if "party" in slots and slots["party"] is not None:
            party = slots["party"]
            slots["party"] = PARTY_MAP.get(party, party)
        return replacement.format(**{k: v for k, v in slots.items() if v is not None})
    raise ValueError(f"narration {narration!r} was not written from the shared pool")


def _rewrite_jsonl(src: Path, dst: Path, field: str, mapper) -> int:
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = 0
    for row in rows:
        value = row.get(field)
        if value:
            new = mapper(value)
            if new != value:
                changed += 1
            row[field] = new
    dst.write_text(
        "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return changed


def main(src_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    banks = _rewrite_jsonl(src_dir / "bank_lines.jsonl", out_dir / "bank_lines.jsonl", "narration", rewrite_narration)
    ledger = _rewrite_jsonl(
        src_dir / "ledger_entries.jsonl", out_dir / "ledger_entries.jsonl", "narration", rewrite_narration
    )
    recon = _rewrite_jsonl(
        src_dir / "recon_lines.jsonl",
        out_dir / "recon_lines.jsonl",
        "description",
        lambda d: DESCRIPTION_MAP.get(d, d),
    )
    # Untouched: no free text, and the answer key must not move.
    for name in ("settlements.jsonl", "ground_truth.jsonl"):
        shutil.copyfile(src_dir / name, out_dir / name)

    print(f"bank_lines narrations rewritten:    {banks}")
    print(f"ledger_entries narrations rewritten:{ledger}")
    print(f"recon_lines descriptions rewritten: {recon}")
    print(f"settlements / ground_truth:         copied verbatim")
    print(f"-> {out_dir}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
