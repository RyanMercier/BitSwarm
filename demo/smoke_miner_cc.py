"""
End-to-end smoke test for the Claude Code miner backend.

What this covers:
  - We hand-write a "scaffolded" repo with a single subtask (stub + test).
  - We git-init it and commit as ``BitSwarm scaffolding`` so the
    miner's ``_generate_patch`` has a baseline to diff against.
  - We call ``miner.agent_cc.execute_subtask`` directly, which spawns
    ``claude -p`` in the workspace and lets it write the implementation.
  - We assert the tests pass and the generated patch is non-empty.

What this does NOT cover:
  - The coordinator decomposition step (skipped on purpose — that would
    cost API tokens).
  - The HTTP miner/validator servers (a follow-up smoke once this
    direct path works).

Run:
    MINER_BACKEND=claude_code python demo/smoke_miner_cc.py

Cost: $0 — the Claude Code subprocess uses your Claude subscription's
bundled inference, not metered API tokens. (The first run may also
trigger a one-time auth flow if you haven't logged in via
``claude auth login`` yet.)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

# Make the project root importable.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("MINER_BACKEND", "claude_code")

from miner.agent_cc import execute_subtask  # noqa: E402


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


# A single, trivial subtask: implement add(a, b).
SHARED_FILES = {}

STUB_FILES = {
    "calc/__init__.py": "",
    "calc/add.py": (
        '"""Tiny adder."""\n'
        '\n'
        'def add(a, b):\n'
        '    """Return a + b. Stub: raises NotImplementedError."""\n'
        '    raise NotImplementedError("calc.add.add not implemented")\n'
    ),
}

TEST_FILES = {
    "tests/__init__.py": "",
    "tests/test_add.py": (
        'from calc.add import add\n'
        '\n'
        'def test_add_positives():\n'
        '    assert add(2, 3) == 5\n'
        '\n'
        'def test_add_negatives():\n'
        '    assert add(-1, -1) == -2\n'
        '\n'
        'def test_add_zero():\n'
        '    assert add(0, 7) == 7\n'
    ),
}

SUBTASK = {
    "subtask_id": "add",
    "description": "Implement an addition function in calc/add.py.",
    "stub_files": [p for p in STUB_FILES if not p.endswith("__init__.py")],
    # Only real test files (not the __init__.py marker) belong here.
    "stub_test_files": [p for p in TEST_FILES if not p.endswith("__init__.py")],
    "allowed_files": list(STUB_FILES.keys()) + list(TEST_FILES.keys()),
    "complexity_weight": 1.0,
    "dependencies": [],
}


def _build_scaffolded_repo(repo_path: str) -> None:
    """Lay down files, git init, commit as the scaffolding baseline."""
    os.makedirs(repo_path, exist_ok=True)
    for path, content in {**STUB_FILES, **TEST_FILES}.items():
        full = os.path.join(repo_path, path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    # requirements.txt — pytest only.
    with open(os.path.join(repo_path, "requirements.txt"), "w") as f:
        f.write("pytest\n")
    subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "BitSwarm scaffolding"],
        cwd=repo_path, env=GIT_ENV, check=True,
    )


async def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bitswarm_cc_smoke_") as workspace:
        repo_path = os.path.join(workspace, "miner_repo")
        _build_scaffolded_repo(repo_path)
        print(f"[smoke] scaffolded repo at {repo_path}")

        # Stub tests should FAIL on the baseline (NotImplementedError).
        # If they don't, the test was a no-op.
        baseline = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_add.py", "-q"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if baseline.returncode == 0:
            print("[smoke] BASELINE TESTS PASSED — stub is a no-op; aborting.")
            return 2
        print(f"[smoke] baseline rc={baseline.returncode} (expected non-zero) -- OK")

        result = await execute_subtask(
            subtask=SUBTASK,
            repo_path=repo_path,
            all_subtask_files={SUBTASK["subtask_id"]: SUBTASK["allowed_files"]},
            shared_files=SHARED_FILES,
            shared_files_content=SHARED_FILES,
            stub_files_content=STUB_FILES,
            test_files_content=TEST_FILES,
            all_subtasks=[SUBTASK],
        )

        print()
        print(f"[smoke] tests_passed = {result.tests_passed}")
        print(f"[smoke] stop_reason  = {result.stop_reason}")
        print(f"[smoke] patch length = {len(result.patch)} chars")
        print(f"[smoke] files modified = {result.files_modified}")
        print()
        if result.patch:
            print("--- patch (first 40 lines) ---")
            print("\n".join(result.patch.splitlines()[:40]))
            print("--- end patch ---")
        else:
            print("[smoke] (no patch generated)")

        if not result.tests_passed:
            print()
            print("--- test output (last 30 lines) ---")
            print("\n".join((result.test_output or "").splitlines()[-30:]))
            print("--- end test output ---")
            return 1
        if not result.patch.strip():
            print("[smoke] FAIL: tests passed but patch is empty (suspicious)")
            return 1
        print("[smoke] SUCCESS")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
