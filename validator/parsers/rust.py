"""
Rust language parser, backed by tree-sitter-rust.

Module-path convention:
  src/lib.rs       -> ``crate``
  src/main.rs      -> ``crate``
  src/widget.rs    -> ``crate::widget``
  src/sub/mod.rs   -> ``crate::sub``
  src/sub/foo.rs   -> ``crate::sub::foo``

Imports are modeled the same way as Java/C#: ``use crate::widget::Widget;``
becomes ``module="crate::widget"`` + ``imported_names=["Widget"]``, so
the cross-file contract check keys by package and looks up the named
type in the target module.

Grouped imports (``use crate::widget::{Helper, Color};``) flatten into a
single ``ImportInfo`` with multiple names. Bare external crate imports
(``use external_crate;``) become ``module="external_crate"`` with empty
``imported_names``.

Definitions:
  - ``struct_item`` registers a class. Tuple structs (``struct Point(i32, i32);``)
    set ``params`` so the common arity check treats ``Point(1, 2)`` as a
    constructor call.
  - ``impl_item Widget { ... }`` walks back and attaches methods to the
    Widget class in the registry. ``self``/``&self``/``&mut self`` are
    dropped from the param list so call sites without an explicit
    receiver (``w.render()`` has 0 args) line up.
  - ``trait_item`` registers as ``kind="interface"`` with its method
    signatures.
  - ``enum_item`` registers as ``kind="enum"``.
  - ``type_item`` registers as ``kind="type"``.
  - ``function_item`` registers as ``kind="function"``.
  - ``mod_item`` with a body recurses one level (file-local nested
    modules; ``mod foo;`` without a body declares a sibling file we
    parse separately).

Call sites:
  - ``foo(args)`` -> callee_name="foo".
  - ``Widget::new(args)`` -> callee_name="Widget" so the arity check
    hits the class's constructor (via the ``new`` convention added to
    ``_constructor_for``). Type-qualified calls bias toward the type;
    module-qualified calls (``crate::helper::do_thing(args)``) bias
    toward the leaf function because the qualifier resolves to a
    nested scoped_identifier rather than a flat type.
  - ``obj.method(args)`` -> callee_name="method".
  - ``Point(1, 2)`` (tuple-struct construction) parses as a normal call
    expression; same path as a function call.

Resolution:
  - ``std::*``, ``core::*``, ``alloc::*``, ``test::*`` always resolve.
  - ``crate::xxx`` must match an authored module path.
  - ``self::`` / ``super::`` lenient (require knowing the importer's
    crate position to fully resolve; the contract check still catches
    missing names downstream).
  - Bare crate name resolves via ``Cargo.toml`` [dependencies] /
    [dev-dependencies] / [build-dependencies].
"""
from __future__ import annotations

import os
from typing import Any

import tree_sitter_rust as tsr
from tree_sitter import Language
from tree_sitter import Parser as TSParser

from validator.parsers.types import (
    CallableInfo,
    CallSite,
    ImportInfo,
    LanguageParser,
    ParamInfo,
    ParseError,
)


_LANGUAGE = Language(tsr.language())


