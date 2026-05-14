"""
Unit tests for the TypeScript / JavaScript parser.

Each test feeds a small snippet through the parser and asserts the
shape of the extracted ``ImportInfo`` / ``CallableInfo`` / ``CallSite``
records. Fixtures live inline rather than in ``tests/parser_fixtures/``
because each snippet is short and the tests read more clearly with the
source colocated.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

ts_parser_mod = pytest.importorskip("validator.parsers.typescript")

from validator.parsers import detect
from validator.parsers.typescript import parser as ts


# ---- extension registry ----

def test_ts_parser_handles_expected_extensions():
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        assert detect(f"foo{ext}") is not None
        assert detect(f"foo{ext}").name == "typescript"


def test_module_path_strips_extension_and_index():
    assert ts.module_path_for_file("src/widget.ts") == "src/widget"
    assert ts.module_path_for_file("src/widget.tsx") == "src/widget"
    assert ts.module_path_for_file("src/foo/index.ts") == "src/foo"
    assert ts.module_path_for_file("src\\foo\\bar.ts") == "src/foo/bar"


# ---- imports ----

def _parse(src: str, filepath: str = "src/file.ts"):
    return ts.parse(src, filepath)


def test_imports_named_default_namespace():
    src = (
        "import { Foo, Bar as B } from './foo';\n"
        "import Default from 'pkg';\n"
        "import * as ns from 'node:fs';\n"
    )
    tree = _parse(src, "src/x.ts")
    imps = ts.extract_imports(tree, src, "src/x.ts")
    by_mod = {i.module: i for i in imps}

    # relative import normalized to importer's directory
    assert "src/foo" in by_mod
    assert by_mod["src/foo"].imported_names == ["Foo", "Bar"]  # original name, not alias
    assert by_mod["src/foo"].is_relative is True

    assert "pkg" in by_mod
    assert by_mod["pkg"].imported_names == ["Default"]
    assert by_mod["pkg"].is_relative is False

    assert "node:fs" in by_mod
    assert by_mod["node:fs"].imported_names == ["ns"]


def test_imports_relative_traverses_parent():
    src = "import { x } from '../shared/types';\n"
    tree = _parse(src, "src/widgets/x.ts")
    imps = ts.extract_imports(tree, src, "src/widgets/x.ts")
    assert len(imps) == 1
    assert imps[0].module == "src/shared/types"
    assert imps[0].is_relative is True


# ---- defined names ----

def test_function_declaration_params():
    src = (
        "export function greet(name: string, n: number = 1, opt?: string): string {\n"
        "  return name;\n"
        "}\n"
        "function _internal(x: number) {}\n"
    )
    tree = _parse(src)
    names = ts.extract_defined_names(tree, src)

    assert "greet" in names
    g = names["greet"]
    assert g.kind == "function"
    assert g.is_exported is True
    assert [p.name for p in g.params] == ["name", "n", "opt"]
    assert [p.has_default for p in g.params] == [False, True, True]
    assert g.required_arg_count == 1
    assert g.max_arg_count == 3

    assert "_internal" in names
    assert names["_internal"].is_exported is False


def test_rest_param_sets_varargs():
    src = "export function many(...xs: number[]) {}\n"
    tree = _parse(src)
    info = ts.extract_defined_names(tree, src)["many"]
    assert info.has_varargs is True
    assert info.params == []  # rest is not counted in the param list


def test_class_with_constructor_and_methods():
    src = (
        "export class Widget {\n"
        "  constructor(name: string, port: number, opts?: object) {}\n"
        "  render(): string { return ''; }\n"
        "}\n"
    )
    tree = _parse(src)
    cls = ts.extract_defined_names(tree, src)["Widget"]
    assert cls.kind == "class"
    assert cls.is_exported is True
    ctor = cls.methods["constructor"]
    assert ctor.kind == "method"
    assert [p.name for p in ctor.params] == ["name", "port", "opts"]
    assert ctor.required_arg_count == 2
    assert ctor.max_arg_count == 3
    assert "render" in cls.methods


def test_interface_type_enum_const():
    src = (
        "export interface Health { check(): boolean }\n"
        "export type Id = string;\n"
        "export enum Color { RED, GREEN }\n"
        "export const PI = 3.14;\n"
    )
    tree = _parse(src)
    names = ts.extract_defined_names(tree, src)
    assert names["Health"].kind == "interface"
    assert "check" in names["Health"].methods
    assert names["Id"].kind == "type"
    assert names["Color"].kind == "enum"
    assert names["PI"].kind == "constant"


# ---- call sites ----

def test_call_sites_function_and_constructor():
    src = (
        "const w = new Widget('a', 9000);\n"
        "greet('x', 2);\n"
        "obj.method(1, 2, 3);\n"
    )
    tree = _parse(src)
    sites = ts.extract_call_sites(tree, src)
    by_name = {s.callee_name: s for s in sites}
    assert by_name["Widget"].arg_count == 2
    assert by_name["greet"].arg_count == 2
    assert by_name["method"].arg_count == 3


# ---- resolution ----

def test_resolves_node_builtins():
    from validator.parsers.types import ImportInfo
    imp_prefix = ImportInfo(module="node:fs", is_relative=False)
    imp_bare = ImportInfo(module="fs", is_relative=False)
    assert ts.resolves(imp_prefix, "/tmp", set(), {}) is True
    assert ts.resolves(imp_bare, "/tmp", set(), {}) is True


def test_resolves_scaffolded_relative():
    from validator.parsers.types import ImportInfo
    scaffolded = {"src/widget.ts", "src/types.ts"}
    imp = ImportInfo(module="src/widget", is_relative=True)
    assert ts.resolves(imp, "/tmp", scaffolded, {}) is True

    missing = ImportInfo(module="src/missing", is_relative=True)
    assert ts.resolves(missing, "/tmp", scaffolded, {}) is False


def test_resolves_relative_index_file():
    from validator.parsers.types import ImportInfo
    scaffolded = {"src/widgets/index.ts"}
    imp = ImportInfo(module="src/widgets", is_relative=True)
    assert ts.resolves(imp, "/tmp", scaffolded, {}) is True


def test_resolves_package_json_dep():
    from validator.parsers.types import ImportInfo
    with tempfile.TemporaryDirectory() as tmp:
        pkg = {
            "name": "demo",
            "dependencies": {"express": "^4.0.0"},
            "devDependencies": {"vitest": "^1.0.0", "@types/node": "^20"},
        }
        with open(os.path.join(tmp, "package.json"), "w") as f:
            json.dump(pkg, f)

        imp = ImportInfo(module="express", is_relative=False)
        assert ts.resolves(imp, tmp, set(), {}) is True

        # scoped pkg
        imp2 = ImportInfo(module="@types/node", is_relative=False)
        assert ts.resolves(imp2, tmp, set(), {}) is True

        # subpath: express/router should resolve via top-level 'express'
        imp3 = ImportInfo(module="express/router", is_relative=False)
        assert ts.resolves(imp3, tmp, set(), {}) is True

        # not in package.json
        imp4 = ImportInfo(module="lodash", is_relative=False)
        assert ts.resolves(imp4, tmp, set(), {}) is False
