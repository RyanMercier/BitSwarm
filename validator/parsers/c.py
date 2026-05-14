"""
C language parser, backed by tree-sitter-c.

There is no canonical "module" or "package" in C: ``#include`` is a
textual substitution. We model it the same way as the other languages
anyway so the common contract checker keeps working:

  - ``module_path_for_file(p)`` returns the repo-relative path with
    forward slashes (e.g. ``"src/widget.h"``).
  - ``#include "widget.h"`` from ``src/main.c`` is normalized to
    ``module="src/widget.h"``, ``imported_names=[]``, ``is_relative=True``.
  - ``#include <stdio.h>`` is ``module="stdio.h"``, ``imported_names=[]``,
    ``is_relative=False`` — always resolves.

A header doesn't list which names are imported by an ``#include``
(everything declared in the header is in scope after inclusion). The
common arity check expands bare-module imports to "every exported class
in the target", so cross-file constructor / function arity still fires
on stack-construction and ``new`` call sites in C++ that consume a C
header.

Definitions extracted:

  - ``function_definition`` / forward ``declaration`` with a
    ``function_declarator``: registered as ``CallableInfo(kind="function")``.
  - ``struct_specifier`` and ``union_specifier`` with a type tag:
    registered as ``CallableInfo(kind="class")`` so the arity rules can
    treat stack-construction the same way as elsewhere. C structs don't
    have constructors, so ``params`` stays empty and the arity check
    only fires when explicit init syntax is used.
  - ``type_definition`` (``typedef``): the typedef name is registered as
    ``"type"``; if the typedef wraps an anonymous struct, the typedef
    name is registered as ``"class"``.
  - ``enum_specifier`` with a type tag: ``CallableInfo(kind="enum")``.
"""
from __future__ import annotations

import os
from typing import Any

import tree_sitter_c as tsc
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


