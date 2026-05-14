"""Unit tests for the Rust parser."""
from __future__ import annotations

import os
import tempfile

import pytest

pytest.importorskip("tree_sitter_rust")

from validator.parsers import detect
from validator.parsers.rust import parser as rust
from validator.parsers.types import ImportInfo


def _parse(src: str, filepath: str = "src/widget.rs"):
    return rust.parse(src, filepath)


# ---- registry / paths ----

def test_rust_parser_registered():
    assert detect("X.rs").name == "rust"


def test_module_path_for_crate_root():
    assert rust.module_path_for_file("src/lib.rs") == "crate"
    assert rust.module_path_for_file("src/main.rs") == "crate"


def test_module_path_for_nested_file():
    assert rust.module_path_for_file("src/widget.rs") == "crate::widget"
    assert rust.module_path_for_file("src/sub/widget.rs") == "crate::sub::widget"
    assert rust.module_path_for_file("src/sub/mod.rs") == "crate::sub"


# ---- imports ----

def test_use_single_named():
    src = "use crate::widget::Widget;\n"
    tree = _parse(src)
    imps = rust.extract_imports(tree, src, "src/user.rs")
    assert len(imps) == 1
    assert imps[0].module == "crate::widget"
    assert imps[0].imported_names == ["Widget"]
    assert imps[0].is_relative is False


def test_use_stdlib():
    src = "use std::collections::HashMap;\n"
    tree = _parse(src)
    imps = rust.extract_imports(tree, src, "src/x.rs")
    assert imps[0].module == "std::collections"
    assert imps[0].imported_names == ["HashMap"]


def test_use_grouped():
    src = "use crate::widget::{Widget, Helper, Color};\n"
    tree = _parse(src)
    imps = rust.extract_imports(tree, src, "src/x.rs")
    assert imps[0].module == "crate::widget"
    assert set(imps[0].imported_names) == {"Widget", "Helper", "Color"}


def test_use_super_self_marked_relative():
    src = (
        "use super::types::Theme;\n"
        "use self::sub::Foo;\n"
    )
    tree = _parse(src)
    imps = rust.extract_imports(tree, src, "src/sub/x.rs")
    rel = [i for i in imps if i.is_relative]
    assert len(rel) == 2


def test_use_bare_external_crate():
    src = "use external_crate;\n"
    tree = _parse(src)
    imps = rust.extract_imports(tree, src, "src/x.rs")
    assert imps[0].module == "external_crate"
    assert imps[0].imported_names == []


# ---- definitions ----

def test_function_with_params():
    src = "pub fn greet(name: &str, n: u32) -> String { String::new() }\n"
    tree = _parse(src)
    fn = rust.extract_defined_names(tree, src)["greet"]
    assert fn.kind == "function"
    assert fn.is_exported is True
    assert [p.name for p in fn.params] == ["name", "n"]
    assert fn.required_arg_count == 2


def test_private_function_unexported():
    src = "fn internal() {}\n"
    tree = _parse(src)
    fn = rust.extract_defined_names(tree, src)["internal"]
    assert fn.is_exported is False


def test_struct_with_impl_methods_attached():
    src = (
        "pub struct Widget { name: String, port: u16 }\n"
        "impl Widget {\n"
        "  pub fn new(name: String, port: u16) -> Self { Widget { name, port } }\n"
        "  pub fn render(&self) -> String { self.name.clone() }\n"
        "}\n"
    )
    tree = _parse(src)
    names = rust.extract_defined_names(tree, src)
    cls = names["Widget"]
    assert cls.kind == "class"
    assert cls.is_exported is True
    assert "new" in cls.methods
    assert cls.methods["new"].required_arg_count == 2
    # &self is dropped from render's params.
    assert cls.methods["render"].required_arg_count == 0


def test_tuple_struct_synthesizes_params():
    src = "pub struct Point(i32, i32);\n"
    tree = _parse(src)
    point = rust.extract_defined_names(tree, src)["Point"]
    assert point.kind == "class"
    assert [p.name for p in point.params] == ["_0", "_1"]
    assert point.required_arg_count == 2


def test_trait_registers_as_interface():
    src = "pub trait Health { fn check(&self) -> bool; }\n"
    tree = _parse(src)
    names = rust.extract_defined_names(tree, src)
    assert names["Health"].kind == "interface"
    assert "check" in names["Health"].methods
    # &self is dropped for the trait method too.
    assert names["Health"].methods["check"].required_arg_count == 0


