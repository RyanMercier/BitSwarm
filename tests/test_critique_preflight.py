"""Tests for the self-critique pass and pre-flight checks."""
from __future__ import annotations

import os

import pytest

from validator import critique, preflight


# ---- Critique ----

def test_critique_parses_issue_lines():
    text = """\
I reviewed the decomposition and found:

ISSUE: tests/test_game.cpp constructs Game(words, "hello") but
       wordle/game.hpp declares Game(const std::string& target).
ISSUE: tests/test_words.cpp imports a Dictionary type but no
       stub declares it.

That's it.
"""
    issues = critique._parse_issues(text)
    assert len(issues) == 2
    assert "Game(words" in issues[0]
    assert "Dictionary" in issues[1]


def test_critique_handles_ok_response():
    issues = critique._parse_issues("OK: no interface drift detected")
    assert issues == []


def test_critique_disable_env_var(monkeypatch):
    monkeypatch.setenv("BITSWARM_SKIP_CRITIQUE", "1")
    # The disabled path returns [] without invoking the backend.
    result = critique.critique({"stub_files": {"x.py": "..."}})
    assert result == []


def test_build_critique_prompt_includes_all_sections():
    decomp = {
        "shared_files": {"types.py": "X = 1"},
        "stub_files":  {"a.py": "def f(): ..."},
        "stub_test_files": {"tests/test_a.py": "from a import f"},
        "integration_test_files": {"tests/test_integration.py": "..."},
    }
    prompt = critique.build_critique_prompt(decomp)
    assert "Shared types / headers" in prompt
    assert "Stub files" in prompt
    assert "Stub test files" in prompt
    assert "Integration test files" in prompt
    assert "types.py" in prompt
    assert "a.py" in prompt


def test_build_critique_prompt_truncates_long_files():
    big = "x" * 20000
    decomp = {"stub_files": {"big.py": big}}
    prompt = critique.build_critique_prompt(decomp)
    assert "truncated" in prompt
    assert len(prompt) < len(big) + 2000  # well below the raw size


# ---- Pre-flight ----

def test_preflight_python_clean_repo(tmp_path):
    """A trivial scaffolded repo whose modules all import cleanly
    should return no errors."""
    # Lay down a tiny "scaffolded" Python package.
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "thing.py").write_text("def thing():\n    raise NotImplementedError\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_integration.py").write_text(
        "from demo.thing import thing\n"
        "def test_x():\n    pass\n"
    )

    decomp = {
        "stub_files": {"demo/thing.py": "..."},
        "integration_test_files": {"tests/test_integration.py": "..."},
    }
    errs = preflight.preflight(decomp, str(tmp_path), language="python")
    assert errs == [], errs


def test_preflight_python_catches_syntax_error(tmp_path):
    """Phase 2 emitting a syntax error should be caught here."""
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "broken.py").write_text("def broken(:\n  pass\n")   # syntax error
    decomp = {"stub_files": {"demo/broken.py": "..."}}
    errs = preflight.preflight(decomp, str(tmp_path), language="python")
    assert errs, "should have surfaced a SyntaxError"
    assert "Pre-flight (Python imports) failed" in errs[0]


def test_preflight_python_catches_missing_import(tmp_path):
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "needs_missing.py").write_text("from demo.absent import Thing\n")
    decomp = {"stub_files": {"demo/needs_missing.py": "..."}}
    errs = preflight.preflight(decomp, str(tmp_path), language="python")
    assert errs


def test_preflight_disable_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_SKIP_PREFLIGHT", "1")
    # Even with a broken repo, disabled preflight returns no errors.
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "broken.py").write_text("def broken(:")
    decomp = {"stub_files": {"demo/broken.py": "..."}}
    assert preflight.preflight(decomp, str(tmp_path), language="python") == []


def test_preflight_java_csharp_are_skipped(tmp_path):
    # Heavy build systems intentionally skipped; should report no
    # errors (not raise).
    decomp = {"stub_files": {"X.java": "..."}}
    assert preflight.preflight(decomp, str(tmp_path), language="java") == []
    assert preflight.preflight(decomp, str(tmp_path), language="csharp") == []


def test_preflight_falls_back_to_python_for_unknown(tmp_path):
    """Unknown language string should fall back to the Python profile
    (via lang_profiles.profile_for), which then runs the python check."""
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "ok.py").write_text("x = 1\n")
    decomp = {"stub_files": {"demo/ok.py": "..."}}
    errs = preflight.preflight(decomp, str(tmp_path), language="klingon")
    assert errs == []
