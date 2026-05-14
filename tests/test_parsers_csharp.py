"""Unit tests for the C# parser."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_c_sharp")

from validator.parsers import detect
from validator.parsers.csharp import parser as cs
from validator.parsers.types import ImportInfo


def _parse(src: str, filepath: str = "Demo/Widget.cs"):
    return cs.parse(src, filepath)


# ---- registry / paths ----

def test_csharp_parser_registered_for_dot_cs():
    p = detect("X.cs")
    assert p is not None
    assert p.name == "csharp"


def test_module_path_from_file_scoped_namespace():
    src = "namespace Demo.Inner;\npublic class X {}\n"
    tree = _parse(src)
    assert cs.module_path_for_file("Demo/Widget.cs",
                                    tree=tree, source=src) == "Demo.Inner"


def test_module_path_from_block_namespace():
    src = "namespace Demo.Inner { public class X {} }\n"
    tree = _parse(src)
    assert cs.module_path_for_file("X.cs", tree=tree, source=src) == "Demo.Inner"


def test_module_path_falls_back_to_path():
    assert cs.module_path_for_file("Demo/Inner/Widget.cs") == "Demo.Inner"
    assert cs.module_path_for_file("src/Demo/Inner/Widget.cs") == "Demo.Inner"


# ---- imports ----

def test_using_directive_simple():
    src = "using System;\nusing System.Collections.Generic;\n"
    tree = _parse(src)
    imps = cs.extract_imports(tree, src, "X.cs")
    modules = [i.module for i in imps]
    assert "System" in modules
    assert "System.Collections.Generic" in modules
    # No named imports for plain `using X;`
    assert all(i.imported_names == [] for i in imps)


def test_using_static():
    src = "using static System.Math;\n"
    tree = _parse(src)
    imps = cs.extract_imports(tree, src, "X.cs")
    assert imps[0].module == "System.Math"


def test_using_alias():
    """Aliased usings model the target namespace; we deliberately drop
    the alias name from ``imported_names`` so the cross-file consistency
    check doesn't emit spurious 'name not defined in module' errors for
    an alias that the target namespace never declared."""
    src = "using IO = System.IO;\n"
    tree = _parse(src)
    imps = cs.extract_imports(tree, src, "X.cs")
    assert imps[0].module == "System.IO"
    assert imps[0].imported_names == []


# ---- definitions ----

def test_class_in_file_scoped_namespace():
    src = (
        "namespace Demo;\n"
        "public class Widget {\n"
        "  public Widget(string name, int port) {}\n"
        "  public string Render() => name;\n"
        "}\n"
    )
    tree = _parse(src)
    names = cs.extract_defined_names(tree, src)
    cls = names["Widget"]
    assert cls.kind == "class"
    assert cls.is_exported is True
    ctor = cls.methods["Widget"]
    assert [p.name for p in ctor.params] == ["name", "port"]
    assert ctor.required_arg_count == 2
    assert "Render" in cls.methods


def test_class_in_block_namespace():
    src = (
        "namespace Demo {\n"
        "  public class Inside {\n"
        "    public Inside(string s) {}\n"
        "  }\n"
        "}\n"
    )
    tree = _parse(src)
    names = cs.extract_defined_names(tree, src)
    assert "Inside" in names
    assert names["Inside"].methods["Inside"].required_arg_count == 1


def test_record_interface_enum_struct():
    src = (
        "namespace Demo;\n"
        "public interface IHealth { bool Check(); }\n"
        "public record Point(int x, int y);\n"
        "public enum Color { RED, GREEN }\n"
        "public struct Vec { public int x; }\n"
    )
    tree = _parse(src)
    names = cs.extract_defined_names(tree, src)
    assert names["IHealth"].kind == "interface"
    assert "Check" in names["IHealth"].methods
    assert names["Point"].kind == "class"
    assert names["Point"].methods["Point"].required_arg_count == 2
    assert names["Color"].kind == "enum"
    assert names["Vec"].kind == "class"


def test_default_param_lowers_required_count():
    src = (
        "namespace Demo;\n"
        "public class P {\n"
        "  public P(string a, int b = 3) {}\n"
        "}\n"
    )
    tree = _parse(src)
    ctor = cs.extract_defined_names(tree, src)["P"].methods["P"]
    assert ctor.required_arg_count == 1
    assert ctor.max_arg_count == 2


def test_params_keyword_sets_varargs():
    src = (
        "namespace Demo;\n"
        "public class P {\n"
        "  public void F(string a, params string[] rest) {}\n"
        "}\n"
    )
    tree = _parse(src)
    f = cs.extract_defined_names(tree, src)["P"].methods["F"]
    assert [p.name for p in f.params] == ["a"]
    assert f.has_varargs is True


# ---- call sites ----

def test_call_sites_new_and_invocation():
    src = (
        "namespace Demo;\n"
        "public class C {\n"
        "  void Use() {\n"
        "    var w = new Widget(\"a\", 80);\n"
        "    w.Render();\n"
        "    Math.Max(1, 2);\n"
        "  }\n"
        "}\n"
    )
    tree = _parse(src)
    sites = cs.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    assert ("Widget", 2) in by_name
    assert ("Render", 0) in by_name
    assert ("Max", 2) in by_name


# ---- resolution ----

def test_resolves_dotnet_stdlib():
    imp = ImportInfo(module="System.Collections.Generic")
    assert cs.resolves(imp, "/tmp", set(), {}) is True
    assert cs.resolves(ImportInfo(module="Microsoft.AspNetCore"), "/tmp", set(), {}) is True


def test_resolves_authored_namespace():
    scaffolded = {"Demo/Widget.cs", "Demo/Helper.cs"}
    imp = ImportInfo(module="Demo")
    assert cs.resolves(imp, "/tmp", scaffolded, {}) is True


def test_resolves_unknown_subnamespace_of_authored_fails():
    scaffolded = {"Demo/Widget.cs"}
    imp = ImportInfo(module="Demo.Sub")
    assert cs.resolves(imp, "/tmp", scaffolded, {}) is False


def test_resolves_third_party_ecosystem():
    imp = ImportInfo(module="Newtonsoft.Json")
    assert cs.resolves(imp, "/tmp", set(), {}) is True