# Rust stdlib & toolchain-provided top-level crates.
_RUST_STDLIB = {"std", "core", "alloc", "test", "proc_macro"}


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _find_first(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _count_args(arg_list_node) -> int:
    count = 0
    for c in arg_list_node.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count


class RustParser:
    """LanguageParser implementation backed by tree-sitter-rust."""

    name = "rust"
    extensions = (".rs",)

    def parse(self, source: str, filepath: str) -> Any:
        ts_parser = TSParser(_LANGUAGE)
        try:
            tree = ts_parser.parse(source.encode("utf-8"))
        except Exception as exc:
            raise ParseError(f"{filepath}: {exc}") from exc
        if tree.root_node.has_error:
            for child in tree.root_node.children:
                if child.type == "ERROR":
                    span = child.end_byte - child.start_byte
                    if span >= max(1, int(0.6 * len(source))):
                        raise ParseError(
                            f"{filepath}: unrecoverable parse error around "
                            f"line {child.start_point[0] + 1}"
                        )
        return tree

    def module_path_for_file(self, filepath: str,
                             tree: Any = None, source: str = "") -> str:
        p = filepath.replace("\\", "/")
        # Strip a leading ``src/`` (the Cargo convention). Tests under
        # ``tests/`` are integration tests; they're top-level too but
        # less commonly referenced from crate paths.
        if p.startswith("src/"):
            p = p[4:]
        if p.endswith(".rs"):
            p = p[:-3]
        if p.endswith("/mod"):
            p = p[: -len("/mod")]
        elif p == "mod":
            p = ""
        if p in ("lib", "main") or not p:
            return "crate"
        return "crate::" + "::".join(p.split("/"))

    # ---- imports ----

    def extract_imports(self, tree, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        out: list[ImportInfo] = []
        for child in tree.root_node.children:
            if child.type != "use_declaration":
                continue
            out.extend(self._parse_use(child))
        return out

    def _parse_use(self, node) -> list[ImportInfo]:
        for c in node.children:
            if c.type == "scoped_identifier":
                return [self._import_from_scoped(c, node)]
            if c.type == "scoped_use_list":
                return self._import_from_use_list(c, node)
            if c.type == "use_list":
                # ``use {a, b};`` — bare top-level grouped use (rare).
                names = [_node_text(x) for x in c.children if x.type == "identifier"]
                return [ImportInfo(
                    module="", imported_names=names,
                    line=node.start_point[0] + 1,
                    is_relative=False, raw=_node_text(node),
                )]
            if c.type == "identifier":
                # ``use external_crate;``
                return [ImportInfo(
                    module=_node_text(c), imported_names=[],
                    line=node.start_point[0] + 1,
                    is_relative=False, raw=_node_text(node),
                )]
            if c.type == "use_as_clause":
                # ``use crate::widget::Widget as W;``
                return self._import_from_as_clause(c, node)
        return []

    def _import_from_scoped(self, scoped_node, use_node) -> ImportInfo:
        full = _node_text(scoped_node)
        parts = full.split("::")
        if len(parts) < 2:
            return ImportInfo(
                module=full, imported_names=[],
                line=use_node.start_point[0] + 1,
                is_relative=False, raw=_node_text(use_node),
            )
        module = "::".join(parts[:-1])
        return ImportInfo(
            module=module,
            imported_names=[parts[-1]],
            line=use_node.start_point[0] + 1,
            is_relative=parts[0] in ("self", "super"),
            raw=_node_text(use_node),
        )

    def _import_from_use_list(self, scoped_list_node, use_node) -> list[ImportInfo]:
        # children: scoped_identifier "::" use_list
        path_node = _find_first(scoped_list_node, "scoped_identifier") \
            or _find_first(scoped_list_node, "identifier")
        list_node = _find_first(scoped_list_node, "use_list")
        if path_node is None or list_node is None:
            return []
        path = _node_text(path_node)
        names: list[str] = []
        for c in list_node.children:
            if c.type == "identifier":
                names.append(_node_text(c))
            elif c.type == "use_as_clause":
                # ``use crate::x::{A as B}``: keep the original name A.
                first_ident = _find_first(c, "identifier")
                if first_ident is not None:
                    names.append(_node_text(first_ident))
            elif c.type == "scoped_identifier":
                # nested: ``use crate::x::{a::b}`` — flatten as best-effort
                inner_full = _node_text(c)
                inner_parts = inner_full.split("::")
                names.append(inner_parts[-1])
        return [ImportInfo(
            module=path, imported_names=names,
            line=use_node.start_point[0] + 1,
            is_relative=path.startswith("self") or path.startswith("super"),
            raw=_node_text(use_node),
        )]

    def _import_from_as_clause(self, as_node, use_node) -> list[ImportInfo]:
        scoped = _find_first(as_node, "scoped_identifier")
        if scoped is not None:
            return [self._import_from_scoped(scoped, use_node)]
        ident = _find_first(as_node, "identifier")
        if ident is not None:
            return [ImportInfo(
                module=_node_text(ident), imported_names=[],
                line=use_node.start_point[0] + 1,
                is_relative=False, raw=_node_text(use_node),
            )]
        return []

    # ---- definitions ----

    def extract_defined_names(self, tree, source: str) -> dict[str, CallableInfo]:
        out: dict[str, CallableInfo] = {}
        # Pass 1: types and free functions.
        for child in tree.root_node.children:
            self._collect_def(child, out)
        # Pass 2: attach impl methods to existing types.
        for child in tree.root_node.children:
            if child.type == "impl_item":
                self._attach_impl(child, out)
        return out

    def _collect_def(self, node, out: dict[str, CallableInfo]) -> None:
        if node.type == "function_item":
            self._collect_function(node, out)
            return
        if node.type == "struct_item":
            self._collect_struct(node, out)
            return
        if node.type == "enum_item":
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="enum", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=_is_pub(node),
                )
            return
        if node.type == "union_item":
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="class", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=_is_pub(node),
                )
            return
        if node.type == "trait_item":
            self._collect_trait(node, out)
            return
        if node.type == "type_item":
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="type", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=_is_pub(node),
                )
            return
        if node.type == "mod_item":
            # ``mod foo { ... }`` — recurse into the body. ``mod foo;``
            # without a body declares a sibling file, handled by walking
            # the scaffolded path set separately.
            body = _find_first(node, "declaration_list")
            if body is not None:
                for c in body.children:
                    self._collect_def(c, out)
            return
        if node.type == "const_item" or node.type == "static_item":
            name_node = _find_first(node, "identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="constant", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=_is_pub(node),
                )

    def _collect_function(self, node, out: dict[str, CallableInfo]) -> None:
        name_node = _find_first(node, "identifier")
        if name_node is None:
            return
        name = _node_text(name_node)
        param_list = _find_first(node, "parameters")
        params, has_varargs = (
            _params_from_parameters(param_list) if param_list is not None
            else ([], False)
        )
        out[name] = CallableInfo(
            kind="function", name=name,
            params=params, has_varargs=has_varargs,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_exported=_is_pub(node),
        )

    def _collect_struct(self, node, out: dict[str, CallableInfo]) -> None:
        name_node = _find_first(node, "type_identifier")
        if name_node is None:
            return
        name = _node_text(name_node)
        params: list[ParamInfo] = []
        ordered = _find_first(node, "ordered_field_declaration_list")
        if ordered is not None:
            # Tuple struct: fields are positional. Synthesize names
            # ``_0``, ``_1``, ... so the arity check has something to
            # display in the error message.
            i = 0
            for c in ordered.children:
                if c.type in ("(", ")", ","):
                    continue
                params.append(ParamInfo(name=f"_{i}", has_default=False))
                i += 1
        out[name] = CallableInfo(
            kind="class", name=name,
            params=params,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_exported=_is_pub(node),
        )

    def _collect_trait(self, node, out: dict[str, CallableInfo]) -> None:
        name_node = _find_first(node, "type_identifier")
        if name_node is None:
            return
        name = _node_text(name_node)
        body = _find_first(node, "declaration_list")
        methods: dict[str, CallableInfo] = {}
        if body is not None:
            for c in body.children:
                if c.type in ("function_signature_item", "function_item"):
                    mname_node = _find_first(c, "identifier")
                    if mname_node is None:
                        continue
                    mname = _node_text(mname_node)
                    param_list = _find_first(c, "parameters")
                    params, has_varargs = (
                        _params_from_parameters(param_list, drop_self=True)
                        if param_list is not None else ([], False)
                    )
                    methods[mname] = CallableInfo(
                        kind="method", name=mname,
                        params=params, has_varargs=has_varargs,
                        line_start=c.start_point[0] + 1,
                        line_end=c.end_point[0] + 1,
                    )
        out[name] = CallableInfo(
            kind="interface", name=name,
            methods=methods,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_exported=_is_pub(node),
        )

    def _attach_impl(self, node, out: dict[str, CallableInfo]) -> None:
        # `impl <Type>` or `impl <Trait> for <Type>`. The target type is
        # the second type_identifier when ``for`` is present, otherwise
        # the first.
        type_nodes = [c for c in node.children
                       if c.type in ("type_identifier", "scoped_type_identifier",
                                      "generic_type")]
        target_name: str | None = None
        if not type_nodes:
            return
        # If `for` is in children, the second type ref is the target.
        has_for = any(c.type == "for" for c in node.children)
        target = type_nodes[1] if (has_for and len(type_nodes) > 1) else type_nodes[0]
        if target.type == "generic_type":
            inner = _find_first(target, "type_identifier")
            target_name = _node_text(inner) if inner is not None else None
        elif target.type == "scoped_type_identifier":
            # Take the rightmost identifier-like child.
            for c in reversed(target.children):
                if c.type in ("type_identifier", "identifier"):
                    target_name = _node_text(c)
                    break
        else:
            target_name = _node_text(target)
        if not target_name:
            return

        existing = out.get(target_name)
        # If the type lives in another file (cross-file impl), we just
        # silently drop the methods — the registry lookup happens by
        # module name, and impls are commonly siblings of the type
        # anyway.
        if existing is None or existing.kind != "class":
            return

        body = _find_first(node, "declaration_list")
        if body is None:
            return
        for c in body.children:
            if c.type != "function_item":
                continue
            mname_node = _find_first(c, "identifier")
            if mname_node is None:
                continue
            mname = _node_text(mname_node)
            param_list = _find_first(c, "parameters")
            params, has_varargs = (
                _params_from_parameters(param_list, drop_self=True)
                if param_list is not None else ([], False)
            )
            method_info = CallableInfo(
                kind="method", name=mname,
                params=params, has_varargs=has_varargs,
                line_start=c.start_point[0] + 1,
                line_end=c.end_point[0] + 1,
            )
            existing.methods[mname] = method_info
            # Rust's idiomatic constructor is ``Type::new(...)``. Mirror
            # it under the class name so the language-agnostic
            # ``_constructor_for`` (which looks for ``cls.name`` in
            # methods) finds it without needing a generic ``new`` rule
            # that would mis-fire on Python/TS classes with a regular
            # method named ``new``.
            if mname == "new" and target_name not in existing.methods:
                existing.methods[target_name] = method_info

    # ---- call sites ----

    def extract_call_sites(self, tree, source: str) -> list[CallSite]:
        sites: list[CallSite] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "call_expression":
                callee = self._callee_name(node)
                args = _find_first(node, "arguments")
                if callee:
                    sites.append(CallSite(
                        callee_name=callee,
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            stack.extend(node.children)
        return sites

    def _callee_name(self, call_node) -> str | None:
        for c in call_node.children:
            if c.type == "identifier":
                return _node_text(c)
            if c.type == "scoped_identifier":
                # ``X::new(args)``: the first direct child is the
                # qualifier. If the qualifier is a flat identifier
                # (i.e. one ``::`` deep, like ``Widget::new``), bias
                # toward the qualifier so the arity check finds the
                # type's constructor. Deeper qualifiers
                # (``crate::helper::do_thing``) fall back to the leaf,
                # which is typically a free function.
                children_idents = [x for x in c.children if x.type == "identifier"]
                if len(c.children) and c.children[0].type == "identifier" \
                        and len(children_idents) == 2:
                    return _node_text(children_idents[0])
                # leaf — last identifier
                for sub in reversed(c.children):
                    if sub.type == "identifier":
                        return _node_text(sub)
            elif c.type == "field_expression":
                for sub in reversed(c.children):
                    if sub.type == "field_identifier":
                        return _node_text(sub)
            elif c.type == "generic_function":
                ident = _find_first(c, "identifier")
                if ident is not None:
                    return _node_text(ident)
                scoped = _find_first(c, "scoped_identifier")
                if scoped is not None:
                    children_idents = [x for x in scoped.children if x.type == "identifier"]
                    if children_idents:
                        return _node_text(children_idents[0])
        return None

    # ---- resolution ----

    def resolves(self, imp: ImportInfo, repo_path: str,
                 scaffolded_files: set[str], project_manifest: dict) -> bool:
        mod = imp.module
        if not mod:
            return True
        top = mod.split("::")[0]
        if top in _RUST_STDLIB:
            return True
        if top == "crate":
            authored = self._authored_modules(scaffolded_files)
            if mod in authored:
                return True
            # Sub-module of an authored module that we don't actually have.
            for am in authored:
                if mod.startswith(am + "::"):
                    return False
            return False
        if top in ("self", "super"):
            # Resolving these properly requires knowing the importer's
            # position in the module tree. Treat as resolved; the
            # missing-name check still fires if the named symbol isn't
            # in the target registry.
            return True
        deps = self._cargo_deps(repo_path)
        deps.update(project_manifest.get("dependencies", {}) or {})
        if top in deps:
            return True
        return False

    def _authored_modules(self, scaffolded_files: set[str]) -> set[str]:
        out: set[str] = set()
        for path in scaffolded_files:
            if not path.endswith(".rs"):
                continue
            mod = self.module_path_for_file(path)
            if mod:
                out.add(mod)
        return out

    def _cargo_deps(self, repo_path: str) -> dict[str, str]:
        cargo = os.path.join(repo_path, "Cargo.toml")
        if not os.path.isfile(cargo):
            return {}
        deps: dict[str, str] = {}
        in_deps = False
        try:
            with open(cargo) as f:
                for line in f:
                    raw = line.strip()
                    if not raw or raw.startswith("#"):
                        continue
                    if raw.startswith("[") and raw.endswith("]"):
                        section = raw[1:-1].strip()
                        in_deps = section in (
                            "dependencies", "dev-dependencies", "build-dependencies"
                        ) or section.endswith("dependencies")
                        continue
                    if in_deps and "=" in raw:
                        name = raw.split("=", 1)[0].strip()
                        if name:
                            deps[name] = ""
        except OSError:
            pass
        return deps


def _is_pub(node) -> bool:
    return _find_first(node, "visibility_modifier") is not None


def _params_from_parameters(param_list,
                              drop_self: bool = False) -> tuple[list[ParamInfo], bool]:
    """Extract Rust parameters.

    ``drop_self`` controls whether a leading ``self``/``&self``/``&mut self``
    receiver is included in the param list. Method call sites in Rust
    don't pass the receiver explicitly (``w.render()`` has 0 args), so
    the registry's method entry should have it dropped.
    """
    params: list[ParamInfo] = []
    has_varargs = False
    for c in param_list.children:
        if c.type == "parameter":
            name_node = _find_first(c, "identifier")
            if name_node is not None:
                params.append(ParamInfo(
                    name=_node_text(name_node), has_default=False
                ))
        elif c.type == "self_parameter":
            if not drop_self:
                params.append(ParamInfo(name="self", has_default=False))
        elif c.type == "variadic_parameter":
            has_varargs = True
    return params, has_varargs


parser: LanguageParser = RustParser()
