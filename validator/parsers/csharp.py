"""
C# language parser, backed by tree-sitter-c-sharp.

Imports are ``using`` directives:

  - ``using System;``                  -> module="System",      names=[]
  - ``using System.Collections;``      -> module="System.Collections", names=[]
  - ``using static System.Math;``      -> module="System.Math", names=[]
  - ``using IO = System.IO;``          -> module="System.IO",   names=["IO"]

A bare ``using X;`` brings every public type in X into scope. The
contract check in ``validator_checks_common`` expands imports with empty
``imported_names`` to "every exported class in the module" so the arity
check still fires for ``new Widget(...)`` calls that go through such a
``using``.

Namespaces can be file-scoped (``namespace Demo;``) or block-scoped
(``namespace Demo { ... }``). Block namespaces nest their types inside a
``declaration_list`` child, so we recurse one level deep when collecting
top-level definitions.
"""
from __future__ import annotations

import json
import os
from typing import Any

import tree_sitter_c_sharp as tscs
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


_LANGUAGE = Language(tscs.language())


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _find_first(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_all(node, type_name: str):
    return [c for c in node.children if c.type == type_name]


def _path_to_namespace(filepath: str) -> str:
    """Path-based fallback for the file's namespace.

    Strips conventional source roots and the filename, returning the
    dotted directory path. Caller's namespace declaration takes priority
    when present.
    """
    p = filepath.replace("\\", "/")
    if p.endswith(".cs"):
        p = p[:-3]
    parts = p.split("/")
    # Drop obvious build-output dirs from the head.
    if parts and parts[0] in ("src", "Src", "source", "Source"):
        parts = parts[1:]
    return ".".join(parts[:-1]) if len(parts) > 1 else ""


class CSharpParser:
    """LanguageParser implementation backed by tree-sitter-c-sharp."""

    name = "csharp"
    extensions = (".cs",)

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
        if tree is not None:
            ns = self._first_namespace(tree.root_node)
            if ns:
                return ns
        return _path_to_namespace(filepath)

    def _first_namespace(self, root) -> str:
        """Return the dotted form of the first namespace declared in the file."""
        for child in root.children:
            if child.type == "file_scoped_namespace_declaration":
                name_node = (_find_first(child, "qualified_name")
                             or _find_first(child, "identifier"))
                if name_node is not None:
                    return _node_text(name_node).strip()
            elif child.type == "namespace_declaration":
                name_node = (_find_first(child, "qualified_name")
                             or _find_first(child, "identifier"))
                if name_node is not None:
                    return _node_text(name_node).strip()
        return ""

    # ---- imports ----

    def extract_imports(self, tree, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        imports: list[ImportInfo] = []
        for child in tree.root_node.children:
            if child.type != "using_directive":
                continue
            imp = self._parse_using(child)
            if imp is not None:
                imports.append(imp)
        return imports

    def _parse_using(self, node) -> ImportInfo | None:
        # Structure:
        #   `using <name>;`                   -> name child
        #   `using static <name>;`            -> static + name child
        #   `using Alias = <name>;`           -> alias-id + `=` + name
        alias_name: str | None = None
        target: str | None = None
        saw_equals = False
        prior_identifier: str | None = None

        for c in node.children:
            if c.type == "=":
                saw_equals = True
                # The identifier seen before `=` was the alias.
                alias_name = prior_identifier
                prior_identifier = None
                continue
            if c.type == "qualified_name":
                target = _node_text(c).strip()
            elif c.type == "identifier":
                if saw_equals or target is None:
                    if not saw_equals:
                        # Track in case `=` follows.
                        prior_identifier = _node_text(c).strip()
                    target = _node_text(c).strip()

        if target is None:
            return None

        # An aliased ``using A = X.Y.Z;`` brings the *target* into scope
        # under a new local name. We don't model the alias as a "named
        # import" because the alias points at a whole namespace (or a
        # type whose method calls go through the rightmost identifier,
        # not the alias). Treating it as bare-namespace import keeps the
        # cross-file consistency check from emitting a spurious
        # "interface mismatch" for ``A`` (which never appears as a
        # defined name in registry[target]).
        return ImportInfo(
            module=target,
            imported_names=[],
            line=node.start_point[0] + 1,
            is_relative=False,
            raw=_node_text(node),
        )

    # ---- definitions ----

    def extract_defined_names(self, tree, source: str) -> dict[str, CallableInfo]:
        out: dict[str, CallableInfo] = {}
        for child in tree.root_node.children:
            self._collect(child, out)
        return out

    def _collect(self, node, out: dict[str, CallableInfo]) -> None:
        if node.type == "namespace_declaration":
            body = _find_first(node, "declaration_list")
            if body is not None:
                for c in body.children:
                    self._collect(c, out)
            return
        if node.type == "file_scoped_namespace_declaration":
            # file-scoped namespace: types come after the namespace stmt
            # as siblings of node at top level, not as children of node.
            return

        if node.type == "class_declaration":
            out[self._type_name(node)] = self._class_to_callable(node, kind="class")
            return
        if node.type == "interface_declaration":
            out[self._type_name(node)] = self._class_to_callable(node, kind="interface")
            return
        if node.type == "struct_declaration":
            out[self._type_name(node)] = self._class_to_callable(node, kind="class")
            return
        if node.type == "record_declaration":
            self._collect_record(node, out)
            return
        if node.type == "enum_declaration":
            name = self._type_name(node)
            out[name] = CallableInfo(
                kind="enum", name=name,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                is_exported=_is_public(node),
            )
            return

    def _type_name(self, node) -> str:
        ident = _find_first(node, "identifier")
        return _node_text(ident) if ident is not None else "<anon>"

    def _class_to_callable(self, node, *, kind: str) -> CallableInfo:
        name = self._type_name(node)
        body = _find_first(node, "declaration_list")
        methods: dict[str, CallableInfo] = {}
        if body is not None:
            for c in body.children:
                if c.type == "method_declaration":
                    mname_node = _find_first(c, "identifier")
                    if mname_node is None:
                        continue
                    mname = _node_text(mname_node)
                    params_node = _find_first(c, "parameter_list")
                    params, has_varargs = (
                        _params_from_list(params_node)
                        if params_node is not None else ([], False)
                    )
                    methods[mname] = CallableInfo(
                        kind="method", name=mname,
                        params=params, has_varargs=has_varargs,
                        line_start=c.start_point[0] + 1,
                        line_end=c.end_point[0] + 1,
                    )
                elif c.type == "constructor_declaration":
                    params_node = _find_first(c, "parameter_list")
                    params, has_varargs = (
                        _params_from_list(params_node)
                        if params_node is not None else ([], False)
                    )
                    methods[name] = CallableInfo(
                        kind="method", name=name,
                        params=params, has_varargs=has_varargs,
                        line_start=c.start_point[0] + 1,
                        line_end=c.end_point[0] + 1,
                    )
        return CallableInfo(
            kind=kind, name=name,
            methods=methods,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_exported=_is_public(node),
        )

    def _collect_record(self, node, out: dict[str, CallableInfo]) -> None:
        name = self._type_name(node)
        params_node = _find_first(node, "parameter_list")
        params, has_varargs = (
            _params_from_list(params_node)
            if params_node is not None else ([], False)
        )
        ctor = CallableInfo(
            kind="method", name=name,
            params=params, has_varargs=has_varargs,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        out[name] = CallableInfo(
            kind="class", name=name,
            params=params, has_varargs=has_varargs,
            methods={name: ctor},
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_exported=_is_public(node),
        )

    # ---- call sites ----

    def extract_call_sites(self, tree, source: str) -> list[CallSite]:
        sites: list[CallSite] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "object_creation_expression":
                # `new Widget(args)` — type is identifier or qualified_name.
                type_node = (_find_first(node, "identifier")
                             or _find_first(node, "qualified_name"))
                args = _find_first(node, "argument_list")
                if type_node is not None:
                    name = _node_text(type_node).split(".")[-1]
                    sites.append(CallSite(
                        callee_name=name,
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            elif node.type == "invocation_expression":
                callee = self._invocation_callee(node)
                args = _find_first(node, "argument_list")
                if callee:
                    sites.append(CallSite(
                        callee_name=callee,
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            stack.extend(node.children)
        return sites

    def _invocation_callee(self, node) -> str | None:
        for c in node.children:
            if c.type == "identifier":
                return _node_text(c)
            if c.type == "member_access_expression":
                # Rightmost identifier.
                for sub in reversed(c.children):
                    if sub.type == "identifier":
                        return _node_text(sub)
        return None

    # ---- resolution ----

    def resolves(self, imp: ImportInfo, repo_path: str,
                 scaffolded_files: set[str], project_manifest: dict) -> bool:
        mod = imp.module
        if not mod:
            return True

        top = mod.split(".")[0]

        # .NET stdlib + common Microsoft surface
        if top in {"System", "Microsoft"}:
            return True

        # Authored namespaces.
        authored = self._authored_namespaces(scaffolded_files)
        if mod in authored:
            return True
        for ns in authored:
            if mod.startswith(ns + "."):
                return False  # sub-namespace we don't define

        # Project-level NuGet references.
        deps = project_manifest.get("dependencies", []) or []
        for dep in deps:
            if mod == dep or mod.startswith(dep + "."):
                return True

        # Common third-party ecosystems
        if top in {"Newtonsoft", "Serilog", "FluentAssertions", "Xunit",
                    "NUnit", "Moq", "AutoFixture"}:
            return True

        return False

    def _authored_namespaces(self, scaffolded_files: set[str]) -> set[str]:
        """Collect namespaces declared in scaffolded ``.cs`` files."""
        out: set[str] = set()
        for path in scaffolded_files:
            if not path.endswith(".cs"):
                continue
            # We don't have the parsed tree here; use the path heuristic.
            ns = _path_to_namespace(path)
            if ns:
                out.add(ns)
        return out


def _is_public(node) -> bool:
    """A C# type is public if any of its ``modifier`` children is ``public``."""
    has_modifier = False
    for c in node.children:
        if c.type == "modifier":
            has_modifier = True
            if any(sub.type == "public" for sub in c.children):
                return True
    # Top-level types default to internal in C#, but we treat them as
    # importable across our scaffolded files since the validator only
    # sees one project.
    return not has_modifier


def _params_from_list(param_list_node) -> tuple[list[ParamInfo], bool]:
    params: list[ParamInfo] = []
    has_varargs = False
    # C# uses ``params`` followed by a type and identifier as siblings;
    # there's no enclosing node, so we track it via a one-shot flag.
    pending_params_keyword = False
    for c in param_list_node.children:
        if c.type == "params":
            pending_params_keyword = True
            continue
        if c.type == "parameter":
            name_node = _find_first(c, "identifier")
            has_default = any(sub.type == "=" for sub in c.children)
            if name_node is not None:
                params.append(ParamInfo(
                    name=_node_text(name_node), has_default=has_default
                ))
            pending_params_keyword = False
        elif c.type == "identifier" and pending_params_keyword:
            # `params <Type>[] <name>` — flat siblings after `params`.
            has_varargs = True
            pending_params_keyword = False
    return params, has_varargs


def _count_args(arg_list_node) -> int:
    count = 0
    for c in arg_list_node.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count


parser: LanguageParser = CSharpParser()
