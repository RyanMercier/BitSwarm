"""Unit tests for the C parser."""
from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_c")

from validator.parsers import detect
from validator.parsers.c import parser as c
from validator.parsers.types import ImportInfo


def _parse(src: str, filepath: str = "src/widget.c"):
    return c.parse(src, filepath)


# ---- registry / paths ----

def test_c_parser_registered():
    assert detect("x.c").name == "c"
    # .h without context defaults to C
    assert detect("x.h").name == "c"


def test_module_path_is_repo_path():
    assert c.module_path_for_file("src/widget.h") == "src/widget.h"
    assert c.module_path_for_file("src\\widget.c") == "src/widget.c"


# ---- includes ----

def test_includes_system_and_local():
    src = (
        "#include <stdio.h>\n"
        "#include \"widget.h\"\n"
        "#include \"../shared/types.h\"\n"
    )
    tree = _parse(src, "src/main.c")
    imps = c.extract_imports(tree, src, "src/main.c")
    by_mod = {i.module: i for i in imps}
    assert "stdio.h" in by_mod
    assert by_mod["stdio.h"].is_relative is False
    # Local includes get joined against the importer's dir.
    assert "src/widget.h" in by_mod
    assert by_mod["src/widget.h"].is_relative is True
    assert "shared/types.h" in by_mod


# ---- definitions ----

def test_function_declaration_and_definition_merge():
    src = (
        "int compute(int a, int b);\n"
        "int compute(int a, int b) { return a + b; }\n"
    )
    tree = _parse(src)
    names = c.extract_defined_names(tree, src)
    assert "compute" in names
    fn = names["compute"]
    assert fn.kind == "function"
    assert [p.name for p in fn.params] == ["a", "b"]
    assert fn.required_arg_count == 2


def test_void_param_is_zero_args():
    src = "void helper(void) {}\n"
    tree = _parse(src)
    fn = c.extract_defined_names(tree, src)["helper"]
    assert fn.params == []
    assert fn.required_arg_count == 0


def test_static_function_marked_unexported():
    src = "static void hidden(int x) {}\n"
    tree = _parse(src)
    fn = c.extract_defined_names(tree, src)["hidden"]
    assert fn.is_exported is False


def test_varargs_function():
    src = "int printf_like(const char *fmt, ...) { return 0; }\n"
    tree = _parse(src)
    fn = c.extract_defined_names(tree, src)["printf_like"]
    assert fn.has_varargs is True


def test_typedef_struct_registers_as_class():
    src = (
        "typedef struct {\n"
        "    int x;\n"
        "    int y;\n"
        "} Point;\n"
    )
    tree = _parse(src)
    names = c.extract_defined_names(tree, src)
    assert names["Point"].kind == "class"


def test_typedef_alias_registers_as_type():
    src = "typedef int MyInt;\n"
    tree = _parse(src)
    names = c.extract_defined_names(tree, src)
    assert names["MyInt"].kind == "type"


def test_struct_with_tag_registers_as_class():
    src = "struct Widget { int port; };\n"
    tree = _parse(src)
    names = c.extract_defined_names(tree, src)
    assert names["Widget"].kind == "class"


def test_enum_with_tag_registers_as_enum():
    src = "enum Color { RED, GREEN, BLUE };\n"
    tree = _parse(src)
    names = c.extract_defined_names(tree, src)
    assert names["Color"].kind == "enum"


# ---- call sites ----

def test_call_sites():
    src = (
        "void use(void) {\n"
        "    render(80);\n"
        "    int n = compute(1, 2);\n"
        "}\n"
    )
    tree = _parse(src)
    sites = c.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    assert ("render", 1) in by_name
    assert ("compute", 2) in by_name


# ---- resolution ----

def test_resolves_system_includes_always():
    imp = ImportInfo(module="stdio.h", is_relative=False)
    assert c.resolves(imp, "/tmp", set(), {}) is True


def test_resolves_scaffolded_local_include():
    scaffolded = {"src/widget.h"}
    imp = ImportInfo(module="src/widget.h", is_relative=True)
    assert c.resolves(imp, "/tmp", scaffolded, {}) is True


def test_unresolved_missing_local_include():
    imp = ImportInfo(module="src/missing.h", is_relative=True)
    assert c.resolves(imp, "/tmp", set(), {}) is False
