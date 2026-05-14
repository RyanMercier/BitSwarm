"""
Smoke test the coordinator under the Claude Code backend.

Calls ``validator.decomposer.decompose`` against a tiny spec and prints
the resulting plan. Confirms Phase 1 (subtask list) and Phase 2 (stub
file contents) both come back parseable, then runs the validator's
Phase 1.5 checks against the result.

Run:
    COORDINATOR_BACKEND=claude_code python demo/smoke_coordinator_cc.py

Cost: $0 (uses your Claude subscription via the CLI).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("COORDINATOR_BACKEND", "claude_code")

from validator.decomposer import decompose  # noqa: E402
from validator.validator_checks import validate_decomposition  # noqa: E402


SPEC = (
    "Build a tiny Python `calc` package with two operations:\n"
    "1) `from calc.adder import add` -> `add(a, b)` returns a + b\n"
    "2) `from calc.multiplier import multiply` -> `multiply(a, b)` returns a * b\n"
    "\n"
    "Decompose into exactly TWO subtasks, one per operation. Each subtask\n"
    "owns its own module and test file. Use complexity_weight=0.5 each so\n"
    "the weights sum to 1.0. No external dependencies; pytest is already\n"
    "in requirements.txt.\n"
)


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


def _make_target_repo(repo_path: str) -> None:
    """Lay down an empty Python target with just requirements.txt."""
    os.makedirs(repo_path, exist_ok=True)
    with open(os.path.join(repo_path, "requirements.txt"), "w") as f:
        f.write("pytest\n")
    with open(os.path.join(repo_path, "README.md"), "w") as f:
        f.write("# tiny calc demo target\n")
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial target repo"],
        cwd=repo_path, env=GIT_ENV, check=True,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bitswarm_coord_cc_") as workspace:
        repo_path = os.path.join(workspace, "target")
        _make_target_repo(repo_path)
        print(f"[smoke] target repo at {repo_path}")

        debug = os.path.join(workspace, "debug")

        decomp = decompose(
            repo_path=repo_path,
            feature_spec=SPEC,
            validate_fn=validate_decomposition,
            debug_dir=debug,
        )

        if decomp is None:
            print("[smoke] FAIL: decompose returned None after retries")
            return 1

        print()
        print("=== decomposition ===")
        subs = decomp.get("subtasks", [])
        print(f"  subtasks:        {len(subs)}")
        for st in subs:
            print(f"    - {st['subtask_id']:18s} "
                  f"weight={st.get('complexity_weight', '?')} "
                  f"stubs={st.get('stub_files', [])}")
        print(f"  shared_files:    {len(decomp.get('shared_files', {}))}")
        print(f"  stub_files:      {len(decomp.get('stub_files', {}))}")
        print(f"  stub_test_files: {len(decomp.get('stub_test_files', {}))}")
        print(f"  integration:     {len(decomp.get('integration_test_files', {}))}")

        # Sanity checks
        if len(subs) == 0:
            print("[smoke] FAIL: no subtasks")
            return 1
        if not decomp.get("stub_files"):
            print("[smoke] FAIL: Phase 2 produced no stub_files")
            return 1
        if not decomp.get("stub_test_files"):
            print("[smoke] FAIL: Phase 2 produced no stub_test_files")
            return 1

        print()
        print("[smoke] SUCCESS")
        # Persist the decomposition for inspection
        out = os.path.join(workspace, "decomposition.json")
        with open(out, "w") as f:
            json.dump(decomp, f, indent=2)
        print(f"  decomposition saved to {out}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
