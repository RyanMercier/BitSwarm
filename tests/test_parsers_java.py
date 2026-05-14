"""Unit tests for the Java parser."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_java")

from validator.parsers import detect
from validator.parsers.java import parser as java
from validator.parsers.types import ImportInfo


def _parse(src: str, filepath: str = "com/example/Widget.java"):
    return java.parse(src, filepath)


# ---- registry / paths ----

def test_java_parser_registered_for_dot_java():
    p = detect("X.java")
    assert p is not None
    assert p.name == "java"


def test_module_path_from_package_declaration():
    src = "package com.example.demo;\npublic class X {}\n"
    tree = _parse(src, "src/main/java/com/example/demo/X.java")
    assert java.module_path_for_file(
        "src/main/java/com/example/demo/X.java",
        tree=tree, source=src,
    ) == "com.example.demo"


def test_module_path_falls_back_to_path():
    """No package declaration -> path-based fallback (strips src/main/java/)."""
    assert java.module_path_for_file(
        "src/main/java/com/example/X.java"
    ) == "com.example"
    assert java.module_path_for_file("com/example/X.java") == "com.example"


# ---- imports ----

def test_imports_named_wildcard_static():
    src = (
        "package demo;\n"
        "import java.util.List;\n"
        "import com.example.shared.*;\n"
        "import static java.lang.Math.PI;\n"
    )
    tree = _parse(src, "demo/X.java")
    imps = java.extract_imports(tree, src, "demo/X.java")
    by_mod = {(i.module, tuple(i.imported_names)) for i in imps}
    assert ("java.util", ("List",)) in by_mod
    assert ("com.example.shared", ("*",)) in by_mod
    assert ("java.lang.Math", ("PI",)) in by_mod
    for i in imps:
        assert i.is_relative is False


# ---- definitions ----

def test_class_with_constructor_and_methods():
    src = (
        "package demo;\n"
        "public class Widget {\n"
        "  public Widget(String name, int port) {}\n"
        "  public String render() { return \"\"; }\n"
        "}\n"
    )
    tree = _parse(src)
    names = java.extract_defined_names(tree, src)
    cls = names["Widget"]
    assert cls.kind == "class"
    assert cls.is_exported is True
    ctor = cls.methods["Widget"]
    assert [p.name for p in ctor.params] == ["name", "port"]
    assert ctor.required_arg_count == 2
    assert ctor.max_arg_count == 2
    assert "render" in cls.methods


def test_interface_record_enum():
    src = (
        "package demo;\n"
        "public interface Health { boolean check(); }\n"
        "public record Point(int x, int y) {}\n"
        "public enum Color { RED, GREEN }\n"
    )
    tree = _parse(src)
    names = java.extract_defined_names(tree, src)
    assert names["Health"].kind == "interface"
    assert "check" in names["Health"].methods

    point = names["Point"]
    assert point.kind == "class"
    # Record exposes its primary constructor params at both the class
    # and constructor level.
    assert [p.name for p in point.params] == ["x", "y"]
    assert "Point" in point.methods
    assert point.methods["Point"].required_arg_count == 2

    assert names["Color"].kind == "enum"


def test_varargs_sets_has_varargs():
    src = (
        "package demo;\n"
        "public class P {\n"
        "  public void f(String a, int b, String... rest) {}\n"
        "}\n"
    )
    tree = _parse(src)
    f = java.extract_defined_names(tree, src)["P"].methods["f"]
    # Two non-vararg params; rest is the varargs marker.
    assert [p.name for p in f.params] == ["a", "b"]
    assert f.has_varargs is True


# ---- call sites ----

def test_call_sites_new_and_invocation():
    src = (
        "package demo;\n"
        "public class C {\n"
        "  void use() {\n"
        "    Widget w = new Widget(\"a\", 80);\n"
        "    w.render();\n"
        "    Math.max(1, 2);\n"
        "  }\n"
        "}\n"
    )
    tree = _parse(src)
    sites = java.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    assert ("Widget", 2) in by_name
    assert ("render", 0) in by_name
    assert ("max", 2) in by_name


# ---- resolution ----

def test_resolves_stdlib_and_authored():
    scaffolded = {
        "src/main/java/com/example/Widget.java",
        "src/main/java/com/example/Helper.java",
    }
    imp_stdlib = ImportInfo(module="java.util", imported_names=["List"])
    assert java.resolves(imp_stdlib, "/tmp", scaffolded, {}) is True

    imp_authored = ImportInfo(module="com.example", imported_names=["Widget"])
    assert java.resolves(imp_authored, "/tmp", scaffolded, {}) is True


def test_resolves_subpackage_of_authored_must_exist():
    scaffolded = {"src/main/java/com/example/Widget.java"}
    # com.example.sub is a sub-package of our authored root but we don't
    # have any files there -> should fail.
    imp = ImportInfo(module="com.example.sub", imported_names=["Foo"])
    assert java.resolves(imp, "/tmp", scaffolded, {}) is False


def test_resolves_external_ecosystem():
    scaffolded = {"src/main/java/com/example/X.java"}
    imp = ImportInfo(module="org.springframework.boot", imported_names=["Application"])
    assert java.resolves(imp, "/tmp", scaffolded, {}) is True
