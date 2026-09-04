"""CLI entry point for bank-profile inference (`pipeline/adapters/inference.py`).

Deliberately a standalone `tools/` script rather than a `reconcile` subcommand.
`pipeline/cli.py` is the *graded* path — `reconcile run`, the thing the
reproduce checkpoint invokes and the thing every metric is measured through
— and profile inference is not part of a reconciliation run. It is an onboarding
step performed once, by a human, before a new bank's export is ever reconciled: the
artifact it produces is a YAML file for review, and the reviewed file is what the
graded path later consumes via `profiles.load_profile`. Keeping the two apart is the
same separation `tools/build_adversarial_set.py` and `tools/measure_performance.py`
already observe, and it keeps a model-authored artifact structurally outside the
measured pipeline until a human moves it there.

Usage (offline replay of the committed cache — the demo path, and the default):

    uv run python tools/infer_bank_profile.py data/unseen_bank/kotak_statement.csv

Usage (a real Fireworks call; the only mode that needs `FIREWORKS_API_KEY`):

    uv run python tools/infer_bank_profile.py data/unseen_bank/kotak_statement.csv --refresh

Exit status is 0 for an accepted profile and 1 for a clean give-up, so the loop's
own termination condition is what the shell sees — a give-up is a legible negative
result, not a crash and not a silently-empty success.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.adapters.inference import (  # noqa: E402
    DEFAULT_MAX_ATTEMPTS,
    InferenceResult,
    infer_bank_profile,
)
from pipeline.llm_cache import CacheMode, PromptCache  # noqa: E402
from pipeline.schemas import BankProfile  # noqa: E402

ADAPTER_CACHE_PATH = REPO_ROOT / "data" / "adapter_cache.json"
"""Separate from `data/llm_cache.json` (Slots A and B) on purpose. The two caches
are keyed identically and could share a file without collision — the key is a
SHA-256 of the whole prompt — but they are refreshed by different operators at
different times for different reasons, and one committed file that two workflows
both rewrite is a merge conflict waiting to happen for no gain."""


def _load_env_api_key() -> str:
    """Read `FIREWORKS_API_KEY` out of the repo's `.env`, stripping surrounding quotes.

    The repo does not auto-load `.env` into the process environment (nothing in
    `pipeline/` may depend on a dotfile being present — offline mode is the
    default), so the one mode that genuinely needs a credential reads it explicitly,
    exactly as `scratch/populate_slot_b_cache.py` does. Falls through to the
    environment variable when there is no `.env`, so CI can supply it the usual way.
    """
    import os

    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.strip().startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "FIREWORKS_API_KEY":
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                return value
    from_environ = os.environ.get("FIREWORKS_API_KEY")
    if from_environ:
        return from_environ
    raise RuntimeError("FIREWORKS_API_KEY not found in .env or the environment")


def _report(result: InferenceResult, *, out: Path | None) -> int:
    for attempt in result.attempts:
        origin = "cache" if attempt.from_cache else "fireworks"
        print(f"\n--- attempt {attempt.index} ({origin}) ---")
        payload = attempt.proposal.to_payload()
        print(f"  date_format: {payload['date_format']!r}")
        print(f"  header:      {payload['header']}")
        for key in (
            "value_date_column",
            "transaction_date_column",
            "narration_column",
            "ref_no_column",
            "withdrawal_column",
            "deposit_column",
            "balance_column",
        ):
            print(f"  {key + ':':<26}{payload[key]!r}")
        print(f"  verification: {attempt.verification.summary()}")

    if not result.accepted:
        print(f"\nGAVE UP: {result.give_up_reason}")
        return 1

    print(f"\nACCEPTED on attempt {result.accepted_on_attempt}, by deterministic verification.")
    print("\n" + (result.profile_yaml or ""))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.profile_yaml or "", encoding="utf-8")
        print(f"written to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("statement", type=Path, help="CSV or XLSX bank export with no hand-written profile")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="call Fireworks on a cache miss (needs FIREWORKS_API_KEY); default is strict offline replay",
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument(
        "--as-profile",
        default=BankProfile.HDFC.value,
        choices=[profile.value for profile in BankProfile],
        help=(
            "the BankProfile tag the inferred profile carries. Provenance only, never "
            "inferred: the enum is closed and the model is not asked which bank this is."
        ),
    )
    parser.add_argument("--cache", type=Path, default=ADAPTER_CACHE_PATH)
    parser.add_argument("--out", type=Path, default=None, help="write the accepted YAML here")
    args = parser.parse_args(argv)

    mode = CacheMode.REFRESH if args.refresh else CacheMode.STRICT
    client = None
    if mode is CacheMode.REFRESH:
        from pipeline.llm_client import FireworksClient

        client = FireworksClient(api_key=_load_env_api_key())

    cache = PromptCache(args.cache)
    print(f"statement:  {args.statement}")
    print(f"cache:      {args.cache} ({len(cache)} entries), mode={mode.value}")

    result = infer_bank_profile(
        args.statement,
        cache,
        mode=mode,
        client=client,
        max_attempts=args.max_attempts,
        bank_profile=BankProfile(args.as_profile),
    )
    return _report(result, out=args.out)


if __name__ == "__main__":
    raise SystemExit(main())
