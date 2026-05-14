"""
Phase A regression tests for validator_checks.

The Python AST logic was lifted out of ``validator/validator_checks.py``
and into ``validator/parsers/python.py`` and
``validator/validator_checks_common.py``. These tests pin the
externally-observable behavior so Phase A is provably a no-op for
Python.

Two layers of coverage:

1. Legacy helpers (``extract_imports``, ``resolves``,
   ``extract_defined_names``) still return the POC shapes so any
   external caller that imported them keeps working.
2. ``validate_decomposition`` produces the same error wording for the
   four failure modes the POC validator was responsible for: unresolved
   import, missing imported name, wrong constructor arity, stub test
   that passes (no-op test).
"""
from __future__ import annotations

import os
import tempfile

import pytest

from validator import validator_checks as vc
from validator.parsers import detect
from validator.parsers.python import parser as python_parser
from validator.parsers.types import CallableInfo, ImportInfo
from validator.validator_checks_common import (
    FileFacts,
    check_interface_contracts,
    check_no_circular_deps,
)


# ---- Legacy helper compatibility ----

def test_extract_imports_legacy_shape():
    src = (
        "import os\n"
        "from collections import OrderedDict\n"
        "from mypkg.sub import Thing, Other\n"
    )
    mods = vc.extract_imports(src)
    assert mods == ["os", "collections", "mypkg.sub"]


def test_extract_imports_handles_syntax_error():
    assert vc.extract_imports("def broken(:::") == []


def test_resolves_stdlib_and_requirements():
    with tempfile.TemporaryDirectory() as repo:
        assert vc.resolves("os", repo, {}, {}, [])
        assert vc.resolves("collections.abc", repo, {}, {}, [])
        assert vc.resolves("flask", repo, {}, {}, ["flask"])
        assert not vc.resolves("totally_made_up", repo, {}, {}, [])


def test_resolves_scaffolded_files():
    with tempfile.TemporaryDirectory() as repo:
        shared = {"mypkg/types.py": "X = 1\n"}
        stubs = {"mypkg/widget.py": "def f(): ...\n"}
        assert vc.resolves("mypkg.types", repo, shared, stubs, [])
        assert vc.resolves("mypkg.widget", repo, shared, stubs, [])
        assert not vc.resolves("mypkg.missing", repo, shared, stubs, [])


def test_extract_defined_names_legacy_shape():
    src = (
        "X = 1\n"
        "def f(a, b=2): pass\n"
        "class C:\n"
        "    def __init__(self, x, y=3):\n"
        "        pass\n"
        "    def go(self, *args, **kw):\n"
        "        pass\n"
    )
    names = vc.extract_defined_names(src)
    assert names["X"] == {"kind": "variable"}
    assert names["f"]["kind"] == "function"
    assert names["f"]["min_args"] == 1
    assert names["f"]["max_args"] == 2
    assert names["f"]["arg_names"] == ["a", "b"]

    cls = names["C"]
    assert cls["kind"] == "class"
    init = cls["methods"]["__init__"]
    assert init["min_args"] == 1
    assert init["max_args"] == 2
    assert init["arg_names"] == ["x", "y"]
    assert init["has_varargs"] is False
    assert init["has_kwargs"] is False

    go = cls["methods"]["go"]
    assert go["has_varargs"] is True
    assert go["has_kwargs"] is True


# ---- Parser registry ----

def test_parser_registry_detects_python():
    p = detect("foo/bar/baz.py")
    assert p is not None
    assert p.name == "python"


def test_parser_registry_returns_none_for_unknown():
    assert detect("foo.unknown") is None


# ---- Common contract checks ----

def _build_facts(files: dict[str, str]) -> list[FileFacts]:
    facts: list[FileFacts] = []
    for path, content in files.items():
        tree = python_parser.parse(content, path)
        facts.append(FileFacts(
            path=path,
            module=python_parser.module_path_for_file(path),
            language=python_parser.name,
            imports=python_parser.extract_imports(tree, content),
            defined_names=python_parser.extract_defined_names(tree, content),
            call_sites=python_parser.extract_call_sites(tree, content),
        ))
    return facts


def test_check_interface_contracts_missing_imported_name():
    facts = _build_facts({
        "pkg/widget.py": "class Widget:\n    def __init__(self, name):\n        pass\n",
        "pkg/uses.py": "from pkg.widget import Gadget\n",  # wrong name
    })
    errors = check_interface_contracts(facts)
    assert any("imports 'Gadget' from 'pkg.widget'" in e for e in errors)


