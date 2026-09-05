"""The Phase 7 checkpoint, verbatim:

> A clean clone, one documented command, metrics byte-identical to the
> committed JSON.

`tests/test_cli.py` already proves two runs against the *same working
tree* agree byte-for-byte; that is reproducibility, not byte-identical reproduction. Byte-identical reproduction is a claim
about a **different checkout** entirely — no `.venv`, no `uv.lock`
resolution already done, none of this process's own Python import cache —
reproducing the exact bytes committed at `data/metrics.json`. Every other
offline mode/checkpoint test in this codebase discharges its claim against a
real artifact rather than a stub (`tests/test_report.py`'s own docstring:
"the checkpoint is discharged... build the report from a real run"); this
is that same discipline applied to "clean clone," which cannot be faked by
running twice in the process that is already running.

**What this test actually does**, end to end, with no shortcuts: `git
clone --local` this repository into a temp directory (a real second
checkout, sharing no state with this one but its `.git` objects), `uv
sync` a fresh virtual environment there from the committed `uv.lock`, run
`uv run reconcile` — the one documented command — with
`FIREWORKS_API_KEY` stripped from the subprocess environment so a Slot
A/Slot B cache miss cannot silently reach the network, and diff the
resulting `metrics.json` against the one committed at `data/metrics.json`
byte for byte.

Slow relative to the rest of the suite (a real clone, a real venv sync, a
real subprocess) — this is the one test in the repository for which that
is the point.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PINNED_METRICS_PATH = REPO_ROOT / "data" / "metrics.json"

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None or shutil.which("git") is None,
    reason="clean-clone reproduce test needs both `git` and `uv` on PATH",
)


def _run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )


@pytest.fixture(scope="module")
def clean_clone(tmp_path_factory) -> Path:
    """A real second checkout of `HEAD`, sharing nothing at all with the tree
    pytest is already running from.

    `--no-hardlinks` is load-bearing. `git clone --local` hardlinks the object
    store by default, and a hardlink cannot cross a volume: on a CI runner whose
    checkout is on one drive and whose temp directory is on another, the clone
    fails outright with "Improper link". Copying the objects also makes the
    isolation total rather than nearly total, which is what this module claims.
    """
    clone_dir = tmp_path_factory.mktemp("clean_clone") / "repo"
    result = _run(
        ["git", "clone", "--local", "--no-hardlinks", "--quiet", str(REPO_ROOT), str(clone_dir)],
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"git clone failed:\n{result.stderr}"
    return clone_dir


def test_pinned_metrics_json_exists_and_names_a_seed_and_a_sha():
    """The pinned run: "generator seed, git SHA, and the metrics JSON produced by
    that run" MUST be pinned and committed — read here as the precondition
    for the rest of this module, not as a fact this test discovers."""
    assert PINNED_METRICS_PATH.exists(), (
        "data/metrics.json is not committed — the pinned run has not been run yet"
    )
    pinned = json.loads(PINNED_METRICS_PATH.read_text(encoding="utf-8"))
    provenance = pinned["provenance"]
    assert provenance["seed"] is not None
    assert provenance["git_sha"] is not None
    assert provenance["model_id"] is not None


def test_clean_clone_syncs_and_reproduces_the_committed_metrics_json_byte_identically(
    clean_clone, tmp_path
):
    sync = _run(["uv", "sync", "--quiet"], cwd=clean_clone)
    assert sync.returncode == 0, f"uv sync failed in the clean clone:\n{sync.stderr}"

    pinned_text = PINNED_METRICS_PATH.read_text(encoding="utf-8")
    provenance = json.loads(pinned_text)["provenance"]

    out_dir = tmp_path / "reproduced"
    # No PYTHONIOENCODING override here on purpose: `pipeline/cli.py`'s own
    # module-level stream reconfigure (added this session) is what has to
    # make the console summary encode on Windows' cp1252 default, not an
    # environment variable a caller has to know to set first.
    env = {k: v for k, v in os.environ.items() if k != "FIREWORKS_API_KEY"}
    run = _run(
        [
            "uv",
            "run",
            "reconcile",
            "--seed",
            str(provenance["seed"]),
            "--git-sha",
            provenance["git_sha"],
            "--out-dir",
            str(out_dir),
        ],
        cwd=clean_clone,
        env=env,
    )
    assert run.returncode == 0, (
        f"`uv run reconcile` failed in the clean clone (no FIREWORKS_API_KEY in "
        f"its environment; the default --llm-cache=strict must never need one):\n"
        f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
    )

    reproduced_text = (out_dir / "metrics.json").read_text(encoding="utf-8")
    assert reproduced_text == pinned_text, (
        "the clean clone's reconcile run did not reproduce data/metrics.json "
        "byte-identically — byte-identical reproduction does not hold"
    )


def test_clean_clone_reconcile_never_touches_the_network_by_default(clean_clone, tmp_path):
    """The same run, checked from the other direction: strict mode with no API
    key present must never even attempt a Fireworks call. A `CacheMissError`
    surfacing as a clean, named failure (rather than a socket timeout) is
    itself evidence of this — `pipeline.llm_cache.CacheMode.STRICT`'s own
    docstring: "a miss is a hard error rather than a fallthrough to the API.\""""
    env = {k: v for k, v in os.environ.items() if k != "FIREWORKS_API_KEY"}
    out_dir = tmp_path / "network_check"
    run = _run(
        ["uv", "run", "reconcile", "--out-dir", str(out_dir)],
        cwd=clean_clone,
        env=env,
    )
    assert run.returncode == 0, run.stderr
    assert "FIREWORKS_API_KEY" not in run.stderr
