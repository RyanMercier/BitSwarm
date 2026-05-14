"""Unit tests for the C++ parser."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_cpp")

from validator.parsers import detect
from validator.parsers.cpp import parser as cpp
from validator.parsers.types import ImportInfo


def _parse(src: str, filepath: str = "src/widget.cpp"):
    return cpp.parse(src, filepath)


# ---- registry / paths ----

def test_cpp_parser_registered_for_cpp_extensions():
    for ext in (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"):
        assert detect(f"x{ext}").name == "cpp"


def test_dot_h_upgrades_to_cpp_with_sibling():
    scaffolded = {"src/widget.h", "src/widget.cpp"}
    p = detect("src/widget.h", scaffolded_files=scaffolded)
    assert p.name == "cpp"


def test_dot_h_stays_c_without_cpp_sibling():
    p = detect("src/widget.h", scaffolded_files={"src/widget.h", "src/widget.c"})
    assert p.name == "c"


# ---- includes ----

def test_includes_via_shared_logic():
    src = "#include <string>\n#include \"widget.h\"\n"
    tree = _parse(src, "src/main.cpp")
    imps = cpp.extract_imports(tree, src, "src/main.cpp")
    by_mod = {i.module: i for i in imps}
    assert "string" in by_mod
    assert by_mod["string"].is_relative is False
    assert "src/widget.h" in by_mod
    assert by_mod["src/widget.h"].is_relative is True


# ---- class / namespace ----

def test_class_with_inline_methods():
    src = (
        "class Widget {\n"
        "public:\n"
        "  Widget(int port) {}\n"
        "  int render() { return 0; }\n"
        "};\n"
    )
    tree = _parse(src)
    cls = cpp.extract_defined_names(tree, src)["Widget"]
    assert cls.kind == "class"
    ctor = cls.methods["Widget"]
    assert [p.name for p in ctor.params] == ["port"]
    assert "render" in cls.methods


def test_class_with_declared_methods_only():
    src = (
        "class Widget {\n"
        "public:\n"
        "  Widget(int port);\n"
        "  std::string render() const;\n"
        "};\n"
    )
    tree = _parse(src)
    cls = cpp.extract_defined_names(tree, src)["Widget"]
    assert cls.kind == "class"
    assert cls.methods["Widget"].required_arg_count == 1
    assert "render" in cls.methods


def test_namespace_types_visible_at_file_level():
    src = (
        "namespace demo {\n"
        "  class Widget {\n"
        "  public:\n"
        "    Widget(int port) {}\n"
        "  };\n"
        "}\n"
    )
    tree = _parse(src)
    # Widget is registered under its bare name; namespace doesn't prefix
    # the registry key because the validator keys by file module.
    names = cpp.extract_defined_names(tree, src)
    assert "Widget" in names
    assert names["Widget"].kind == "class"


def test_nested_namespaces_flatten():
    src = (
        "namespace outer { namespace inner {\n"
        "  class X { public: X(int a, int b) {} };\n"
        "} }\n"
    )
    tree = _parse(src)
    names = cpp.extract_defined_names(tree, src)
    assert "X" in names
    assert names["X"].methods["X"].required_arg_count == 2


def test_using_alias_registers_as_type():
    src = "using MyInt = int;\n"
    tree = _parse(src)
    names = cpp.extract_defined_names(tree, src)
    assert names["MyInt"].kind == "type"


def test_top_level_function():
    src = "int compute(int a, int b) { return a + b; }\n"
    tree = _parse(src)
    fn = cpp.extract_defined_names(tree, src)["compute"]
    assert fn.kind == "function"
    assert fn.required_arg_count == 2


# ---- call sites ----

def test_call_sites_stack_and_new_and_invocation():
    src = (
        "void use() {\n"
        "  Widget w(\"a\", 80);\n"
        "  auto p = new demo::Widget(\"b\", 90);\n"
        "  w.render();\n"
        "  helper(1, 2, 3);\n"
        "}\n"
    )
    tree = _parse(src)
    sites = cpp.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    # Stack construction: Widget w(...)
    assert ("Widget", 2) in by_name
    # new ns::Widget(...): rightmost type identifier is "Widget"
    assert any(name == "Widget" and ac == 2 for name, ac in by_name)
    # Method call
    assert ("render", 0) in by_name
    # Free function
    assert ("helper", 3) in by_name


# ---- resolution ----

def test_resolves_via_shared_helper():
    scaffolded = {"src/widget.hpp"}
    imp = ImportInfo(module="src/widget.hpp", is_relative=True)
    assert cpp.resolves(imp, "/tmp", scaffolded, {}) is True

    sys_imp = ImportInfo(module="string", is_relative=False)
    assert cpp.resolves(sys_imp, "/tmp", set(), {}) is True

    missing = ImportInfo(module="src/missing.h", is_relative=True)
    assert cpp.resolves(missing, "/tmp", set(), {}) is False
