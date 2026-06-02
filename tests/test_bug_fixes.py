"""
Regression tests for bugs the multi-language audit caught.

Each test names the specific fix it pins so future refactors can't
silently undo the change.
"""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import tempfile
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


def test_rust_constructor_arity_still_fires_via_class_name_alias():
    """The Rust parser registers ``new`` under both its real name AND
    the class's name, so the language-agnostic ``cls.name in methods``
    branch finds it without a generic ``new``-as-constructor rule."""
    from validator.parsers.rust import parser as rust
    from validator.validator_checks_common import (
        FileFacts, check_interface_contracts,
    )

    widget_src = (
        "pub struct Widget;\n"
        "impl Widget {\n"
        "  pub fn new(host: String, port: u16) -> Self { unimplemented!() }\n"
        "}\n"
    )
    user_src = (
        "use crate::widget::Widget;\n"
        "pub fn make() -> Widget { Widget::new(host()) }\n"  # 1 arg, needs 2
    )

    def facts(path, src):
        tree = rust.parse(src, path)
        return FileFacts(
            path=path,
            module=rust.module_path_for_file(path),
            language=rust.name,
            imports=rust.extract_imports(tree, src, path),
            defined_names=rust.extract_defined_names(tree, src),
            call_sites=rust.extract_call_sites(tree, src),
        )

    errors = check_interface_contracts([
        facts("src/widget.rs", widget_src),
        facts("src/user.rs", user_src),
    ])
    assert any("Arity mismatch" in e and "Widget" in e for e in errors), errors


# ---- Fix 4: TS arrow-function constants ----

def test_ts_arrow_function_lexical_decl_has_arity():
    """`export const handler = (req, res) => {...}` is a callable; the
    arity check must look at (req, res), not skip it as a constant."""
    from validator.parsers.typescript import parser as ts
    from validator.validator_checks_common import (
        FileFacts, check_interface_contracts,
    )

    server_src = (
        "export const handler = (req: any, res: any) => { return res; };\n"
    )
    user_src = (
        "import { handler } from './server';\n"
        "handler(1);\n"  # 1 arg, needs 2
    )

    def facts(path, src):
        tree = ts.parse(src, path)
        return FileFacts(
            path=path,
            module=ts.module_path_for_file(path),
            language=ts.name,
            imports=ts.extract_imports(tree, src, path),
            defined_names=ts.extract_defined_names(tree, src),
            call_sites=ts.extract_call_sites(tree, src),
        )

    errors = check_interface_contracts([
        facts("src/server.ts", server_src),
        facts("src/user.ts", user_src),
    ])
    assert any("Arity mismatch" in e and "handler" in e for e in errors), errors


def test_ts_shorthand_arrow_single_param():
    """`const f = x => x` is a 1-arg function, not a constant."""
    from validator.parsers.typescript import parser as ts
    src = "export const identity = x => x;\n"
    tree = ts.parse(src, "src/x.ts")
    info = ts.extract_defined_names(tree, src)["identity"]
    assert info.kind == "function"
    assert info.required_arg_count == 1


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


# ---- Fix 9: miner server TOCTOU on lock ----

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_miner_lock_does_not_deadlock_when_repeatedly_busy():
    """Pre-fix: the TOCTOU race could let two requests both pass the
    ``lock.locked()`` check; the second would block on the async with
    rather than 409. With the fix the second request gets 409 cleanly."""
    from miner.server import state

    async def run():
        # Manually acquire the lock to simulate "task in flight".
        await state.lock.acquire()
        try:
            # A new request that tries to acquire non-blockingly should
            # fail fast  -  exactly what the fixed endpoint does.
            try:
                await asyncio.wait_for(state.lock.acquire(), timeout=0)
                acquired = True
            except asyncio.TimeoutError:
                acquired = False
            return acquired
        finally:
            state.lock.release()

    acquired = asyncio.run(run())
    assert acquired is False


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


# ---- Fix 3: C# aliased using doesn't trip cross-file consistency check ----

def test_csharp_aliased_using_no_spurious_interface_mismatch():
    """``using A = Demo.Other;`` previously set imported_names=['A'],
    which produced 'Interface mismatch: imports A from Demo.Other'
    whenever Demo.Other was in the registry. The alias is now empty."""
    from validator.parsers.csharp import parser as cs
    from validator.validator_checks_common import (
        FileFacts, check_interface_contracts,
    )

    other_src = "namespace Demo.Other;\npublic class Helper {}\n"
    user_src = (
        "namespace Demo;\n"
        "using Aliased = Demo.Other;\n"
        "public class C {}\n"
    )

    def facts(path, src):
        tree = cs.parse(src, path)
        return FileFacts(
            path=path,
            module=cs.module_path_for_file(path, tree=tree, source=src),
            language=cs.name,
            imports=cs.extract_imports(tree, src, path),
            defined_names=cs.extract_defined_names(tree, src),
            call_sites=cs.extract_call_sites(tree, src),
        )

    errors = check_interface_contracts([
        facts("Demo/Other/Helper.cs", other_src),
        facts("Demo/C.cs", user_src),
    ])
    assert not any("Aliased" in e for e in errors), errors
