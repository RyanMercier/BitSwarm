"""
Regression tests for bugs the multi-language audit caught.

Each test names the specific fix it pins so future refactors can't
silently undo the change.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest


# ---- Fix 1: validator_checks.py disk-walk used a leaked `path` var ----

def test_disk_walk_passes_relative_path_not_outer_loop_var(tmp_path):
    """Regression for the leaked-variable bug: the on-disk walk inside
    Check 3b previously called ``python_parser.extract_imports(tree,
    content, path)`` where ``path`` was either undefined (when
    ``stub_test_files`` was empty) or the wrong file's path. This test
    exercises the path: a scaffolded package + a stub-tests-empty
    decomposition + an existing repo file that imports from the
    scaffolded package."""
    from validator import validator_checks as vc

    # Build a repo with a Python file that imports from a scaffolded
    # package.
    repo = str(tmp_path)
    existing_dir = os.path.join(repo, "raytracer")
    os.makedirs(existing_dir)
    with open(os.path.join(existing_dir, "__init__.py"), "w") as f:
        f.write("")
    # main.py imports from raytracer.scene, which will be created by
    # scaffolding.
    with open(os.path.join(repo, "main.py"), "w") as f:
        f.write("from raytracer.scene import Scene\n")

    decomp = {
        "subtasks": [
            {"subtask_id": "scene",
             "stub_files": ["raytracer/scene.py"],
             "stub_test_files": [],  # intentionally empty (triggered NameError)
             "dependencies": [],
             "complexity_weight": 1.0},
        ],
        "shared_files": {},
        "stub_files": {
            "raytracer/scene.py": (
                "class Scene:\n"
                "    def __init__(self):\n"
                "        raise NotImplementedError\n"
            ),
        },
        "stub_test_files": {},
        "integration_test_files": {},
        "requirements_additions": [],
    }
    errors = vc.validate_decomposition(decomp, repo)
    # The decomposition is well-formed; main.py's import resolves
    # against the scaffolded raytracer.scene. The bug would crash with
    # ``NameError: name 'path' is not defined`` before producing any
    # error list at all.
    assert "subtask 'scene' has no stub test files".lower() in " ".join(
        e.lower() for e in errors
    ) or any("no stub test files" in e for e in errors)


# ---- Fix 2: _constructor_for `new` no longer fires for non-Rust classes ----

def test_python_class_with_method_named_new_does_not_get_arity_checked_as_ctor():
    """A Python class can legitimately define a method called ``new``
    that isn't its constructor. The cross-file arity check must not
    treat it as one."""
    from validator.parsers.python import parser as py
    from validator.validator_checks_common import (
        FileFacts, check_interface_contracts,
    )

    widget_src = (
        "class Widget:\n"
        "    def new(self, host, port):\n"  # 2 required args
        "        return 'fresh'\n"
    )
    user_src = (
        "from pkg.widget import Widget\n"
        "w = Widget()\n"  # 0 args  -  would mismatch `new` if the check fired
    )

    def facts(path, src):
        tree = py.parse(src, path)
        return FileFacts(
            path=path,
            module=py.module_path_for_file(path),
            language=py.name,
            imports=py.extract_imports(tree, src, path),
            defined_names=py.extract_defined_names(tree, src),
            call_sites=py.extract_call_sites(tree, src),
        )

    errors = check_interface_contracts([
        facts("pkg/widget.py", widget_src),
        facts("pkg/user.py", user_src),
    ])
    # Widget has no __init__ / constructor / Widget method / params, so
    # _constructor_for returns None and no arity error is raised.
    assert not any("Arity mismatch" in e for e in errors), errors


# ---- Fix 7: Python posonlyargs counted in arity ----

def test_python_posonly_args_count_toward_arity():
    """``def f(a, /, b)`` has 2 required args. The old parser missed
    position-only params entirely (since they live on
    ``args.posonlyargs``, not ``args.args``)."""
    from validator.parsers.python import parser as py
    src = "def f(a, /, b, c=3):\n    return a + b + c\n"
    tree = py.parse(src, "x.py")
    info = py.extract_defined_names(tree, src)["f"]
    assert [p.name for p in info.params] == ["a", "b", "c"]
    assert info.required_arg_count == 2
    assert info.max_arg_count == 3


# ---- Fix 5+6: makedirs("") + scaffolder requirements isfile ----

def test_scaffolder_works_when_repo_has_no_requirements_txt(tmp_path):
    """Older scaffolder crashed reading a non-existent requirements.txt
    when the decomposition added new pip deps."""
    from validator import scaffolder

    repo = str(tmp_path)
    decomp = {
        "shared_files": {},
        "stub_files": {"top.py": "x = 1\n"},  # top-level filename, no dirname
        "stub_test_files": {},
        "integration_test_files": {},
        "requirements_additions": ["some-new-pkg"],
    }
    # If write_scaffolding raises FileNotFoundError reading
    # requirements.txt, the test fails. (We also need a git repo for
    # the commit step.)
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], capture_output=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], capture_output=True)

    # We don't actually run pip install in tests; patch it out.
    with patch("validator.scaffolder.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
        scaffolder.write_scaffolding(decomp, repo)

    # The new requirements.txt should exist with the addition.
    req = os.path.join(repo, "requirements.txt")
    assert os.path.isfile(req)
    with open(req) as f:
        assert "some-new-pkg" in f.read()


# ---- Fix 11: Makefile ``test:`` detection ignores recipe lines ----

def test_makefile_test_target_ignores_recipe_lines(tmp_path):
    """A Makefile recipe like ``\\techo test:`` should not be mistaken
    for a top-level ``test:`` target."""
    from validator.test_runners import _makefile_has_test_target

    mk = os.path.join(tmp_path, "Makefile")
    with open(mk, "w") as f:
        f.write("all:\n\techo test:\n\techo done\n")
    assert _makefile_has_test_target(str(tmp_path)) is False


def test_makefile_test_target_recognizes_real_target(tmp_path):
    from validator.test_runners import _makefile_has_test_target

    mk = os.path.join(tmp_path, "Makefile")
    with open(mk, "w") as f:
        f.write("test: build\n\tpytest\n")
    assert _makefile_has_test_target(str(tmp_path)) is True


def test_makefile_test_assignment_is_not_a_target(tmp_path):
    """``test := value`` is a variable assignment, not a target."""
    from validator.test_runners import _makefile_has_test_target

    mk = os.path.join(tmp_path, "Makefile")
    with open(mk, "w") as f:
        f.write("test := unit\nall:\n\techo $(test)\n")
    assert _makefile_has_test_target(str(tmp_path)) is False


# ---- Fix 12: test_runners.detect_runner survives a missing repo dir ----

def test_detect_runner_does_not_crash_on_missing_repo(tmp_path):
    from validator.test_runners import detect_runner
    missing = str(tmp_path / "does_not_exist")
    spec = detect_runner(missing)
    # Falls through to pytest (the default).
    assert spec.name == "pytest"