def test_enum_and_type_alias():
    src = (
        "pub enum Color { Red, Green, Blue }\n"
        "pub type Id = String;\n"
    )
    tree = _parse(src)
    names = rust.extract_defined_names(tree, src)
    assert names["Color"].kind == "enum"
    assert names["Id"].kind == "type"


def test_impl_for_trait_attaches_to_type():
    """``impl Trait for Type`` should attach methods to ``Type``."""
    src = (
        "pub struct Widget;\n"
        "pub trait Health { fn check(&self) -> bool; }\n"
        "impl Health for Widget {\n"
        "  fn check(&self) -> bool { true }\n"
        "}\n"
    )
    tree = _parse(src)
    widget = rust.extract_defined_names(tree, src)["Widget"]
    assert "check" in widget.methods


def test_const_and_static():
    src = "pub const PI: f64 = 3.14;\nstatic GREETING: &str = \"hi\";\n"
    tree = _parse(src)
    names = rust.extract_defined_names(tree, src)
    assert names["PI"].kind == "constant"
    assert names["PI"].is_exported is True
    assert names["GREETING"].kind == "constant"
    assert names["GREETING"].is_exported is False


# ---- call sites ----

def test_call_site_free_function():
    src = (
        "fn caller() {\n"
        "    helper(1, 2, 3);\n"
        "}\n"
    )
    tree = _parse(src)
    sites = rust.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    assert ("helper", 3) in by_name


def test_call_site_type_constructor():
    """``Widget::new(args)`` should emit callee_name='Widget' for constructor arity."""
    src = (
        "fn caller() {\n"
        "    let w = Widget::new(\"hi\".to_string(), 80);\n"
        "}\n"
    )
    tree = _parse(src)
    sites = rust.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    assert ("Widget", 2) in by_name


def test_call_site_method_invocation():
    src = (
        "fn caller(w: Widget) {\n"
        "    let r = w.render();\n"
        "    w.send(1, 2);\n"
        "}\n"
    )
    tree = _parse(src)
    sites = rust.extract_call_sites(tree, src)
    by_name = {(s.callee_name, s.arg_count) for s in sites}
    assert ("render", 0) in by_name
    assert ("send", 2) in by_name


def test_call_site_tuple_struct_construction():
    src = "fn make() -> Point { Point(1, 2) }\n"
    tree = _parse(src)
    sites = rust.extract_call_sites(tree, src)
    assert any(s.callee_name == "Point" and s.arg_count == 2 for s in sites)


# ---- resolution ----

def test_resolves_stdlib():
    imp = ImportInfo(module="std::collections", imported_names=["HashMap"])
    assert rust.resolves(imp, "/tmp", set(), {}) is True
    assert rust.resolves(
        ImportInfo(module="core::mem"), "/tmp", set(), {}
    ) is True


def test_resolves_authored_crate_module():
    scaffolded = {"src/widget.rs", "src/sub/helper.rs"}
    imp = ImportInfo(module="crate::widget", imported_names=["Widget"])
    assert rust.resolves(imp, "/tmp", scaffolded, {}) is True

    sub = ImportInfo(module="crate::sub::helper", imported_names=["Helper"])
    assert rust.resolves(sub, "/tmp", scaffolded, {}) is True


def test_unresolved_crate_submodule():
    scaffolded = {"src/widget.rs"}
    imp = ImportInfo(module="crate::widget::sub", imported_names=["X"])
    assert rust.resolves(imp, "/tmp", scaffolded, {}) is False


def test_super_self_lenient():
    imp = ImportInfo(module="super::sibling", imported_names=["X"], is_relative=True)
    assert rust.resolves(imp, "/tmp", set(), {}) is True


def test_resolves_cargo_toml_dep():
    with tempfile.TemporaryDirectory() as tmp:
        cargo = os.path.join(tmp, "Cargo.toml")
        with open(cargo, "w") as f:
            f.write(
                "[package]\nname = \"demo\"\n\n"
                "[dependencies]\n"
                "serde = \"1.0\"\n"
                "tokio = { version = \"1\", features = [\"full\"] }\n"
            )
        imp = ImportInfo(module="serde", imported_names=[])
        assert rust.resolves(imp, tmp, set(), {}) is True
        imp2 = ImportInfo(module="tokio::sync", imported_names=["Mutex"])
        assert rust.resolves(imp2, tmp, set(), {}) is True
        imp3 = ImportInfo(module="not_a_dep", imported_names=[])
        assert rust.resolves(imp3, tmp, set(), {}) is False
