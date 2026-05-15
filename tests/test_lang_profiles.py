"""
Tests for the per-language coordinator profile registry +
``build_file_generation_prompt`` integration.

The registry is what makes BitSwarm's coordinator multi-language. A
regression here means the Phase 2 prompt will silently drift back to
Python defaults for non-Python projects.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from validator.lang_profiles import (
    LanguageProfile,
    all_profiles,
    profile_for,
)


# ---- Registry shape ----

def test_seven_languages_registered():
    names = {p.name for p in all_profiles()}
    assert names == {"python", "typescript", "java", "csharp", "c", "cpp", "rust"}


def test_each_profile_has_required_fields():
    for p in all_profiles():
        assert p.name, p
        assert p.display_name, p
        assert p.extensions, p
        assert p.integration_test_filename, p
        assert p.phase2_intro, p
        assert p.stub_rules, p
        assert p.test_rules, p
        assert p.integration_rules, p
        assert p.test_command_hint, p


def test_extensions_are_unique_per_language():
    """Each extension should point at one profile. ``.h`` is the
    deliberate exception (shared between C and C++; per-file
    disambiguation happens in ``parsers/__init__.py:detect``)."""
    seen: dict[str, str] = {}
    for p in all_profiles():
        for ext in p.extensions:
            if ext == ".h":
                continue  # disambiguated at detect-time
            if ext in seen:
                raise AssertionError(
                    f"Extension {ext} claimed by both {seen[ext]} and {p.name}"
                )
            seen[ext] = p.name


# ---- Resolution: explicit, alias, env, auto-detect, fallback ----

def test_explicit_canonical_name():
    assert profile_for("typescript").name == "typescript"
    assert profile_for("cpp").name == "cpp"
    assert profile_for("rust").name == "rust"


def test_aliases_resolve():
    assert profile_for("ts").name == "typescript"
    assert profile_for("c++").name == "cpp"
    assert profile_for("rs").name == "rust"
    assert profile_for("py").name == "python"
    assert profile_for("cs").name == "csharp"


def test_unknown_language_falls_back_to_python():
    assert profile_for("klingon").name == "python"


def test_env_var_resolves(monkeypatch):
    monkeypatch.setenv("COORDINATOR_LANGUAGE", "rust")
    assert profile_for().name == "rust"


def test_env_var_overridden_by_explicit_arg(monkeypatch):
    monkeypatch.setenv("COORDINATOR_LANGUAGE", "rust")
    # Explicit arg wins.
    assert profile_for("python").name == "python"


def test_auto_detect_from_cargo_toml(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    assert profile_for(repo_path=str(tmp_path)).name == "rust"


def test_auto_detect_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"name":"x"}')
    assert profile_for(repo_path=str(tmp_path)).name == "typescript"


def test_auto_detect_from_pom_xml(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert profile_for(repo_path=str(tmp_path)).name == "java"


def test_auto_detect_from_cmake(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("project(x)\n")
    assert profile_for(repo_path=str(tmp_path)).name == "cpp"


def test_auto_detect_from_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    assert profile_for(repo_path=str(tmp_path)).name == "python"


def test_auto_detect_falls_through_to_python_for_empty_repo(tmp_path):
    assert profile_for(repo_path=str(tmp_path)).name == "python"


def test_env_var_beats_repo_detection(tmp_path, monkeypatch):
    """Explicit env var wins over autodetection (so the user can
    deliberately decompose a non-default language inside a multi-
    language repo)."""
    (tmp_path / "Cargo.toml").write_text("[package]\nname = \"x\"\n")
    monkeypatch.setenv("COORDINATOR_LANGUAGE", "cpp")
    assert profile_for(repo_path=str(tmp_path)).name == "cpp"


# ---- Profile content sanity ----

@pytest.mark.parametrize("name,must_contain", [
    ("python",     "NotImplementedError"),
    ("typescript", 'Error("not implemented'),
    ("java",       "UnsupportedOperationException"),
    ("csharp",     "NotImplementedException"),
    ("c",          'assert(0 && "not implemented'),
    ("cpp",        'logic_error("not implemented'),
    ("rust",       "unimplemented!"),
])
def test_each_profile_has_its_stub_idiom(name, must_contain):
    p = profile_for(name)
    assert must_contain in p.stub_rules, (
        f"{name} profile should mention {must_contain!r} in stub_rules"
    )


@pytest.mark.parametrize("name,frag", [
    ("python",     "pytest"),
    ("typescript", "vitest"),
    ("java",       "JUnit"),
    ("csharp",     "xUnit"),
    ("c",          "<assert.h>"),
    ("cpp",        "<cassert>"),
    ("rust",       "#[test]"),
])
def test_each_profile_names_its_test_framework(name, frag):
    p = profile_for(name)
    assert frag in p.test_rules, (
        f"{name} profile should mention {frag!r} in test_rules"
    )


# ---- Integration with build_file_generation_prompt ----

def _tiny_decomp() -> dict:
    return {
        "subtasks": [
            {"subtask_id": "alpha",
             "stub_files": ["pkg/alpha.x"],
             "stub_test_files": ["tests/test_alpha.x"],
             "dependencies": [],
             "complexity_weight": 1.0},
        ],
        "shared_files": {},
        "integration_test_files": {},
    }


def test_prompt_picks_python_by_default(tmp_path):
    from validator.decomposer import build_file_generation_prompt
    prompt = build_file_generation_prompt(_tiny_decomp(), str(tmp_path), "demo spec")
    assert "Python" in prompt
    assert "NotImplementedError" in prompt
    assert "pytest" in prompt


def test_prompt_switches_on_explicit_language(tmp_path):
    from validator.decomposer import build_file_generation_prompt
    prompt = build_file_generation_prompt(
        _tiny_decomp(), str(tmp_path), "demo spec", language="rust",
    )
    assert "Rust" in prompt
    assert "unimplemented!" in prompt
    assert "cargo test" in prompt
    # And the Python rules shouldn't be in the resulting prompt.
    assert "NotImplementedError" not in prompt


def test_prompt_switches_on_repo_autodetect(tmp_path):
    from validator.decomposer import build_file_generation_prompt
    (tmp_path / "package.json").write_text('{"name":"x"}')
    prompt = build_file_generation_prompt(_tiny_decomp(), str(tmp_path), "demo")
    assert "TypeScript" in prompt
    assert "vitest" in prompt


def test_prompt_uses_profile_integration_filename(tmp_path):
    """When Phase 1 leaves ``integration_test_files`` empty, Phase 2's
    prompt should suggest the profile's per-language filename rather
    than the Python default."""
    from validator.decomposer import build_file_generation_prompt
    decomp = _tiny_decomp()
    # cpp profile -> tests/test_integration.cpp default.
    prompt = build_file_generation_prompt(
        decomp, str(tmp_path), "demo", language="cpp",
    )
    assert "tests/test_integration.cpp" in prompt
    assert "tests/test_integration.py" not in prompt
