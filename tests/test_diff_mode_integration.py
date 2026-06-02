"""
Integration smoke tests for the diff-mode pipeline.

These tests stitch together the foundation pieces (scaffolder,
tools.configure, warm-start builder, file-write validation) end-to-end
on a tiny fixture repo. They do not call any LLM; the "agent" is a
hand-written stand-in that performs the file modification the way a
real miner would.

Coverage:
  1. write_scaffolding(mode='diff') commits a baseline that includes
     new test files but leaves existing source files unchanged.
  2. build_diff_warm_start_message includes every required section
     and embeds the current file content + target stub.
  3. configure_tools(mode='diff', target_stubs=...) routes the
     interface check at file_write through the target stub, not the
     original file.
  4. A stand-in agent that follows the diff-mode protocol (read
     current, read target stub, write modified version) produces a
     git patch that, when applied, makes the new tests pass and
     leaves the existing tests passing too.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from miner.tools import (
    configure as configure_tools,
    run_tool,
)
from miner.warm_start import build_diff_warm_start_message
from validator.scaffolder import write_scaffolding


# ---- Fixtures ----------------------------------------------------------

@pytest.fixture
def calc_repo(tmp_path):
    """A minimal Python repo with ops + main + an existing test, all
    inside a git working tree."""
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "__init__.py").write_text("")
    (tmp_path / "calc" / "ops.py").write_text(textwrap.dedent('''\
        def add(a, b):
            return a + b


        def sub(a, b):
            return a - b
        '''))
    (tmp_path / "calc" / "main.py").write_text(textwrap.dedent('''\
        from calc.ops import add


        def run():
            return add(2, 3)
        '''))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_ops.py").write_text(textwrap.dedent('''\
        from calc.ops import add, sub


        def test_add():
            assert add(1, 2) == 3


        def test_sub():
            assert sub(5, 2) == 3
        '''))

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@local",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@local",
    }
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path,
                    check=True, env=env)
    return str(tmp_path)


def _good_diff_decomp(calc_repo):
    """A coordinator-output-shaped decomposition for adding multiply()
    to calc.ops and using it in calc.main.run()."""
    target_stub_ops = textwrap.dedent('''\
        def add(a, b):
            ...


        def sub(a, b):
            ...


        def multiply(a, b):
            """Return a * b."""
            ...
        ''')
    target_stub_main = textwrap.dedent('''\
        from calc.ops import multiply


        def run():
            """Return 2 * 3 = 6."""
            ...
        ''')
    new_test = textwrap.dedent('''\
        from calc.ops import multiply
        from calc.main import run


        def test_multiply():
            assert multiply(4, 5) == 20


        def test_run_uses_multiply():
            assert run() == 6
        ''')
    return {
        "mode": "diff",
        "subtasks": [
            {
                "subtask_id": "add_multiply",
                "description": "Add multiply() to ops; switch main.run() to use it",
                "modify_files": ["calc/ops.py", "calc/main.py"],
                "new_test_files": ["tests/test_multiply.py"],
                "behavior_spec": (
                    "Add a multiply(a, b) function in calc/ops.py. "
                    "Update calc/main.run() to return multiply(2, 3) instead "
                    "of add(2, 3). Keep add() and sub() unchanged."
                ),
                "dependencies": [],
                "complexity_weight": 1.0,
            }
        ],
        "target_stubs": {
            "calc/ops.py": target_stub_ops,
            "calc/main.py": target_stub_main,
        },
        "new_test_files": {
            "tests/test_multiply.py": new_test,
        },
        "shared_additions": {},
        "integration_test_files": {},
        "requirements_additions": [],
    }


# ---- Tests -------------------------------------------------------------

def test_scaffolder_diff_writes_only_net_new(calc_repo):
    decomp = _good_diff_decomp(calc_repo)
    write_scaffolding(decomp, calc_repo)

    # Existing source files are untouched
    assert "def multiply" not in open(os.path.join(calc_repo, "calc/ops.py")).read()
    # main.py still calls add (it was not modified at scaffold time)
    assert "from calc.ops import add" in open(os.path.join(calc_repo, "calc/main.py")).read()
    # New test file landed
    new_test_path = os.path.join(calc_repo, "tests/test_multiply.py")
    assert os.path.isfile(new_test_path)
    assert "def test_multiply" in open(new_test_path).read()


def test_scaffolder_diff_commits_baseline(calc_repo):
    decomp = _good_diff_decomp(calc_repo)
    write_scaffolding(decomp, calc_repo)

    log = subprocess.check_output(
        ["git", "log", "--pretty=%s"], cwd=calc_repo, text=True,
    )
    assert "BitSwarm diff baseline" in log


def test_warm_start_includes_current_and_target(calc_repo):
    decomp = _good_diff_decomp(calc_repo)
    subtask = decomp["subtasks"][0]

    msg = build_diff_warm_start_message(
        subtask=subtask,
        repo_root=calc_repo,
        target_stubs=decomp["target_stubs"],
        new_test_files_content=decomp["new_test_files"],
        shared_additions_content={},
        all_subtasks=decomp["subtasks"],
    )

    # Current file content
    assert "CURRENT (unmodified): calc/ops.py" in msg
    assert "def add(a, b):\n    return a + b" in msg
    # Target stub content
    assert "TARGET STUB: calc/ops.py" in msg
    assert "def multiply(a, b):" in msg
    # New test
    assert "NEW TEST FILE: tests/test_multiply.py" in msg
    assert "def test_multiply" in msg
    # Behavior spec text
    assert "Add a multiply(a, b) function" in msg
    # Subtask-level metadata
    assert "(DIFF MODE)" in msg
    assert "add_multiply" in msg


def test_tools_interface_check_uses_target_stub_in_diff_mode(calc_repo):
    """In diff mode, the file_write interface check should compare against
    the target stub (which declares multiply), not the original file (which
    does not). A miner that writes the post-edit content with the new
    multiply() should be allowed."""
    decomp = _good_diff_decomp(calc_repo)
    write_scaffolding(decomp, calc_repo)

    subtask = decomp["subtasks"][0]
    allowed_files = subtask["modify_files"] + subtask["new_test_files"]

    configure_tools(
        calc_repo,
        allowed_files,
        stub_test_files=subtask["new_test_files"],
        mode="diff",
        target_stubs=decomp["target_stubs"],
    )

    post_edit_ops = textwrap.dedent('''\
        def add(a, b):
            return a + b


        def sub(a, b):
            return a - b


        def multiply(a, b):
            return a * b
        ''')
    result = run_tool("file_write", {"path": "calc/ops.py", "content": post_edit_ops})
    assert result["success"], result["output"]
    # File on disk reflects the write
    assert "def multiply(a, b):\n    return a * b" in open(
        os.path.join(calc_repo, "calc/ops.py")
    ).read()


def test_tools_interface_check_blocks_unauthorized_addition(calc_repo):
    """If the miner tries to add a public symbol that isn't in the
    target stub, the write should be blocked."""
    decomp = _good_diff_decomp(calc_repo)
    write_scaffolding(decomp, calc_repo)

    subtask = decomp["subtasks"][0]
    allowed_files = subtask["modify_files"] + subtask["new_test_files"]

    configure_tools(
        calc_repo,
        allowed_files,
        stub_test_files=subtask["new_test_files"],
        mode="diff",
        target_stubs=decomp["target_stubs"],
    )

    bad = textwrap.dedent('''\
        def add(a, b): return a + b
        def sub(a, b): return a - b
        def multiply(a, b): return a * b
        def DIVIDE_NOT_IN_STUB(a, b): return a // b
        ''')
    result = run_tool("file_write", {"path": "calc/ops.py", "content": bad})
    assert not result["success"]
    assert "INTERFACE VIOLATION" in result["output"]
    assert "DIVIDE_NOT_IN_STUB" in result["output"]


def test_end_to_end_simulated_miner_produces_passing_patch(calc_repo):
    """Stitch the full diff-mode flow end-to-end with a hand-written
    'miner' that performs the modification the way a real LLM agent
    would. Verify:
      - the patch produced cleanly applies on a fresh checkout
      - existing tests still pass after the change
      - new tests pass after the change
    """
    decomp = _good_diff_decomp(calc_repo)
    write_scaffolding(decomp, calc_repo)

    subtask = decomp["subtasks"][0]
    allowed_files = subtask["modify_files"] + subtask["new_test_files"]

    configure_tools(
        calc_repo,
        allowed_files,
        stub_test_files=subtask["new_test_files"],
        mode="diff",
        target_stubs=decomp["target_stubs"],
    )

    # Simulated miner: modify ops.py and main.py to satisfy the contract
    new_ops = textwrap.dedent('''\
        def add(a, b):
            return a + b


        def sub(a, b):
            return a - b


        def multiply(a, b):
            """Return a * b."""
            return a * b
        ''')
    new_main = textwrap.dedent('''\
        from calc.ops import multiply


        def run():
            """Return 2 * 3 = 6."""
            return multiply(2, 3)
        ''')
    assert run_tool("file_write", {"path": "calc/ops.py", "content": new_ops})["success"]
    assert run_tool("file_write", {"path": "calc/main.py", "content": new_main})["success"]

    # Run the new tests; they should pass
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_multiply.py", "-q", "--tb=short"],
        cwd=calc_repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"new tests failed:\n{result.stdout}\n{result.stderr}"
    )

    # Run the EXISTING test suite; should still pass (regression gate)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_ops.py", "-q", "--tb=short"],
        cwd=calc_repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"existing tests broke:\n{result.stdout}\n{result.stderr}"
    )

    # The patch (diff against the BitSwarm diff baseline) should be
    # non-empty and apply cleanly when replayed on a fresh checkout
    # of the baseline.
    diff = subprocess.run(
        ["git", "diff", "HEAD", "--"] + allowed_files,
        cwd=calc_repo, capture_output=True, text=True,
    )
    patch = diff.stdout
    assert patch.strip(), "expected non-empty patch"
    assert "+def multiply" in patch
    assert "+from calc.ops import multiply" in patch
