"""Tests for the test-first decomposition flow.

Verifies that ``build_integration_test_prompt`` produces a usable
Phase 1.5 prompt and that ``build_file_generation_prompt`` injects
the already-written integration tests as the contract when they're
present.
"""
from __future__ import annotations

from validator.decomposer import (
    build_file_generation_prompt,
    build_integration_test_prompt,
)


def _plan_with_two_subtasks() -> dict:
    return {
        "subtasks": [
            {"subtask_id": "scorer",
             "description": "score a guess against a target",
             "stub_files": ["wordle/scorer.py"],
             "stub_test_files": ["tests/test_scorer.py"],
             "dependencies": [],
             "complexity_weight": 0.4},
            {"subtask_id": "game",
             "description": "game state machine",
             "stub_files": ["wordle/game.py"],
             "stub_test_files": ["tests/test_game.py"],
             "dependencies": ["scorer"],
             "complexity_weight": 0.6},
        ],
        "shared_files": {
            "wordle/types.py": "class Feedback: ...\n",
        },
        "integration_test_files": {},
    }


# ---- Phase 1.5 prompt ----

def test_integration_prompt_lists_integration_files(tmp_path):
    prompt = build_integration_test_prompt(
        _plan_with_two_subtasks(), str(tmp_path),
        "a tiny wordle clone",
        language="python",
    )
    assert "INTEGRATION TESTS" in prompt
    # Default integration test filename for python should appear.
    assert "tests/test_integration.py" in prompt
    # The prompt should expose the subtask layout so the model can write
    # tests against it.
    assert "scorer" in prompt
    assert "game" in prompt
    # And the shared types so the model can use them.
    assert "wordle/types.py" in prompt


def test_integration_prompt_switches_language(tmp_path):
    prompt = build_integration_test_prompt(
        _plan_with_two_subtasks(), str(tmp_path),
        "wordle in C++",
        language="cpp",
    )
    assert "C++17" in prompt
    assert "tests/test_integration.cpp" in prompt
    assert "tests/test_integration.py" not in prompt


def test_integration_prompt_warns_about_signature_consistency(tmp_path):
    prompt = build_integration_test_prompt(
        _plan_with_two_subtasks(), str(tmp_path),
        "demo",
        language="python",
    )
    # The key insight: ONE signature shape per type across tests.
    lowered = prompt.lower()
    assert "consistent" in lowered or "source of truth" in lowered


# ---- Phase 2 prompt with test-first contract ----

def test_phase2_prompt_includes_contract_when_integration_tests_present(tmp_path):
    plan = _plan_with_two_subtasks()
    plan["integration_test_files"] = {
        "tests/test_integration.py": (
            "from wordle.game import Game\n"
            "def test_x():\n"
            "    g = Game('ALLOY')\n"   # the pinned constructor signature
            "    assert g\n"
        ),
    }
    prompt = build_file_generation_prompt(
        plan, str(tmp_path), "demo", language="python",
    )
    # Contract section appears.
    assert "Integration tests (ALREADY WRITTEN" in prompt
    # The specific signature appears inline so Phase 2 has it.
    assert "g = Game('ALLOY')" in prompt
    # And the source-of-truth language is there.
    assert "source of truth" in prompt.lower() or "contract" in prompt.lower()


def test_phase2_prompt_omits_contract_when_no_integration_tests(tmp_path):
    plan = _plan_with_two_subtasks()
    # No pre-written integration tests = old behaviour, no contract block.
    plan["integration_test_files"] = {}
    prompt = build_file_generation_prompt(
        plan, str(tmp_path), "demo", language="python",
    )
    assert "Integration tests (ALREADY WRITTEN" not in prompt


def test_phase2_prompt_uses_profile_idiom(tmp_path):
    plan = _plan_with_two_subtasks()
    prompt_py = build_file_generation_prompt(
        plan, str(tmp_path), "demo", language="python",
    )
    prompt_rs = build_file_generation_prompt(
        plan, str(tmp_path), "demo", language="rust",
    )
    assert "NotImplementedError" in prompt_py
    assert "unimplemented!" in prompt_rs
    assert "NotImplementedError" not in prompt_rs
