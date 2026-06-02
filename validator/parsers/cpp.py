"""
C++ language parser, backed by tree-sitter-cpp.

Shares ``#include`` extraction and the function-declarator walk with
``parsers/c.py``. Adds:

  - ``namespace_definition``: recurses into the body so types declared
    inside ``namespace ns { ... }`` are visible at the registry layer.
    Names are NOT FQN-prefixed; the contract checker keys by file
    module, and we treat the file path as the module just like for C.
  - ``class_specifier`` / ``struct_specifier``: full classes with
    methods. Methods come from ``function_definition`` (inline) and
    ``declaration`` / ``field_declaration`` (signature-only) inside the
    ``field_declaration_list``. Access specifiers (``public:`` /
    ``private:``) are not enforced  -  anything visible in the body is
    counted, because cross-file privacy checks aren't the point of
    Phase 1.5.
  - ``alias_declaration``: ``using MyInt = int;``.
  - ``new_expression`` and stack-construction (``Widget w(args);``) as
    call sites in addition to ``call_expression``.
"""
from __future__ import annotations

import os
from typing import Any

import tree_sitter_cpp as tscpp
from tree_sitter import Language
from tree_sitter import Parser as TSParser

from validator.parsers.c import (
    _count_args,
    _find_first,
    _find_first_identifier,
    _func_name_from_declarator,
    _node_text,
    _params_from_parameter_list,
    extract_c_like_includes,
    resolve_c_like_include,
)
from validator.parsers.types import (
    CallableInfo,
    CallSite,
    ImportInfo,
    LanguageParser,
    ParamInfo,
    ParseError,
)


_LANGUAGE = Language(tscpp.language())

_CPP_EXTS = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h")