def test_check_interface_contracts_wrong_arity():
    facts = _build_facts({
        "pkg/widget.py": "class Widget:\n    def __init__(self, host, port):\n        pass\n",
        "pkg/uses.py": (
            "from pkg.widget import Widget\n"
            "w = Widget(1)\n"  # missing port
        ),
    })
    errors = check_interface_contracts(facts)
    assert any("Arity mismatch" in e and "Widget" in e for e in errors)


def test_check_interface_contracts_arity_with_default():
    """A class with a defaulted param should accept both arities."""
    facts = _build_facts({
        "pkg/w.py": "class W:\n    def __init__(self, x, y=1):\n        pass\n",
        "pkg/u.py": (
            "from pkg.w import W\n"
            "a = W(1)\n"
            "b = W(1, 2)\n"
        ),
    })
    errors = check_interface_contracts(facts)
    assert errors == []


def test_check_interface_contracts_varargs_skip():
    facts = _build_facts({
        "pkg/w.py": "class W:\n    def __init__(self, *args):\n        pass\n",
        "pkg/u.py": (
            "from pkg.w import W\n"
            "x = W(1, 2, 3, 4, 5)\n"
        ),
    })
    assert check_interface_contracts(facts) == []


def test_check_no_circular_deps_detects_cycle():
    subtasks = [
        {"subtask_id": "a", "dependencies": ["b"]},
        {"subtask_id": "b", "dependencies": ["c"]},
        {"subtask_id": "c", "dependencies": ["a"]},
    ]
    errors = check_no_circular_deps(subtasks)
    assert len(errors) == 1
    assert "Circular dependency" in errors[0]


def test_check_no_circular_deps_clean():
    subtasks = [
        {"subtask_id": "a", "dependencies": []},
        {"subtask_id": "b", "dependencies": ["a"]},
        {"subtask_id": "c", "dependencies": ["a", "b"]},
    ]
    assert check_no_circular_deps(subtasks) == []


# ---- validate_decomposition end-to-end ----

