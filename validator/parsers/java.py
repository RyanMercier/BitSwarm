"""
Java language parser, backed by tree-sitter-java.

Imports of the form ``import x.y.Z;`` are modeled as ``module = "x.y"``
plus ``imported_names = ["Z"]``, so the cross-file contract check
(which is shared with Python and TypeScript) just works: registry
keys are packages, lookups are the type names a package exports.

Wildcard imports (``import x.y.*;``) become ``imported_names = ["*"]``;
the contract check expands them to "every class in the package".

Static imports (``import static x.y.C.MEMBER;``) become
``module = "x.y.C"`` (the owning type) and ``imported_names = ["MEMBER"]``.
"""
from __future__ import annotations

import os
from typing import Any

import tree_sitter_java as tsj
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


_LANGUAGE = Language(tsj.language())

_MAVEN_ROOTS = ("src/main/java/", "src/test/java/",
                 "src/main/kotlin/", "src/test/kotlin/")


def _node_text(node) -> str:
    return node.text.decode("utf-8", errors="replace")


def _find_first(node, type_name: str):
    for c in node.children:
        if c.type == type_name:
            return c
    return None


def _path_to_package(filepath: str) -> str:
    """Path-based fallback when ``package_declaration`` is missing.

    Strips Maven/Gradle source roots and the class filename, returning
    the dotted directory path. Empty string if the file lives at the
    top level.
    """
    p = filepath.replace("\\", "/")
    for root in _MAVEN_ROOTS:
        if p.startswith(root):
            p = p[len(root):]
            break
    else:
        if p.startswith("src/"):
            p = p[4:]
    if p.endswith(".java"):
        p = p[:-5]
    parts = p.split("/")
    return ".".join(parts[:-1]) if len(parts) > 1 else ""