class CppParser:
    """LanguageParser implementation backed by tree-sitter-cpp."""

    name = "cpp"
    extensions = _CPP_EXTS

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
        return filepath.replace("\\", "/")

    # ---- imports ----

    def extract_imports(self, tree, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        return extract_c_like_includes(tree, filepath)

    # ---- definitions ----

    def extract_defined_names(self, tree, source: str) -> dict[str, CallableInfo]:
        out: dict[str, CallableInfo] = {}
        for child in tree.root_node.children:
            self._collect(child, out)
        return out

    def _collect(self, node, out: dict[str, CallableInfo]) -> None:
        # Preprocessor guards (``#ifndef WIDGET_HPP ... #endif``) and
        # ``extern "C" { ... }`` blocks nest declarations one level
        # below the translation unit. Recurse so we don't miss them.
        if node.type in ("preproc_ifdef", "preproc_if", "preproc_else",
                         "preproc_elif", "linkage_specification"):
            for c in node.children:
                self._collect(c, out)
            return
        if node.type == "namespace_definition":
            # Recurse into the body  -  types declared inside are visible
            # at the file level for our purposes.
            body = _find_first(node, "declaration_list")
            if body is not None:
                for c in body.children:
                    self._collect(c, out)
            return
        if node.type == "class_specifier":
            self._collect_class(node, out, kind="class")
            return
        if node.type == "struct_specifier":
            # A bare struct (not inside a typedef)  -  register as a class.
            name_node = _find_first(node, "type_identifier")
            if name_node is None:
                return
            # If the struct has a field_declaration_list with methods,
            # treat it like a class; otherwise it's a plain data struct.
            body = _find_first(node, "field_declaration_list")
            if body is not None and _has_methods(body):
                self._collect_class(node, out, kind="class")
            else:
                out[_node_text(name_node)] = CallableInfo(
                    kind="class", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            return
        if node.type == "union_specifier":
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="class", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            return
        if node.type == "enum_specifier":
            # Both `enum X` and `enum class X`.
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="enum", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            return
        if node.type == "type_definition":
            # ``typedef struct {...} Name;``  -  fall back to C-style logic.
            self._collect_typedef(node, out)
            return
        if node.type == "alias_declaration":
            # ``using MyInt = int;``
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                out[_node_text(name_node)] = CallableInfo(
                    kind="type", name=_node_text(name_node),
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            return
        if node.type == "function_definition":
            self._collect_function(node, out)
            return
        if node.type == "declaration":
            decl = _find_first(node, "function_declarator")
            if decl is not None:
                self._collect_function_from_declarator(node, decl, out)
            return
        if node.type == "template_declaration":
            # Recurse  -  the templated entity is one of the children.
            for c in node.children:
                self._collect(c, out)

    def _collect_class(self, node, out: dict[str, CallableInfo], *, kind: str) -> None:
        name_node = _find_first(node, "type_identifier")
        if name_node is None:
            return
        name = _node_text(name_node)
        body = _find_first(node, "field_declaration_list")
        methods: dict[str, CallableInfo] = {}
        if body is not None:
            for c in body.children:
                self._collect_class_member(c, name, methods)
        out[name] = CallableInfo(
            kind=kind, name=name,
            methods=methods,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )

    def _collect_class_member(self, node, class_name: str,
                                methods: dict[str, CallableInfo]) -> None:
        if node.type == "function_definition":
            self._collect_method(node, class_name, methods, is_def=True)
            return
        if node.type == "declaration":
            decl = _find_first(node, "function_declarator")
            if decl is not None:
                self._collect_method(node, class_name, methods, is_def=False)
            return
        if node.type == "field_declaration":
            # Method declaration with a return type lives here:
            #   ``std::string render() const;``
            decl = _find_first(node, "function_declarator")
            if decl is not None:
                self._collect_method(node, class_name, methods, is_def=False)

    def _collect_method(self, node, class_name: str,
                         methods: dict[str, CallableInfo], *, is_def: bool) -> None:
        decl = _find_first(node, "function_declarator")
        if decl is None:
            return
        name = _func_name_from_declarator(decl)
        if not name:
            return
        param_list = _find_first(decl, "parameter_list")
        params, has_varargs = (
            _params_from_parameter_list(param_list) if param_list is not None
            else ([], False)
        )
        info = CallableInfo(
            kind="method", name=name,
            params=params, has_varargs=has_varargs,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        # Constructors share the class name; keep the most informative
        # (definition trumps declaration when params line up).
        existing = methods.get(name)
        if existing is None or (not existing.params and params):
            methods[name] = info

    def _collect_function(self, node, out: dict[str, CallableInfo]) -> None:
        decl = _find_first(node, "function_declarator")
        if decl is None:
            return
        self._collect_function_from_declarator(node, decl, out)

    def _collect_function_from_declarator(self, node, declarator,
                                           out: dict[str, CallableInfo]) -> None:
        name = _func_name_from_declarator(declarator)
        if not name:
            return
        param_list = _find_first(declarator, "parameter_list")
        params, has_varargs = (
            _params_from_parameter_list(param_list) if param_list is not None
            else ([], False)
        )
        existing = out.get(name)
        new_info = CallableInfo(
            kind="function", name=name,
            params=params, has_varargs=has_varargs,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        if existing is None or (not existing.params and params):
            out[name] = new_info

    def _collect_typedef(self, node, out: dict[str, CallableInfo]) -> None:
        struct_node = (_find_first(node, "struct_specifier")
                       or _find_first(node, "union_specifier"))
        name_node = None
        for c in node.children:
            if c.type == "type_identifier":
                name_node = c
        if name_node is None:
            return
        name = _node_text(name_node)
        if struct_node is not None:
            out[name] = CallableInfo(
                kind="class", name=name,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        else:
            out[name] = CallableInfo(
                kind="type", name=name,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )

    # ---- call sites ----

    def extract_call_sites(self, tree, source: str) -> list[CallSite]:
        sites: list[CallSite] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "call_expression":
                callee = _cpp_callee(node)
                args = _find_first(node, "argument_list")
                if callee is not None:
                    sites.append(CallSite(
                        callee_name=callee,
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            elif node.type == "new_expression":
                # ``new ns::Widget(args)``  -  pull the rightmost type.
                type_node = _new_target_type(node)
                args = _find_first(node, "argument_list")
                if type_node:
                    sites.append(CallSite(
                        callee_name=type_node,
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            elif node.type == "declaration":
                # Stack construction ``Widget w("a", 80);`` shows up as a
                # ``declaration`` whose type ref is a ``type_identifier`` /
                # ``qualified_identifier`` and whose ``init_declarator``
                # carries an ``argument_list``.
                self._maybe_record_stack_construction(node, sites)
            stack.extend(node.children)
        return sites

    def _maybe_record_stack_construction(self, decl_node, sites: list[CallSite]) -> None:
        type_name = None
        for c in decl_node.children:
            if c.type == "type_identifier":
                type_name = _node_text(c)
                break
            if c.type == "qualified_identifier":
                # rightmost identifier-like token
                for sub in reversed(c.children):
                    if sub.type in ("type_identifier", "identifier"):
                        type_name = _node_text(sub)
                        break
                break
        if not type_name:
            return
        for c in decl_node.children:
            if c.type != "init_declarator":
                continue
            args = _find_first(c, "argument_list")
            if args is None:
                continue
            sites.append(CallSite(
                callee_name=type_name,
                arg_count=_count_args(args),
                line=decl_node.start_point[0] + 1,
            ))

    # ---- resolution ----

    def resolves(self, imp: ImportInfo, repo_path: str,
                 scaffolded_files: set[str], project_manifest: dict) -> bool:
        return resolve_c_like_include(imp, repo_path, scaffolded_files,
                                       project_manifest, _CPP_EXTS)


def _cpp_callee(call_node) -> str | None:
    for c in call_node.children:
        if c.type == "identifier":
            return _node_text(c)
        if c.type == "field_expression":
            for sub in reversed(c.children):
                if sub.type == "field_identifier":
                    return _node_text(sub)
        if c.type == "qualified_identifier":
            for sub in reversed(c.children):
                if sub.type in ("identifier", "type_identifier"):
                    return _node_text(sub)
        if c.type == "template_function":
            for sub in c.children:
                if sub.type == "identifier":
                    return _node_text(sub)
    return None


def _new_target_type(new_node) -> str | None:
    for c in new_node.children:
        if c.type == "type_identifier":
            return _node_text(c)
        if c.type == "qualified_identifier":
            for sub in reversed(c.children):
                if sub.type in ("type_identifier", "identifier"):
                    return _node_text(sub)
        if c.type == "template_type":
            for sub in c.children:
                if sub.type == "type_identifier":
                    return _node_text(sub)
    return None


def _has_methods(field_decl_list) -> bool:
    for c in field_decl_list.children:
        if c.type == "function_definition":
            return True
        if c.type in ("declaration", "field_declaration"):
            if _find_first(c, "function_declarator") is not None:
                return True
    return False


parser: LanguageParser = CppParser()