_LANGUAGE = Language(tsc.language())


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _find_first(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_all(node, type_name: str):
    return [c for c in node.children if c.type == type_name]


def _func_name_from_declarator(declarator) -> str | None:
    """Walk a ``function_declarator`` / ``pointer_declarator`` tree to find the name.

    The function name lives at the leaf as an ``identifier`` or
    ``field_identifier``. Pointer-returning functions (``int *foo()``)
    wrap the declarator in a ``pointer_declarator``; nested arrays and
    function pointers nest similarly. Walking the first matching child
    each level converges on the name.
    """
    while declarator is not None:
        if declarator.type in ("identifier", "field_identifier"):
            return _node_text(declarator)
        # Look for a nested declarator or an identifier child.
        next_node = None
        for c in declarator.children:
            if c.type in ("function_declarator", "pointer_declarator",
                          "array_declarator", "parenthesized_declarator"):
                next_node = c
                break
            if c.type in ("identifier", "field_identifier"):
                return _node_text(c)
        declarator = next_node
    return None


def _params_from_parameter_list(param_list) -> tuple[list[ParamInfo], bool]:
    """Extract param names; mark variadic if ``...`` is present.

    C parameter syntax: ``parameter_declaration`` has the type as a
    sibling of an ``identifier``. We pull the identifier; if it's
    missing (declaration like ``void f(int);``) we leave the slot
    unnamed (empty string) so the count is still right.

    ``(void)`` is special-cased to mean "zero params" — the C convention
    for an empty parameter list.
    """
    params: list[ParamInfo] = []
    has_varargs = False
    # Detect explicit (void).
    decls = _find_all(param_list, "parameter_declaration")
    if len(decls) == 1:
        only = decls[0]
        type_only = (len(only.children) == 1
                     and only.children[0].type == "primitive_type"
                     and _node_text(only.children[0]) == "void")
        if type_only:
            return [], False

    for c in param_list.children:
        if c.type == "parameter_declaration":
            name_node = None
            for sub in c.children:
                if sub.type == "identifier":
                    name_node = sub
                    break
                if sub.type in ("pointer_declarator", "array_declarator"):
                    name_node = _find_first_identifier(sub)
                    if name_node is not None:
                        break
            name = _node_text(name_node) if name_node is not None else ""
            params.append(ParamInfo(name=name, has_default=False))
        elif c.type == "variadic_parameter":
            has_varargs = True
    return params, has_varargs


def _find_first_identifier(node):
    """DFS for the first ``identifier`` descendant."""
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            return n
        stack.extend(reversed(n.children))
    return None


def _count_args(arg_list_node) -> int:
    count = 0
    for c in arg_list_node.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count


def _resolve_relative_include(target: str, importer_path: str) -> str:
    """Join a quoted ``#include "x.h"`` against the importer's directory."""
    importer_dir = os.path.dirname(importer_path.replace("\\", "/"))
    joined = os.path.normpath(os.path.join(importer_dir, target)) if importer_dir \
        else os.path.normpath(target)
    return joined.replace("\\", "/")


def extract_c_like_includes(tree, importer_path: str) -> list[ImportInfo]:
    """Shared with ``cpp.py`` — preprocessor includes work the same in both."""
    imports: list[ImportInfo] = []
    for child in tree.root_node.children:
        if child.type != "preproc_include":
            continue
        sys_node = _find_first(child, "system_lib_string")
        if sys_node is not None:
            raw = _node_text(sys_node).strip("<>")
            imports.append(ImportInfo(
                module=raw, imported_names=[],
                line=child.start_point[0] + 1,
                is_relative=False, raw=_node_text(child),
            ))
            continue
        str_node = _find_first(child, "string_literal")
        if str_node is not None:
            frag = _find_first(str_node, "string_content")
            if frag is None:
                continue
            target = _node_text(frag)
            module = _resolve_relative_include(target, importer_path) \
                if importer_path else target
            imports.append(ImportInfo(
                module=module, imported_names=[],
                line=child.start_point[0] + 1,
                is_relative=True, raw=_node_text(child),
            ))
    return imports


def resolve_c_like_include(imp: ImportInfo, repo_path: str,
                            scaffolded_files: set[str],
                            project_manifest: dict,
                            extensions: tuple[str, ...]) -> bool:
    """Shared with ``cpp.py``. System includes always resolve; local
    includes must match a scaffolded path or an on-disk file."""
    mod = imp.module
    if not mod:
        return True
    if not imp.is_relative:
        # System / angle-bracket include: assume external (stdlib, system
        # libs, project-level -I paths the validator can't see).
        return True
    # Local include: must exist as a scaffolded header or on disk.
    if mod in scaffolded_files:
        return True
    # Some projects ship the impl with the same stem.
    stem, _ = os.path.splitext(mod)
    for ext in extensions:
        if (stem + ext) in scaffolded_files:
            return True
    if os.path.isfile(os.path.join(repo_path, mod)):
        return True
    return False


# ---- C parser ----

_C_EXTS = (".c", ".h")
_C_HEADER_EXTS = (".h",)


class CParser:
    """LanguageParser implementation backed by tree-sitter-c."""

    name = "c"
    extensions = _C_EXTS

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
        # Walk into preprocessor conditional blocks. Header guards
        # (``#ifndef X / #define X / ... / #endif``) wrap everything
        # in ``preproc_ifdef``, so anything we care about nests one
        # level deeper than a naked source file.
        if node.type in ("preproc_ifdef", "preproc_if", "preproc_else",
                         "preproc_elif", "linkage_specification"):
            for c in node.children:
                self._collect(c, out)
            return
        if node.type == "function_definition":
            self._collect_function(node, out)
        elif node.type == "declaration":
            # A forward declaration like ``int foo(int x);`` shows up as a
            # ``declaration`` containing a ``function_declarator``. Treat it
            # as the function's canonical signature if no definition has
            # been seen yet — header files only carry declarations.
            decl = _find_first(node, "function_declarator")
            if decl is not None:
                self._collect_function_from_declarator(node, decl, out)
        elif node.type == "type_definition":
            self._collect_typedef(node, out)
        elif node.type == "struct_specifier":
            self._collect_struct(node, out, kind="class")
        elif node.type == "union_specifier":
            self._collect_struct(node, out, kind="class")
        elif node.type == "enum_specifier":
            self._collect_enum(node, out)

    def _collect_function(self, node, out: dict[str, CallableInfo]) -> None:
        decl = _find_first(node, "function_declarator") \
            or _find_first(node, "pointer_declarator")
        if decl is None:
            return
        self._collect_function_from_declarator(node, decl, out)

    def _collect_function_from_declarator(self, node, declarator,
                                           out: dict[str, CallableInfo]) -> None:
        # Walk down to find the actual function_declarator (might be wrapped).
        fd = declarator
        while fd is not None and fd.type != "function_declarator":
            inner = None
            for c in fd.children:
                if c.type in ("function_declarator", "pointer_declarator",
                              "parenthesized_declarator"):
                    inner = c
                    break
            fd = inner
        if fd is None:
            return
        name = _func_name_from_declarator(fd)
        if not name:
            return
        param_list = _find_first(fd, "parameter_list")
        params, has_varargs = (
            _params_from_parameter_list(param_list) if param_list is not None
            else ([], False)
        )
        # If the function is already registered (declaration first, then
        # definition), keep the version with params filled in.
        existing = out.get(name)
        new_info = CallableInfo(
            kind="function", name=name,
            params=params, has_varargs=has_varargs,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_exported=not _is_static(node),
        )
        if existing is None or (not existing.params and params):
            out[name] = new_info

    def _collect_typedef(self, node, out: dict[str, CallableInfo]) -> None:
        # ``typedef struct {...} Name;`` or ``typedef int MyInt;``
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
            # typedef-struct: register the typedef name as a class so
            # downstream arity checks can refer to it.
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

    def _collect_struct(self, node, out: dict[str, CallableInfo], *, kind: str) -> None:
        name_node = _find_first(node, "type_identifier")
        if name_node is None:
            return  # anonymous struct (handled by typedef wrapper if any)
        name = _node_text(name_node)
        out[name] = CallableInfo(
            kind=kind, name=name,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )

    def _collect_enum(self, node, out: dict[str, CallableInfo]) -> None:
        name_node = _find_first(node, "type_identifier")
        if name_node is None:
            return
        name = _node_text(name_node)
        out[name] = CallableInfo(
            kind="enum", name=name,
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
                callee = _find_first(node, "identifier") \
                    or _find_first(node, "field_expression")
                args = _find_first(node, "argument_list")
                if callee is not None:
                    name = _callee_text(callee)
                    if name:
                        sites.append(CallSite(
                            callee_name=name,
                            arg_count=_count_args(args) if args is not None else 0,
                            line=node.start_point[0] + 1,
                        ))
            stack.extend(node.children)
        return sites

    # ---- resolution ----

    def resolves(self, imp: ImportInfo, repo_path: str,
                 scaffolded_files: set[str], project_manifest: dict) -> bool:
        return resolve_c_like_include(imp, repo_path, scaffolded_files,
                                       project_manifest, _C_EXTS)


def _is_static(function_def_node) -> bool:
    for c in function_def_node.children:
        if c.type == "storage_class_specifier" and "static" in _node_text(c):
            return True
    return False


def _callee_text(node) -> str | None:
    if node.type == "identifier":
        return _node_text(node)
    if node.type == "field_expression":
        # `obj.method` or `obj->method` — return the rightmost field_identifier
        for c in reversed(node.children):
            if c.type == "field_identifier":
                return _node_text(c)
    return None


parser: LanguageParser = CParser()