class JavaParser:
    """LanguageParser implementation backed by tree-sitter-java."""

    name = "java"
    extensions = (".java",)

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
            for child in tree.root_node.children:
                if child.type != "package_declaration":
                    continue
                for c in child.children:
                    if c.type in ("scoped_identifier", "identifier"):
                        return _node_text(c).strip()
        return _path_to_package(filepath)

    # ---- imports ----

    def extract_imports(self, tree, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        imports: list[ImportInfo] = []
        for child in tree.root_node.children:
            if child.type != "import_declaration":
                continue
            has_wildcard = any(c.type == "asterisk" for c in child.children)
            scoped = None
            for c in child.children:
                if c.type in ("scoped_identifier", "identifier"):
                    scoped = c
                    break
            if scoped is None:
                continue
            full = _node_text(scoped).strip()

            if has_wildcard:
                module = full
                names: list[str] = ["*"]
            else:
                # `x.y.Z` -> module = "x.y", names = ["Z"]
                if "." in full:
                    module, last = full.rsplit(".", 1)
                    names = [last]
                else:
                    module = ""
                    names = [full]

            imports.append(ImportInfo(
                module=module,
                imported_names=names,
                line=child.start_point[0] + 1,
                is_relative=False,
                raw=_node_text(child),
            ))
        return imports

    # ---- definitions ----

    def extract_defined_names(self, tree, source: str) -> dict[str, CallableInfo]:
        out: dict[str, CallableInfo] = {}
        for child in tree.root_node.children:
            self._collect_top_level(child, out)
        return out

    def _collect_top_level(self, node, out: dict[str, CallableInfo]) -> None:
        if node.type == "class_declaration":
            info = self._class_or_interface_to_callable(node, kind="class")
            out[info.name] = info
        elif node.type == "interface_declaration":
            info = self._class_or_interface_to_callable(node, kind="interface")
            out[info.name] = info
        elif node.type == "enum_declaration":
            name_node = _find_first(node, "identifier")
            if name_node is not None:
                name = _node_text(name_node)
                out[name] = CallableInfo(
                    kind="enum", name=name,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    is_exported=_is_public(node),
                )
        elif node.type == "record_declaration":
            self._collect_record(node, out)

    def _collect_record(self, node, out: dict[str, CallableInfo]) -> None:
        name_node = _find_first(node, "identifier")
        params_node = _find_first(node, "formal_parameters")
        if name_node is None:
            return
        name = _node_text(name_node)
        params, has_varargs = (
            _params_from_formal(params_node) if params_node is not None else ([], False)
        )
        # Record's primary constructor is the record's parameter list. We model
        # the record as a class whose constructor matches its primary params.
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

    def _class_or_interface_to_callable(self, node, *, kind: str) -> CallableInfo:
        name_node = _find_first(node, "identifier")
        name = _node_text(name_node) if name_node is not None else "<anon>"
        body = (_find_first(node, "class_body")
                or _find_first(node, "interface_body"))
        methods: dict[str, CallableInfo] = {}
        if body is not None:
            for c in body.children:
                if c.type == "method_declaration":
                    mname_node = _find_first(c, "identifier")
                    if mname_node is None:
                        continue
                    mname = _node_text(mname_node)
                    params_node = _find_first(c, "formal_parameters")
                    params, has_varargs = (
                        _params_from_formal(params_node)
                        if params_node is not None else ([], False)
                    )
                    methods[mname] = CallableInfo(
                        kind="method", name=mname,
                        params=params, has_varargs=has_varargs,
                        line_start=c.start_point[0] + 1,
                        line_end=c.end_point[0] + 1,
                    )
                elif c.type == "constructor_declaration":
                    params_node = _find_first(c, "formal_parameters")
                    params, has_varargs = (
                        _params_from_formal(params_node)
                        if params_node is not None else ([], False)
                    )
                    # Constructor is keyed by class name to match how we
                    # look it up from _constructor_for(cls_info).
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

    # ---- call sites ----

    def extract_call_sites(self, tree, source: str) -> list[CallSite]:
        sites: list[CallSite] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "object_creation_expression":
                type_node = _find_first(node, "type_identifier")
                args = _find_first(node, "argument_list")
                if type_node is not None:
                    sites.append(CallSite(
                        callee_name=_node_text(type_node),
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            elif node.type == "method_invocation":
                # Find the last identifier before argument_list.
                method_id = None
                args = None
                for c in node.children:
                    if c.type == "identifier":
                        method_id = c
                    elif c.type == "argument_list":
                        args = c
                if method_id is not None:
                    sites.append(CallSite(
                        callee_name=_node_text(method_id),
                        arg_count=_count_args(args) if args is not None else 0,
                        line=node.start_point[0] + 1,
                    ))
            stack.extend(node.children)
        return sites

    # ---- resolution ----

    def resolves(self, imp: ImportInfo, repo_path: str,
                 scaffolded_files: set[str], project_manifest: dict) -> bool:
        mod = imp.module
        if not mod:
            return True

        top = mod.split(".")[0]

        # Java stdlib + closely-adjacent
        if top in {"java", "javax", "jdk"}:
            return True

        # Walk the scaffolded files to know which packages we're authoring.
        scaffolded_packages: set[str] = set()
        for path in scaffolded_files:
            if path.endswith(".java"):
                pkg = _path_to_package(path)
                if pkg:
                    scaffolded_packages.add(pkg)

        # Exact-package match: an authored package.
        if mod in scaffolded_packages:
            return True

        # Sub-package of an authored root that we don't have files for.
        # This is the "made-up subpackage" failure mode.
        for pkg in scaffolded_packages:
            if mod.startswith(pkg + "."):
                return False

        # Project deps: caller passes a list of dep coordinates like
        # ``"org.springframework.boot:spring-boot-starter:3.0.0"``.
        # The group prefix usually matches the import's package root.
        deps = project_manifest.get("dependencies", []) or []
        for dep in deps:
            group = dep.split(":")[0] if ":" in dep else dep
            if mod == group or mod.startswith(group + "."):
                return True

        # Common ecosystem prefixes — Spring, Jackson, JUnit, Lombok all
        # live under ``org.``, ``com.``, ``io.``, ``net.``. If the import
        # isn't a scaffolded sub-package, assume it's an external lib.
        if top in {"org", "com", "io", "net", "kotlin", "scala", "groovy", "lombok"}:
            return True

        return False


def _is_public(node) -> bool:
    mods = _find_first(node, "modifiers")
    if mods is None:
        # No modifiers = package-private in Java, but we treat it as
        # "exported within the package" for our cross-file checks.
        return True
    return any(c.type == "public" for c in mods.children)


def _params_from_formal(formal_params_node) -> tuple[list[ParamInfo], bool]:
    params: list[ParamInfo] = []
    has_varargs = False
    for c in formal_params_node.children:
        if c.type == "formal_parameter":
            name_node = _find_first(c, "identifier")
            if name_node is not None:
                params.append(ParamInfo(
                    name=_node_text(name_node), has_default=False
                ))
        elif c.type == "spread_parameter":
            # `String... rest` — varargs, doesn't count toward required params.
            has_varargs = True
    return params, has_varargs


def _count_args(arg_list_node) -> int:
    count = 0
    for c in arg_list_node.children:
        if c.type in ("(", ")", ","):
            continue
        count += 1
    return count


parser: LanguageParser = JavaParser()
