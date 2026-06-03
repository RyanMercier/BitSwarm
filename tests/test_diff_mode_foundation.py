"""
Tests for the diff-mode coordinator foundation:
- Phase 1 / Phase 2 prompt builders construct sensible prompts
- The diff-mode validator catches the structural failure modes it
  is supposed to catch

These tests do NOT exercise an LLM. They verify the wiring; the
behaviour of the actual coordinator runs lives in end-to-end demos.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from validator.diff_prompts import (
    DIFF_COORDINATOR_SYSTEM_PROMPT,
    build_diff_phase1_prompt,
    build_diff_phase2_prompt,
)
from validator.diff_validator import validate_diff_decomposition


# ---- Fixtures ----------------------------------------------------------

@pytest.fixture
def tiny_repo(tmp_path):
    """A minimal Python repo with two source files and one test file."""
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "__init__.py").write_text("")
    (tmp_path / "calc" / "ops.py").write_text(
        "def add(a, b):\n    return a + b\n\n"
        "def sub(a, b):\n    return a - b\n"
    )
    (tmp_path / "calc" / "main.py").write_text(
        "from calc.ops import add\n\n"
        "def run():\n    return add(2, 3)\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ops.py").write_text(
        "from calc.ops import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n"
    )
    return str(tmp_path)


# ---- Prompt builder tests ----------------------------------------------

def test_phase1_prompt_includes_change_spec_and_file_tree(tiny_repo):
    prompt = build_diff_phase1_prompt(
        repo_path=tiny_repo,
        change_spec="Add a multiply() function to calc.ops and use it in calc.main.run()",
    )
    assert "Add a multiply()" in prompt
    assert "calc/ops.py" in prompt
    assert "calc/main.py" in prompt
    assert '"mode": "diff"' in prompt
    assert "modify_files" in prompt
    assert "target_stubs" in prompt


def test_phase1_prompt_includes_existing_file_contents(tiny_repo):
    prompt = build_diff_phase1_prompt(
        repo_path=tiny_repo,
        change_spec="Add multiply()",
    )
    assert "def add(a, b)" in prompt
    assert "def sub(a, b)" in prompt
    assert "=== calc/ops.py ===" in prompt


def test_phase1_prompt_truncates_content_on_large_repos(tmp_path):
    """When the repo exceeds the inline-content budget, the prompt
    must NOT embed every file's content (that's what caused the
    'Prompt is too long' crash on real OSS targets). It still
    includes the file tree and explicitly-referenced files."""
    # Build a repo with 60 files each ~5KB ~> 300KB total, far above
    # the 80KB budget. Mention only one file by name in the spec.
    src = tmp_path / "bigpkg"
    src.mkdir()
    (src / "__init__.py").write_text("")
    for i in range(60):
        (src / f"mod_{i:02d}.py").write_text(
            "# filler\n" + "\n".join(f"def fn_{j}(): pass" for j in range(200))
        )

    prompt = build_diff_phase1_prompt(
        repo_path=str(tmp_path),
        change_spec="Modify bigpkg/mod_05.py to add a new helper.",
    )

    # File tree is always present
    assert "bigpkg" in prompt
    assert "mod_05.py" in prompt
    # The mentioned file should be pre-loaded
    assert "=== bigpkg/mod_05.py ===" in prompt
    # An UN-mentioned file's content should NOT be embedded
    assert "=== bigpkg/mod_42.py ===" not in prompt
    # The inclusion-note explains the truncation
    assert "large" in prompt.lower()
    assert "pre-loaded" in prompt.lower()


def test_phase1_prompt_carries_previous_errors(tiny_repo):
    prompt = build_diff_phase1_prompt(
        repo_path=tiny_repo,
        change_spec="x",
        previous_errors=["complexity_weight values sum to 0.8, expected 1.0."],
    )
    assert "VALIDATION ERRORS" in prompt
    assert "sum to 0.8" in prompt


def test_phase2_prompt_lists_target_stubs_and_new_tests(tiny_repo):
    decomp = {
        "mode": "diff",
        "subtasks": [
            {
                "subtask_id": "add_multiply",
                "description": "Add multiply() to ops",
                "modify_files": ["calc/ops.py", "calc/main.py"],
                "new_test_files": ["tests/test_multiply.py"],
                "behavior_spec": "Add a multiply function; main.run() returns 2*3.",
                "dependencies": [],
                "complexity_weight": 1.0,
            }
        ],
    }
    prompt = build_diff_phase2_prompt(
        decomposition=decomp,
        repo_path=tiny_repo,
        change_spec="Add multiply()",
    )
    assert "calc/ops.py" in prompt
    assert "calc/main.py" in prompt
    assert "tests/test_multiply.py" in prompt
    # Existing-file content should be pre-loaded so the model writes
    # stubs that respect what was there.
    assert "def add(a, b)" in prompt
    assert "EXISTING (to be modified): calc/ops.py" in prompt


def test_system_prompt_has_required_sections():
    p = DIFF_COORDINATOR_SYSTEM_PROMPT
    assert "Diff-Mode Coordinator" in p
    assert "target-state stub" in p.lower()
    assert "modify_files" in p
    assert "new_test_files" in p
    assert "complexity weights sum to 1.0" in p.lower()


# ---- Validator tests ---------------------------------------------------

def _good_decomp(repo_path):
    return {
        "mode": "diff",
        "subtasks": [
            {
                "subtask_id": "add_multiply",
                "description": "Add multiply() to ops",
                "modify_files": ["calc/ops.py"],
                "new_test_files": ["tests/test_multiply.py"],
                "behavior_spec": "Add multiply.",
                "dependencies": [],
                "complexity_weight": 1.0,
            }
        ],
        "target_stubs": {
            "calc/ops.py": "def add(a, b):\n    ...\ndef multiply(a, b):\n    ...",
        },
        "new_test_files": {
            "tests/test_multiply.py": "def test_multiply(): ...",
        },
        "shared_additions": {},
        "integration_test_files": {},
        "requirements_additions": [],
    }


def test_validator_accepts_good_decomposition(tiny_repo):
    errors = validate_diff_decomposition(_good_decomp(tiny_repo), tiny_repo)
    assert errors == []


def test_validator_rejects_wrong_mode(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["mode"] = "scaffold"
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("mode='diff'" in e for e in errors)


def test_validator_rejects_nonexistent_modify_file(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"][0]["modify_files"] = ["calc/nonexistent.py"]
    d["target_stubs"] = {"calc/nonexistent.py": "..."}
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("does not exist" in e and "nonexistent.py" in e for e in errors)


def test_validator_rejects_overlapping_modify_files(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"] = [
        {
            "subtask_id": "a",
            "description": "x",
            "modify_files": ["calc/ops.py"],
            "new_test_files": ["tests/test_a.py"],
            "behavior_spec": "x",
            "dependencies": [],
            "complexity_weight": 0.5,
        },
        {
            "subtask_id": "b",
            "description": "y",
            "modify_files": ["calc/ops.py"],  # collides with a
            "new_test_files": ["tests/test_b.py"],
            "behavior_spec": "y",
            "dependencies": [],
            "complexity_weight": 0.5,
        },
    ]
    d["target_stubs"] = {"calc/ops.py": "..."}
    d["new_test_files"] = {
        "tests/test_a.py": "...",
        "tests/test_b.py": "...",
    }
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("appears in both subtask" in e for e in errors)


def test_validator_rejects_missing_target_stub(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["target_stubs"] = {}  # missing stub for calc/ops.py
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("no entry in target_stubs" in e for e in errors)


def test_validator_rejects_new_test_file_collision(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"][0]["new_test_files"] = ["tests/test_ops.py"]  # exists
    d["new_test_files"] = {"tests/test_ops.py": "..."}
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("already exists" in e and "test_ops.py" in e for e in errors)


def test_validator_rejects_missing_new_test_content(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"][0]["new_test_files"] = ["tests/test_missing.py"]
    d["new_test_files"] = {}  # no content for declared test
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("no content" in e and "test_missing.py" in e for e in errors)


def test_validator_rejects_weight_sum_mismatch(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"][0]["complexity_weight"] = 0.5
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("sum to 0.500" in e for e in errors)


def test_validator_rejects_unknown_dependency(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"][0]["dependencies"] = ["nonexistent_subtask"]
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("nonexistent_subtask" in e for e in errors)


def test_validator_rejects_dependency_cycle(tiny_repo):
    d = _good_decomp(tiny_repo)
    (open(os.path.join(tiny_repo, "calc", "main.py"), "r"))
    d["subtasks"] = [
        {
            "subtask_id": "a",
            "description": "x",
            "modify_files": ["calc/ops.py"],
            "new_test_files": ["tests/test_a.py"],
            "behavior_spec": "x",
            "dependencies": ["b"],
            "complexity_weight": 0.5,
        },
        {
            "subtask_id": "b",
            "description": "y",
            "modify_files": ["calc/main.py"],
            "new_test_files": ["tests/test_b.py"],
            "behavior_spec": "y",
            "dependencies": ["a"],  # cycle: a -> b -> a
            "complexity_weight": 0.5,
        },
    ]
    d["target_stubs"] = {"calc/ops.py": "...", "calc/main.py": "..."}
    d["new_test_files"] = {"tests/test_a.py": "...", "tests/test_b.py": "..."}
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("cycle" in e.lower() for e in errors)


def test_validator_rejects_empty_subtasks(tiny_repo):
    d = _good_decomp(tiny_repo)
    d["subtasks"] = []
    errors = validate_diff_decomposition(d, tiny_repo)
    assert any("no subtasks" in e for e in errors)
