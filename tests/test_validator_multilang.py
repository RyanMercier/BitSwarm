"""
End-to-end multi-language validation.

Drives ``validator.validator_checks.validate_decomposition`` against
small TypeScript decompositions to confirm Phase 1.5 catches the same
class of errors it catches for Python: unresolved imports, missing
imported names, and constructor arity mismatches.

The TS check skips the stub-test-must-fail phase (Check 9) because the
fixtures don't ship a real package.json / vitest install. The
structural checks (1-8) are exercised directly.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

pytest.importorskip("tree_sitter_typescript")

from validator import validator_checks as vc


def _write_pkg_json(repo: str, deps: dict | None = None) -> None:
    pkg = {
        "name": "demo",
        "dependencies": deps or {},
        "devDependencies": {"vitest": "^1.0.0"},
        "scripts": {"test": "vitest run"},
    }
    with open(os.path.join(repo, "package.json"), "w") as f:
        json.dump(pkg, f)


def _ts_decomposition() -> dict:
    """A clean two-subtask TS decomposition with shared types."""
    return {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["src/widget.ts"],
             "stub_test_files": ["src/widget.test.ts"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["src/user.ts"],
             "stub_test_files": ["src/user.test.ts"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {
            "src/types.ts": (
                "export interface Color { name: string }\n"
                "export const RED: Color = { name: 'red' };\n"
            ),
        },
        "stub_files": {
            "src/widget.ts": (
                "import { Color } from './types';\n"
                "export class Widget {\n"
                "  constructor(color: Color, port: number) {\n"
                "    throw new Error('Widget not implemented');\n"
                "  }\n"
                "  render(): string {\n"
                "    throw new Error('Widget.render not implemented');\n"
                "  }\n"
                "}\n"
            ),
            "src/user.ts": (
                "import { Widget } from './widget';\n"
                "import { Color, RED } from './types';\n"
                "export function useWidget(color: Color): Widget {\n"
                "  const w = new Widget(color, 8080);\n"
                "  throw new Error('useWidget not implemented');\n"
                "}\n"
            ),
        },
        "stub_test_files": {
            "src/widget.test.ts": (
                "import { Widget } from './widget';\n"
                "import { RED } from './types';\n"
                "test('renders', () => {\n"
                "  const w = new Widget(RED, 8080);\n"
                "  expect(w.render()).toBe('ok');\n"
                "});\n"
            ),
            "src/user.test.ts": (
                "import { useWidget } from './user';\n"
                "import { RED } from './types';\n"
                "test('uses widget', () => {\n"
                "  expect(useWidget(RED)).toBeTruthy();\n"
                "});\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }


def _run_validate_no_runner(decomp: dict, repo: str) -> list[str]:
    """Run validate_decomposition but stop short of the stub-test runner.

    We monkeypatch ``verify_stub_tests_fail`` to return [], since the
    fixtures aren't a real npm project. All structural checks run.
    """
    original = vc.verify_stub_tests_fail
    try:
        vc.verify_stub_tests_fail = lambda *a, **k: []
        return vc.validate_decomposition(decomp, repo)
    finally:
        vc.verify_stub_tests_fail = original


def test_ts_decomposition_clean(tmp_path):
    repo = str(tmp_path)
    _write_pkg_json(repo)
    errors = _run_validate_no_runner(_ts_decomposition(), repo)
    assert errors == [], errors


def test_ts_unresolved_relative_import(tmp_path):
    repo = str(tmp_path)
    _write_pkg_json(repo)
    decomp = _ts_decomposition()
    decomp["stub_files"]["src/widget.ts"] = (
        "import { Color } from './does_not_exist';\n"
        "export class Widget {\n"
        "  constructor(color: Color, port: number) {}\n"
        "  render(): string { return ''; }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, repo)
    assert any("Unresolved import" in e and "does_not_exist" in e for e in errors), errors


def test_ts_missing_imported_name(tmp_path):
    """types.ts exports Color but widget tries to import Theme."""
    repo = str(tmp_path)
    _write_pkg_json(repo)
    decomp = _ts_decomposition()
    decomp["stub_files"]["src/widget.ts"] = (
        "import { Theme } from './types';\n"
        "export class Widget {\n"
        "  constructor(theme: Theme, port: number) {}\n"
        "  render(): string { return ''; }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, repo)
    assert any("Interface mismatch" in e and "'Theme'" in e for e in errors), errors


def test_ts_arity_mismatch(tmp_path):
    """Widget constructor takes (color, port), user calls Widget(color) only."""
    repo = str(tmp_path)
    _write_pkg_json(repo)
    decomp = _ts_decomposition()
    decomp["stub_files"]["src/user.ts"] = (
        "import { Widget } from './widget';\n"
        "import { Color } from './types';\n"
        "export function useWidget(color: Color): Widget {\n"
        "  const w = new Widget(color);\n"  # missing port arg
        "  throw new Error('useWidget not implemented');\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, repo)
    assert any("Arity mismatch" in e and "Widget" in e for e in errors), errors


def test_ts_bare_import_resolved_via_package_json(tmp_path):
    """A non-relative import resolves when it's listed in dependencies."""
    repo = str(tmp_path)
    _write_pkg_json(repo, deps={"express": "^4.0.0"})
    decomp = _ts_decomposition()
    decomp["stub_files"]["src/widget.ts"] = (
        "import express from 'express';\n"
        "import { Color } from './types';\n"
        "export class Widget {\n"
        "  constructor(color: Color, port: number) {}\n"
        "  render(): string { return ''; }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, repo)
    # express resolves through package.json; structural checks remain clean.
    assert errors == [], errors


def test_ts_bare_import_unresolved_when_missing_from_package_json(tmp_path):
    repo = str(tmp_path)
    _write_pkg_json(repo)  # no deps
    decomp = _ts_decomposition()
    decomp["stub_files"]["src/widget.ts"] = (
        "import lodash from 'lodash';\n"
        "import { Color } from './types';\n"
        "export class Widget {\n"
        "  constructor(color: Color, port: number) {}\n"
        "  render(): string { return ''; }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, repo)
    assert any("Unresolved import" in e and "lodash" in e for e in errors), errors


# ----- Java -----

pytest.importorskip("tree_sitter_java")


def _java_decomposition() -> dict:
    """A clean two-subtask Java decomposition with shared types."""
    return {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["src/main/java/com/example/Widget.java"],
             "stub_test_files": ["src/test/java/com/example/WidgetTest.java"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["src/main/java/com/example/User.java"],
             "stub_test_files": ["src/test/java/com/example/UserTest.java"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {
            "src/main/java/com/example/Color.java": (
                "package com.example;\n"
                "public class Color {\n"
                "  public static final String RED = \"red\";\n"
                "  public Color() {}\n"
                "}\n"
            ),
        },
        "stub_files": {
            "src/main/java/com/example/Widget.java": (
                "package com.example;\n"
                "public class Widget {\n"
                "  public Widget(Color color, int port) {\n"
                "    throw new UnsupportedOperationException(\"not implemented\");\n"
                "  }\n"
                "  public String render() {\n"
                "    throw new UnsupportedOperationException(\"not implemented\");\n"
                "  }\n"
                "}\n"
            ),
            "src/main/java/com/example/User.java": (
                "package com.example;\n"
                "public class User {\n"
                "  public Widget makeWidget(Color color) {\n"
                "    return new Widget(color, 8080);\n"
                "  }\n"
                "}\n"
            ),
        },
        "stub_test_files": {
            "src/test/java/com/example/WidgetTest.java": (
                "package com.example;\n"
                "public class WidgetTest {\n"
                "  public void testRender() {\n"
                "    Widget w = new Widget(new Color(), 8080);\n"
                "    if (!w.render().equals(\"ok\")) throw new RuntimeException();\n"
                "  }\n"
                "}\n"
            ),
            "src/test/java/com/example/UserTest.java": (
                "package com.example;\n"
                "public class UserTest {\n"
                "  public void testMake() {\n"
                "    User u = new User();\n"
                "    if (u.makeWidget(new Color()) == null) throw new RuntimeException();\n"
                "  }\n"
                "}\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }


def test_java_decomposition_clean(tmp_path):
    errors = _run_validate_no_runner(_java_decomposition(), str(tmp_path))
    assert errors == [], errors


def test_java_missing_imported_type(tmp_path):
    """User imports a Gadget that doesn't exist in com.example."""
    decomp = _java_decomposition()
    decomp["stub_files"]["src/main/java/com/example/User.java"] = (
        "package com.example;\n"
        "import com.example.Gadget;\n"  # Gadget is not defined anywhere
        "public class User {\n"
        "  public Gadget make() { return new Gadget(); }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Interface mismatch" in e and "'Gadget'" in e for e in errors), errors


def test_java_arity_mismatch(tmp_path):
    """Widget ctor needs (Color, int), user calls Widget(color) only."""
    decomp = _java_decomposition()
    decomp["stub_files"]["src/main/java/com/example/User.java"] = (
        "package com.example;\n"
        "public class User {\n"
        "  public Widget makeWidget(Color color) {\n"
        "    return new Widget(color);\n"  # missing port
        "  }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Arity mismatch" in e and "Widget" in e for e in errors), errors


def test_java_unresolved_subpackage(tmp_path):
    """Import refers to com.example.sub which we don't scaffold."""
    decomp = _java_decomposition()
    decomp["stub_files"]["src/main/java/com/example/User.java"] = (
        "package com.example;\n"
        "import com.example.sub.Thing;\n"  # subpackage doesn't exist
        "public class User {\n"
        "  public Thing get() { return null; }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Unresolved import" in e and "com.example.sub" in e for e in errors), errors


# ----- C# -----

pytest.importorskip("tree_sitter_c_sharp")


def _csharp_decomposition() -> dict:
    """A clean two-subtask C# decomposition with shared types."""
    return {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["Demo/Widget.cs"],
             "stub_test_files": ["Demo/WidgetTest.cs"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["Demo/User.cs"],
             "stub_test_files": ["Demo/UserTest.cs"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {
            "Demo/Color.cs": (
                "namespace Demo;\n"
                "public class Color {\n"
                "  public string Name { get; }\n"
                "  public Color(string name) { Name = name; }\n"
                "}\n"
            ),
        },
        "stub_files": {
            "Demo/Widget.cs": (
                "namespace Demo;\n"
                "public class Widget {\n"
                "  public Widget(Color color, int port) {\n"
                "    throw new System.NotImplementedException();\n"
                "  }\n"
                "  public string Render() {\n"
                "    throw new System.NotImplementedException();\n"
                "  }\n"
                "}\n"
            ),
            "Demo/User.cs": (
                "namespace Demo;\n"
                "public class User {\n"
                "  public Widget MakeWidget(Color color) {\n"
                "    return new Widget(color, 8080);\n"
                "  }\n"
                "}\n"
            ),
        },
        "stub_test_files": {
            "Demo/WidgetTest.cs": (
                "namespace Demo;\n"
                "public class WidgetTest {\n"
                "  public void TestRender() {\n"
                "    var w = new Widget(new Color(\"red\"), 8080);\n"
                "    if (w.Render() != \"ok\") throw new System.Exception();\n"
                "  }\n"
                "}\n"
            ),
            "Demo/UserTest.cs": (
                "namespace Demo;\n"
                "public class UserTest {\n"
                "  public void TestMake() {\n"
                "    var u = new User();\n"
                "    if (u.MakeWidget(new Color(\"red\")) == null) throw new System.Exception();\n"
                "  }\n"
                "}\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }


def test_csharp_decomposition_clean(tmp_path):
    errors = _run_validate_no_runner(_csharp_decomposition(), str(tmp_path))
    assert errors == [], errors


def test_csharp_arity_mismatch(tmp_path):
    """Color ctor needs (string), Widget tries Color() (no args)."""
    decomp = _csharp_decomposition()
    decomp["stub_files"]["Demo/User.cs"] = (
        "namespace Demo;\n"
        "public class User {\n"
        "  public Widget MakeWidget() {\n"
        "    return new Widget(new Color(), 8080);\n"  # Color needs string
        "  }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Arity mismatch" in e and "Color" in e for e in errors), errors


def test_csharp_unresolved_subnamespace(tmp_path):
    """Import refers to Demo.Sub which we don't scaffold."""
    decomp = _csharp_decomposition()
    decomp["stub_files"]["Demo/User.cs"] = (
        "namespace Demo;\n"
        "using Demo.Sub;\n"  # sub-namespace doesn't exist
        "public class User {\n"
        "  public Widget MakeWidget(Color color) {\n"
        "    return new Widget(color, 8080);\n"
        "  }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Unresolved import" in e and "Demo.Sub" in e for e in errors), errors


# ----- C -----

pytest.importorskip("tree_sitter_c")


def _c_decomposition() -> dict:
    """A clean two-subtask C decomposition with a shared types header."""
    return {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["src/widget.h", "src/widget.c"],
             "stub_test_files": ["src/widget_test.c"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["src/user.c"],
             "stub_test_files": ["src/user_test.c"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {
            "src/types.h": (
                "#ifndef TYPES_H\n#define TYPES_H\n"
                "typedef struct { int r; int g; int b; } Color;\n"
                "#endif\n"
            ),
        },
        "stub_files": {
            "src/widget.h": (
                "#ifndef WIDGET_H\n#define WIDGET_H\n"
                "#include \"types.h\"\n"
                "int widget_render(Color color, int port);\n"
                "#endif\n"
            ),
            "src/widget.c": (
                "#include \"widget.h\"\n"
                "int widget_render(Color color, int port) {\n"
                "    return -1; /* stub */\n"
                "}\n"
            ),
            "src/user.c": (
                "#include \"widget.h\"\n"
                "#include \"types.h\"\n"
                "int use(Color color) {\n"
                "    return widget_render(color, 8080);\n"
                "}\n"
            ),
        },
        "stub_test_files": {
            "src/widget_test.c": (
                "#include \"widget.h\"\n"
                "int main(void) {\n"
                "    Color c = {255, 0, 0};\n"
                "    return widget_render(c, 80) == 0 ? 0 : 1;\n"
                "}\n"
            ),
            "src/user_test.c": (
                "#include \"types.h\"\n"
                "int use(Color color);\n"
                "int main(void) {\n"
                "    Color c = {0, 0, 0};\n"
                "    return use(c) == 0 ? 0 : 1;\n"
                "}\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }


def test_c_decomposition_clean(tmp_path):
    errors = _run_validate_no_runner(_c_decomposition(), str(tmp_path))
    assert errors == [], errors


def test_c_arity_mismatch(tmp_path):
    """widget_render needs (Color, int); user.c calls it with one arg."""
    decomp = _c_decomposition()
    decomp["stub_files"]["src/user.c"] = (
        "#include \"widget.h\"\n"
        "#include \"types.h\"\n"
        "int use(Color color) {\n"
        "    return widget_render(color);\n"  # missing port
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Arity mismatch" in e and "widget_render" in e for e in errors), errors


def test_c_unresolved_local_include(tmp_path):
    """Header that doesn't exist anywhere in scaffolded or on disk."""
    decomp = _c_decomposition()
    decomp["stub_files"]["src/user.c"] = (
        "#include \"does_not_exist.h\"\n"
        "#include \"types.h\"\n"
        "int use(Color color) { return 0; }\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Unresolved import" in e and "does_not_exist" in e for e in errors), errors


def test_c_system_include_resolves(tmp_path):
    """``<stdio.h>`` should always resolve, even with no toolchain present."""
    decomp = _c_decomposition()
    decomp["stub_files"]["src/widget.c"] = (
        "#include <stdio.h>\n"
        "#include \"widget.h\"\n"
        "int widget_render(Color color, int port) { return -1; }\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    # Structural checks (no Unresolved/Interface/Arity) must remain clean.
    assert not any("Unresolved import" in e for e in errors), errors


# ----- C++ -----

pytest.importorskip("tree_sitter_cpp")


def _cpp_decomposition() -> dict:
    """A clean C++ decomposition: one shared header with a class,
    two consumer files that exercise constructor + free function arity."""
    return {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["src/widget.hpp", "src/widget.cpp"],
             "stub_test_files": ["src/widget_test.cpp"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["src/user.cpp"],
             "stub_test_files": ["src/user_test.cpp"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {},
        "stub_files": {
            "src/widget.hpp": (
                "#ifndef WIDGET_HPP\n#define WIDGET_HPP\n"
                "#include <string>\n"
                "class Widget {\n"
                "public:\n"
                "  Widget(std::string name, int port);\n"
                "  std::string render() const;\n"
                "};\n"
                "#endif\n"
            ),
            "src/widget.cpp": (
                "#include \"widget.hpp\"\n"
                "Widget::Widget(std::string name, int port) {}\n"
                "std::string Widget::render() const { return \"\"; }\n"
            ),
            "src/user.cpp": (
                "#include \"widget.hpp\"\n"
                "Widget make() {\n"
                "  return Widget(\"hi\", 8080);\n"
                "}\n"
            ),
        },
        "stub_test_files": {
            "src/widget_test.cpp": (
                "#include \"widget.hpp\"\n"
                "int main() {\n"
                "  Widget w(\"test\", 80);\n"
                "  return w.render() == \"ok\" ? 0 : 1;\n"
                "}\n"
            ),
            "src/user_test.cpp": (
                "#include \"widget.hpp\"\n"
                "Widget make();\n"
                "int main() {\n"
                "  Widget w = make();\n"
                "  return 0;\n"
                "}\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }


def test_cpp_decomposition_clean(tmp_path):
    errors = _run_validate_no_runner(_cpp_decomposition(), str(tmp_path))
    assert errors == [], errors


def test_cpp_constructor_arity_mismatch(tmp_path):
    """Widget(std::string, int); user calls Widget(\"hi\")  -  one missing arg."""
    decomp = _cpp_decomposition()
    decomp["stub_files"]["src/user.cpp"] = (
        "#include \"widget.hpp\"\n"
        "Widget make() {\n"
        "  return Widget(\"hi\");\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Arity mismatch" in e and "Widget" in e for e in errors), errors


def test_cpp_unresolved_local_include(tmp_path):
    decomp = _cpp_decomposition()
    decomp["stub_files"]["src/user.cpp"] = (
        "#include \"does_not_exist.hpp\"\n"
        "int dummy() { return 0; }\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Unresolved import" in e and "does_not_exist" in e for e in errors), errors


# ----- Rust -----

pytest.importorskip("tree_sitter_rust")


def _write_cargo_toml(repo: str, deps: dict | None = None) -> None:
    with open(os.path.join(repo, "Cargo.toml"), "w") as f:
        f.write("[package]\nname = \"demo\"\nversion = \"0.1.0\"\n")
        f.write("edition = \"2021\"\n\n")
        f.write("[dependencies]\n")
        for k, v in (deps or {}).items():
            f.write(f"{k} = \"{v}\"\n")


def _rust_decomposition() -> dict:
    return {
        "subtasks": [
            {"subtask_id": "widget",
             "stub_files": ["src/widget.rs"],
             "stub_test_files": ["src/widget_test.rs"],
             "dependencies": [],
             "complexity_weight": 0.5},
            {"subtask_id": "user",
             "stub_files": ["src/user.rs"],
             "stub_test_files": ["src/user_test.rs"],
             "dependencies": ["widget"],
             "complexity_weight": 0.5},
        ],
        "shared_files": {
            "src/types.rs": (
                "pub struct Color { pub r: u8, pub g: u8, pub b: u8 }\n"
                "impl Color {\n"
                "  pub fn new(r: u8, g: u8, b: u8) -> Self {\n"
                "    Color { r, g, b }\n"
                "  }\n"
                "}\n"
            ),
        },
        "stub_files": {
            "src/widget.rs": (
                "use crate::types::Color;\n"
                "pub struct Widget {\n"
                "  color: Color,\n"
                "  port: u16,\n"
                "}\n"
                "impl Widget {\n"
                "  pub fn new(color: Color, port: u16) -> Self {\n"
                "    unimplemented!()\n"
                "  }\n"
                "  pub fn render(&self) -> String {\n"
                "    unimplemented!()\n"
                "  }\n"
                "}\n"
            ),
            "src/user.rs": (
                "use crate::widget::Widget;\n"
                "use crate::types::Color;\n"
                "pub fn make_widget(color: Color) -> Widget {\n"
                "  Widget::new(color, 8080)\n"
                "}\n"
            ),
        },
        "stub_test_files": {
            "src/widget_test.rs": (
                "use crate::widget::Widget;\n"
                "use crate::types::Color;\n"
                "#[test]\n"
                "fn test_render() {\n"
                "  let c = Color::new(255, 0, 0);\n"
                "  let w = Widget::new(c, 80);\n"
                "  assert_eq!(w.render(), \"ok\");\n"
                "}\n"
            ),
            "src/user_test.rs": (
                "use crate::user::make_widget;\n"
                "use crate::types::Color;\n"
                "#[test]\n"
                "fn test_make_widget() {\n"
                "  let c = Color::new(0, 0, 0);\n"
                "  let _w = make_widget(c);\n"
                "}\n"
            ),
        },
        "integration_test_files": {},
        "requirements_additions": [],
    }


def test_rust_decomposition_clean(tmp_path):
    _write_cargo_toml(str(tmp_path))
    errors = _run_validate_no_runner(_rust_decomposition(), str(tmp_path))
    assert errors == [], errors


def test_rust_missing_imported_name(tmp_path):
    """user.rs imports Gadget from crate::widget, but only Widget is defined."""
    _write_cargo_toml(str(tmp_path))
    decomp = _rust_decomposition()
    decomp["stub_files"]["src/user.rs"] = (
        "use crate::widget::Gadget;\n"  # Gadget not defined
        "use crate::types::Color;\n"
        "pub fn make_widget(color: Color) -> Gadget {\n"
        "  unimplemented!()\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Interface mismatch" in e and "'Gadget'" in e for e in errors), errors


def test_rust_constructor_arity_mismatch(tmp_path):
    """Widget::new wants (Color, u16); user passes only color."""
    _write_cargo_toml(str(tmp_path))
    decomp = _rust_decomposition()
    decomp["stub_files"]["src/user.rs"] = (
        "use crate::widget::Widget;\n"
        "use crate::types::Color;\n"
        "pub fn make_widget(color: Color) -> Widget {\n"
        "  Widget::new(color)\n"  # missing port arg
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Arity mismatch" in e and "Widget" in e for e in errors), errors


def test_rust_function_arity_mismatch(tmp_path):
    """make_widget takes one arg; caller passes two."""
    _write_cargo_toml(str(tmp_path))
    decomp = _rust_decomposition()
    decomp["stub_test_files"]["src/user_test.rs"] = (
        "use crate::user::make_widget;\n"
        "use crate::types::Color;\n"
        "#[test]\n"
        "fn bad() {\n"
        "  let c = Color::new(0, 0, 0);\n"
        "  let _w = make_widget(c, 99);\n"  # too many args
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Arity mismatch" in e and "make_widget" in e for e in errors), errors


def test_rust_unresolved_submodule(tmp_path):
    _write_cargo_toml(str(tmp_path))
    decomp = _rust_decomposition()
    decomp["stub_files"]["src/user.rs"] = (
        "use crate::widget::sub::Thing;\n"  # sub-module doesn't exist
        "use crate::widget::Widget;\n"
        "use crate::types::Color;\n"
        "pub fn make_widget(color: Color) -> Widget {\n"
        "  Widget::new(color, 8080)\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    assert any("Unresolved import" in e and "crate::widget::sub" in e for e in errors), errors


def test_rust_external_crate_resolves_via_cargo(tmp_path):
    _write_cargo_toml(str(tmp_path), deps={"serde": "1.0"})
    decomp = _rust_decomposition()
    decomp["stub_files"]["src/widget.rs"] = (
        "use crate::types::Color;\n"
        "use serde::Serialize;\n"
        "pub struct Widget { color: Color, port: u16 }\n"
        "impl Widget {\n"
        "  pub fn new(color: Color, port: u16) -> Self { unimplemented!() }\n"
        "  pub fn render(&self) -> String { unimplemented!() }\n"
        "}\n"
    )
    errors = _run_validate_no_runner(decomp, str(tmp_path))
    # No Unresolved import since serde is in Cargo.toml.
    assert not any("Unresolved import" in e and "serde" in e for e in errors), errors