def _minimal_decomposition(**overrides) -> dict:
    """A self-consistent two-subtask decomposition that should validate clean."""
    decomp = {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["pkg/widget.py"],
             "stub_test_files": ["tests/test_widget.py"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["pkg/user.py"],
             "stub_test_files": ["tests/test_user.py"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {
            "pkg/types.py": "class Color:\n    RED = 'red'\n",
        },
        "stub_files": {
            "pkg/widget.py": (
                "from pkg.types import Color\n"
                "class Widget:\n"
                "    def __init__(self, color):\n"
                "        raise NotImplementedError('Widget.__init__ not implemented')\n"
                "    def render(self):\n"
                "        raise NotImplementedError('Widget.render not implemented')\n"
            ),
            "pkg/user.py": (
                "from pkg.widget import Widget\n"
                "def use_widget(color):\n"
                "    w = Widget(color)\n"
                "    raise NotImplementedError('use_widget not implemented')\n"
            ),
        },
        "stub_test_files": {
            "tests/test_widget.py": (
                "from pkg.widget import Widget\n"
                "from pkg.types import Color\n"
                "def test_widget_renders():\n"
                "    w = Widget(Color.RED)\n"
                "    assert w.render() == 'rendered'\n"
            ),
            "tests/test_user.py": (
                "from pkg.user import use_widget\n"
                "from pkg.types import Color\n"
                "def test_use_widget():\n"
                "    assert use_widget(Color.RED) == 'ok'\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }
    decomp.update(overrides)
    return decomp


def _make_repo_with_requirements(tmpdir: str) -> str:
    """Create a minimal repo with a requirements.txt."""
    repo = os.path.join(tmpdir, "repo")
    os.makedirs(repo)
    with open(os.path.join(repo, "requirements.txt"), "w") as f:
        f.write("pytest\n")
    return repo


def test_validate_decomposition_clean(tmp_path):
    """A well-formed Python decomposition validates without errors (modulo
    the stub-runner step which is best-effort and not asserted here)."""
    repo = _make_repo_with_requirements(str(tmp_path))
    decomp = _minimal_decomposition()
    errors = vc.validate_decomposition(decomp, repo)
    # The structural checks (1-8) must produce no errors.
    # Check 9 (stub-test-must-fail) is exercised in its own test.
    structural = [e for e in errors if "Stub test" not in e and "Error running stub test" not in e]
    assert structural == [], structural


def test_validate_decomposition_catches_unresolved_import(tmp_path):
    repo = _make_repo_with_requirements(str(tmp_path))
    decomp = _minimal_decomposition()
    decomp["stub_files"]["pkg/widget.py"] = (
        "from pkg.does_not_exist import Color\n"
        "class Widget:\n"
        "    def __init__(self, color):\n"
        "        raise NotImplementedError\n"
        "    def render(self):\n"
        "        raise NotImplementedError\n"
    )
    errors = vc.validate_decomposition(decomp, repo)
    assert any("Unresolved import" in e and "pkg.does_not_exist" in e for e in errors)


def test_validate_decomposition_catches_missing_imported_name(tmp_path):
    repo = _make_repo_with_requirements(str(tmp_path))
    decomp = _minimal_decomposition()
    # types.py only exports Color, but widget tries to import Theme
    decomp["stub_files"]["pkg/widget.py"] = (
        "from pkg.types import Theme\n"
        "class Widget:\n"
        "    def __init__(self, theme):\n"
        "        raise NotImplementedError\n"
    )
    errors = vc.validate_decomposition(decomp, repo)
    assert any("Interface mismatch" in e and "'Theme'" in e for e in errors)


def test_validate_decomposition_catches_arity_mismatch(tmp_path):
    repo = _make_repo_with_requirements(str(tmp_path))
    decomp = _minimal_decomposition()
    # Widget needs (color), but user calls Widget(color, extra)
    decomp["stub_files"]["pkg/user.py"] = (
        "from pkg.widget import Widget\n"
        "def use_widget(color):\n"
        "    w = Widget(color, 'extra')\n"
        "    raise NotImplementedError\n"
    )
    errors = vc.validate_decomposition(decomp, repo)
    assert any("Arity mismatch" in e and "Widget" in e for e in errors)


def test_validate_decomposition_weights_sum():
    with tempfile.TemporaryDirectory() as repo:
        decomp = _minimal_decomposition()
        decomp["subtasks"][0]["complexity_weight"] = 0.3
        decomp["subtasks"][1]["complexity_weight"] = 0.3
        errors = vc.validate_decomposition(decomp, repo)
        assert any("Complexity weights sum" in e for e in errors)


def test_validate_decomposition_path_overlap():
    with tempfile.TemporaryDirectory() as repo:
        decomp = _minimal_decomposition()
        decomp["subtasks"][1]["stub_files"] = ["pkg/widget.py"]  # same as subtask 0
        decomp["stub_files"]["pkg/user.py"] = decomp["stub_files"]["pkg/widget.py"]
        errors = vc.validate_decomposition(decomp, repo)
        assert any("File path overlap" in e for e in errors)


def test_validate_decomposition_circular_deps():
    with tempfile.TemporaryDirectory() as repo:
        decomp = _minimal_decomposition()
        decomp["subtasks"][0]["dependencies"] = ["user"]
        # widget depends on user, user depends on widget -> cycle
        errors = vc.validate_decomposition(decomp, repo)
        assert any("Circular dependency" in e for e in errors)


def test_validate_decomposition_missing_stub_content():
    with tempfile.TemporaryDirectory() as repo:
        decomp = _minimal_decomposition()
        # Subtask lists pkg/widget.py but it's missing from stub_files
        del decomp["stub_files"]["pkg/widget.py"]
        errors = vc.validate_decomposition(decomp, repo)
        assert any("its content is missing" in e and "pkg/widget.py" in e for e in errors)


def test_validate_decomposition_stub_tests_must_fail(tmp_path):
    """Phase 1.5 check 9: a stub test that passes is a no-op test and a real bug."""
    repo = _make_repo_with_requirements(str(tmp_path))
    decomp = _minimal_decomposition()
    # Replace test_widget.py with a no-op that doesn't call the stub
    decomp["stub_test_files"]["tests/test_widget.py"] = (
        "def test_widget_noop():\n"
        "    assert 1 == 1\n"
    )
    decomp["stub_test_files"]["tests/test_user.py"] = (
        "def test_user_noop():\n"
        "    assert True\n"
    )
    errors = vc.validate_decomposition(decomp, repo)
    assert any("PASSED on scaffolding" in e for e in errors), errors
