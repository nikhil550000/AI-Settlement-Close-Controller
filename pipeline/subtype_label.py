"""`SubtypeLabel`, Slot A's eight-value output vocabulary (§4.2).

Split out from `pipeline/classifier.py` (session 5.1) so `pipeline/apply.py`
can reference the type on `CaseOutcome` without an import cycle:
`pipeline/classifier.py` imports `pipeline.apply.CaseOutcome` (component 5
runs downstream of component 8 — see `classifier.py`'s module docstring),
so `apply.py` cannot import `SubtypeLabel` back from `classifier.py`. Both
modules import it from here instead.
"""

from __future__ import annotations

from enum import StrEnum


class SubtypeLabel(StrEnum):
    """Slot A's eight-value output vocabulary (§4.2): the seven `OPERATIONAL_EXCEPTION`
    subtypes plus `AMBIGUOUS_CASE`.

    Deliberately its own type rather than `pipeline.ground_truth.ExceptionSubtype`
    reused whole: that enum carries ten members, including `NONE`, `OMISSION` and
    `MISPOSTING`, none of which Slot A may ever emit under constrained decoding.
    String values match `ExceptionSubtype`'s for the seven shared members, so a
    fired trigger converts to a label with no translation table.
    """

    SETTLEMENT_UTR_MISSING = "SETTLEMENT_UTR_MISSING"
    BANK_CREDIT_OVERDUE = "BANK_CREDIT_OVERDUE"
    SETTLEMENT_AMOUNT_MISMATCH = "SETTLEMENT_AMOUNT_MISMATCH"
    UNMATCHED_INBOUND_CREDIT = "UNMATCHED_INBOUND_CREDIT"
    REVERSAL_UNMATCHED = "REVERSAL_UNMATCHED"
    DUPLICATE_CREDIT = "DUPLICATE_CREDIT"
    DISPUTE_PENDING = "DISPUTE_PENDING"
    AMBIGUOUS_CASE = "AMBIGUOUS_CASE"
