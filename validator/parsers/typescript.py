"""
TypeScript / JavaScript language parser, backed by tree-sitter-typescript.

Handles ``.ts``, ``.tsx``, ``.js``, ``.jsx``, ``.mjs``, ``.cjs``.

TypeScript is a strict superset of JavaScript, so the TS grammar parses
both: type annotations on raw JS are a no-op rather than a syntax error.
If wild-JS code starts producing false negatives we can later split out
``.js``/``.jsx`` onto tree-sitter-javascript.

The ``.tsx`` and ``.jsx`` extensions go through ``language_tsx()`` so JSX
parses as expression syntax.
"""
from __future__ import annotations

import json
import os
from typing import Any

import tree_sitter_typescript as tst
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


_TS_LANGUAGE = Language(tst.language_typescript())
_TSX_LANGUAGE = Language(tst.language_tsx())

# Node.js built-in modules (no "node:" prefix variant).
_NODE_BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster",
    "console", "constants", "crypto", "dgram", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module",
    "net", "os", "path", "perf_hooks", "process", "punycode",
    "querystring", "readline", "repl", "stream", "string_decoder",
    "test", "timers", "tls", "trace_events", "tty", "url", "util",
    "v8", "vm", "wasi", "worker_threads", "zlib",
}

_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _find_first(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _find_all(node, type_name: str):
    return [c for c in node.children if c.type == type_name]


def _select_language(filepath: str) -> Language:
    if filepath.endswith(".tsx") or filepath.endswith(".jsx"):
        return _TSX_LANGUAGE
    return _TS_LANGUAGE


def _strip_known_ext(path: str) -> str:
    for ext in _TS_EXTS:
        if path.endswith(ext):
            return path[: -len(ext)]
    return path


class TypeScriptParser:
    """LanguageParser implementation backed by tree-sitter-typescript."""

    name = "typescript"
    extensions = _TS_EXTS

    def parse(self, source: str, filepath: str) -> Any:
        language = _select_language(filepath)
        ts_parser = TSParser(language)
        try:
            tree = ts_parser.parse(source.encode("utf-8"))
        except Exception as exc:
            raise ParseError(f"{filepath}: {exc}") from exc

        # tree-sitter doesn't raise on parse failure; it inserts ERROR
        # nodes. We're lenient: only refuse a tree that has a top-level
        # ERROR covering most of the source. That mirrors how the Python
        # validator only rejects ``SyntaxError``s that prevent parsing
        # rather than every lint-style issue.
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
        """Canonical module ref: forward slashes, no extension, no trailing /index."""
        path = filepath.replace("\\", "/")
        path = _strip_known_ext(path)
        if path.endswith("/index"):
            path = path[: -len("/index")]
        return path

    # ---- imports ----

    def extract_imports(self, tree, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        imports: list[ImportInfo] = []
        for child in tree.root_node.children:
            if child.type == "import_statement":
                imp = self._parse_import_statement(child, filepath)
                if imp is not None:
                    imports.append(imp)
        return imports

    def _parse_import_statement(self, node, importer_path: str) -> ImportInfo | None:
        source_path: str | None = None
        names: list[str] = []
        for c in node.children:
            if c.type == "string":
                frag = _find_first(c, "string_fragment")
                if frag is not None:
                    source_path = _node_text(frag)
            elif c.type == "import_clause":
                names.extend(self._parse_import_clause(c))

        if source_path is None:
            return None

        is_relative = source_path.startswith("./") or source_path.startswith("../")
        module = source_path
        if is_relative and importer_path:
            importer_dir = os.path.dirname(importer_path.replace("\\", "/"))
            joined = os.path.normpath(os.path.join(importer_dir, source_path))
            module = joined.replace("\\", "/")

        return ImportInfo(
            module=module,
            imported_names=names,
            line=node.start_point[0] + 1,
            is_relative=is_relative,
            raw=_node_text(node),
        )

    def _parse_import_clause(self, node) -> list[str]:
        """Collect the locally-bound names for an import clause.

        ``import D from 'm'``           -> ``['D']``
        ``import { a, b as c } from 'm'`` -> ``['a', 'b']`` (original names)
        ``import * as ns from 'm'``       -> ``['ns']``
        """
        names: list[str] = []
        for c in node.children:
            if c.type == "identifier":
                names.append(_node_text(c))
            elif c.type == "named_imports":
                for spec in c.children:
                    if spec.type == "import_specifier":
                        idents = _find_all(spec, "identifier")
                        if idents:
                            names.append(_node_text(idents[0]))
            elif c.type == "namespace_import":
                ident = _find_first(c, "identifier")
                if ident is not None:
                    names.append(_node_text(ident))
        return names

    # ---- definitions ----

    def extract_defined_names(self, tree, source: str) -> dict[str, CallableInfo]:
        out: dict[str, CallableInfo] = {}
        for child in tree.root_node.children:
            self._collect_top_level(child, out, exported=False)
        return out

    def _collect_top_level(self, node, out: dict[str, CallableInfo],
                            exported: bool) -> None:
        if node.type == "export_statement":
            for c in node.children:
                if c.type in ("export", "default"):
                    continue
                self._collect_top_level(c, out, exported=True)
            return

        if node.type == "function_declaration":
            info = self._function_to_callable(node, kind="function", exported=exported)
            out[info.name] = info
            return

        if node.type == "class_declaration":
            info = self._class_to_callable(node, exported=exported)
            out[info.name] = info
            return

        if node.type == "interface_declaration":
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                name = _node_text(name_node)
                methods = self._methods_from_interface_body(node)
                out[name] = CallableInfo(
                    kind="interface",
                    name=name,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    methods=methods,
                    is_exported=exported,
                )
            return

        if node.type == "type_alias_declaration":
            name_node = _find_first(node, "type_identifier")
            if name_node is not None:
                name = _node_text(name_node)
                out[name] = CallableInfo(
                    kind="type",
                    name=name,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=exported,
                )
            return

        if node.type == "enum_declaration":
            name_node = (_find_first(node, "identifier")
                         or _find_first(node, "type_identifier"))
            if name_node is not None:
                name = _node_text(name_node)
                out[name] = CallableInfo(
                    kind="enum",
                    name=name,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=exported,
                )
            return

        if node.type == "lexical_declaration":
            for c in node.children:
                if c.type == "variable_declarator":
                    name_node = _find_first(c, "identifier")
                    if name_node is None:
                        continue
                    name = _node_text(name_node)
                    # ``const handler = (req) => {...}`` and
                    # ``const handler = function(req) {...}`` are
                    # functions for arity-checking purposes, not
                    # opaque constants. Detect the rhs and promote.
                    fn_value = _find_first(c, "arrow_function") \
                        or _find_first(c, "function_expression")
                    if fn_value is not None:
                        params_node = _find_first(fn_value, "formal_parameters")
                        if params_node is None:
                            # Single-param shorthand: ``const f = x => x;``
                            # The parser puts the lone identifier as a
                            # direct child of the arrow_function.
                            params = []
                            has_rest = False
                            for sub in fn_value.children:
                                if sub.type == "identifier":
                                    params = [ParamInfo(name=_node_text(sub),
                                                        has_default=False)]
                                    break
                        else:
                            params, has_rest = _params_from_formal(params_node)
                        out[name] = CallableInfo(
                            kind="function",
                            name=name,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            params=params,
                            has_varargs=has_rest,
                            is_exported=exported,
                        )
                        continue
                    out[name] = CallableInfo(
                        kind="constant",
                        name=name,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        is_exported=exported,
                    )

    def _function_to_callable(self, node, *, kind: str,
                               exported: bool) -> CallableInfo:
        name_node = (_find_first(node, "identifier")
                     or _find_first(node, "property_identifier"))
        params_node = _find_first(node, "formal_parameters")
        params, has_rest = (
            _params_from_formal(params_node) if params_node is not None else ([], False)
        )
        return CallableInfo(
            kind=kind,
            name=_node_text(name_node) if name_node is not None else "<anon>",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            params=params,
            has_varargs=has_rest,
            is_exported=exported,
        )

    def _class_to_callable(self, node, *, exported: bool) -> CallableInfo:
        name_node = _find_first(node, "type_identifier")
        name = _node_text(name_node) if name_node is not None else "<anon>"
        body = _find_first(node, "class_body")
        methods: dict[str, CallableInfo] = {}
        if body is not None:
            for c in body.children:
                if c.type != "method_definition":
                    continue
                mname_node = _find_first(c, "property_identifier")
                mname = _node_text(mname_node) if mname_node is not None else "<anon>"
                params_node = _find_first(c, "formal_parameters")
                params, has_rest = (
                    _params_from_formal(params_node) if params_node is not None else ([], False)
                )
                methods[mname] = CallableInfo(
                    kind="method",
                    name=mname,
                    line_start=c.start_point[0] + 1,
                    line_end=c.end_point[0] + 1,
                    params=params,
                    has_varargs=has_rest,
                )
        return CallableInfo(
            kind="class",
            name=name,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            methods=methods,
            is_exported=exported,
        )

    def _methods_from_interface_body(self, interface_node) -> dict[str, CallableInfo]:
        body = _find_first(interface_node, "interface_body")
        out: dict[str, CallableInfo] = {}
        if body is None:
            return out
        for c in body.children:
            if c.type != "method_signature":
                continue
            mname_node = _find_first(c, "property_identifier")
            if mname_node is None:
                continue
            mname = _node_text(mname_node)
            params_node = _find_first(c, "formal_parameters")
            params, has_rest = (
                _params_from_formal(params_node) if params_node is not None else ([], False)
            )
            out[mname] = CallableInfo(
                kind="method",
                name=mname,
                line_start=c.start_point[0] + 1,
                line_end=c.end_point[0] + 1,
                params=params,
                has_varargs=has_rest,
            )
        return out

    # ---- call sites ----

    def extract_call_sites(self, tree, source: str) -> list[CallSite]:
        sites: list[CallSite] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type in ("call_expression", "new_expression"):
                callee = self._callee_name(node)
                args_node = _find_first(node, "arguments")
                arg_count = _count_args(args_node) if args_node is not None else 0
                if callee:
                    sites.append(CallSite(
                        callee_name=callee,
                        arg_count=arg_count,
                        line=node.start_point[0] + 1,
                    ))
            stack.extend(node.children)
        return sites

    def _callee_name(self, node) -> str | None:
        for c in node.children:
            if c.type == "identifier":
                return _node_text(c)
            if c.type == "member_expression":
                for sub in reversed(c.children):
                    if sub.type == "property_identifier":
                        return _node_text(sub)
        return None

    # ---- resolution ----

    def resolves(self, imp: ImportInfo, repo_path: str,
                 scaffolded_files: set[str], project_manifest: dict) -> bool:
        mod = imp.module
        if not mod:
            return False

        # node: builtins
        if mod.startswith("node:"):
            return True
        if mod in _NODE_BUILTINS:
            return True

        if imp.is_relative:
            # extract_imports has normalized this to a repo-rooted path.
            for ext in _TS_EXTS:
                if (mod + ext) in scaffolded_files:
                    return True
                if (mod + "/index" + ext) in scaffolded_files:
                    return True
            for ext in _TS_EXTS:
                if os.path.isfile(os.path.join(repo_path, mod + ext)):
                    return True
                if os.path.isfile(os.path.join(repo_path, mod, "index" + ext)):
                    return True
            return False

        # Bare imports: project deps.
        deps = _read_package_deps(repo_path)
        deps.update(project_manifest.get("dependencies", {}) or {})

        top = mod.split("/")[0]
        if mod.startswith("@"):
            scope_pkg = "/".join(mod.split("/")[:2])
            if scope_pkg in deps:
                return True
        if mod in deps or top in deps:
            return True

        return False


def _read_package_deps(repo_path: str) -> dict[str, str]:
    pkg_path = os.path.join(repo_path, "package.json")
    if not os.path.isfile(pkg_path):
        return {}
    try:
        with open(pkg_path) as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    merged: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        merged.update(pkg.get(key, {}) or {})
    return merged


def _params_from_formal(formal_params_node) -> tuple[list[ParamInfo], bool]:
    params: list[ParamInfo] = []
    has_rest = False
    for c in formal_params_node.children:
        if c.type == "required_parameter":
            # rest_pattern as first child means `...args` -> mark has_rest, don't add a param
            if _find_first(c, "rest_pattern") is not None:
                has_rest = True
                continue
            name = _param_name(c)
            has_default = any(child.type == "=" for child in c.children)
            if name is None:
                continue
            params.append(ParamInfo(name=name, has_default=has_default))
        elif c.type == "optional_parameter":
            name = _param_name(c)
            if name is None:
                continue
            params.append(ParamInfo(name=name, has_default=True))
    return params, has_rest


def _param_name(param_node) -> str | None:
    for c in param_node.children:
        if c.type == "identifier":
            return _node_text(c)
        if c.type in ("object_pattern", "array_pattern"):
            # Destructured param: use the raw text as a stand-in name. We don't
            # need the real binding names for arity, only for arg_names display.
            return _node_text(c)
    return None


def _count_args(arguments_node) -> int:
    count = 0
    for c in arguments_node.children:
        if c.type in ("(", ")", ","):
            continue
        # Spread arg counts as one — we can't statically know how many it expands to.
        count += 1
    return count


parser: LanguageParser = TypeScriptParser()
