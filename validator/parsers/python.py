"""
Python language parser.

This is the Phase 1.5 validation logic from the POC, lifted into the
LanguageParser protocol verbatim. Behavior is unchanged: same imports
extracted, same names extracted, same resolution rules. Only the
function shapes are different so the rest of the validator can dispatch
through a uniform interface.
"""
from __future__ import annotations

import ast
import os
from typing import Any

from validator.parsers.types import (
    CallableInfo,
    CallSite,
    ImportInfo,
    LanguageParser,
    ParamInfo,
    ParseError,
)


# Standard library module top-level names (Python 3.x).
STDLIB_TOP_LEVEL = {
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio", "asyncore",
    "atexit", "base64", "bdb", "binascii", "binhex", "bisect", "builtins",
    "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code",
    "codecs", "codeop", "collections", "colorsys", "compileall", "concurrent",
    "configparser", "contextlib", "contextvars", "copy", "copyreg", "cProfile",
    "crypt", "csv", "ctypes", "curses", "dataclasses", "datetime", "dbm",
    "decimal", "difflib", "dis", "distutils", "doctest", "email", "encodings",
    "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch",
    "fractions", "ftplib", "functools", "gc", "getopt", "getpass", "gettext",
    "glob", "grp", "gzip", "hashlib", "heapq", "hmac", "html", "http",
    "idlelib", "imaplib", "imghdr", "imp", "importlib", "inspect", "io",
    "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
    "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math",
    "mimetypes", "mmap", "modulefinder", "multiprocessing", "netrc", "nis",
    "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev",
    "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil", "platform",
    "plistlib", "poplib", "posix", "posixpath", "pprint", "profile", "pstats",
    "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue", "quopri",
    "random", "re", "readline", "reprlib", "resource", "rlcompleter", "runpy",
    "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver",
    "sqlite3", "ssl", "stat", "statistics", "string", "stringprep", "struct",
    "subprocess", "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny",
    "tarfile", "telnetlib", "tempfile", "termios", "test", "textwrap",
    "threading", "time", "timeit", "tkinter", "token", "tokenize", "trace",
    "traceback", "tracemalloc", "tty", "turtle", "turtledemo", "types",
    "typing", "unicodedata", "unittest", "urllib", "uu", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "winreg", "winsound",
    "wsgiref", "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport",
    "zlib", "_thread",
}

# Well-known import-name to pip-package mappings (used by resolves()).
_KNOWN_PACKAGE_NAMES = {
    "flask", "flask_sqlalchemy", "werkzeug", "jinja2", "sqlalchemy",
    "pytest", "_pytest", "requests", "google", "pydantic", "authlib",
    "numpy", "PIL", "cv2", "sklearn", "scipy", "matplotlib",
}

_PIL_ALIASES = {"PIL", "pil"}
_NUMPY_ALIASES = {"numpy", "np"}


def _module_from_path(fpath: str) -> str:
    """``pkg/sub/mod.py`` -> ``pkg.sub.mod``; trims trailing ``.__init__``."""
    if fpath.endswith(".py"):
        fpath = fpath[:-3]
    dotted = fpath.replace("/", ".").replace("\\", ".")
    if dotted.endswith(".__init__"):
        dotted = dotted[:-9]
    return dotted


class PythonParser:
    """Implements :class:`LanguageParser` over the stdlib ``ast`` module."""

    name = "python"
    extensions = (".py",)

    def parse(self, source: str, filepath: str) -> Any:
        try:
            return ast.parse(source, filename=filepath)
        except SyntaxError as exc:
            raise ParseError(f"{filepath}: {exc.msg} (line {exc.lineno})") from exc

    def module_path_for_file(self, filepath: str,
                             tree: Any = None, source: str = "") -> str:
        return _module_from_path(filepath)

    def extract_imports(self, tree: ast.AST, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        imports: list[ImportInfo] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(ImportInfo(
                        module=alias.name,
                        imported_names=[],
                        line=node.lineno,
                        is_relative=False,
                        raw=f"import {alias.name}",
                    ))
            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    # bare `from . import x` -> represent module as the dots
                    module = "." * (node.level or 0)
                else:
                    module = node.module
                imports.append(ImportInfo(
                    module=module,
                    imported_names=[a.name for a in node.names],
                    line=node.lineno,
                    is_relative=(node.level or 0) > 0,
                    raw=f"from {module} import ...",
                ))
        return imports

    def extract_defined_names(self, tree: ast.AST, source: str) -> dict[str, CallableInfo]:
        names: dict[str, CallableInfo] = {}
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names[node.name] = _func_to_callable(node, kind="function")

            elif isinstance(node, ast.ClassDef):
                methods: dict[str, CallableInfo] = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[item.name] = _func_to_callable(
                            item, kind="method", drop_self=True,
                        )
                names[node.name] = CallableInfo(
                    kind="class",
                    name=node.name,
                    line_start=node.lineno,
                    line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                    methods=methods,
                    is_exported=not node.name.startswith("_"),
                )

            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = CallableInfo(
                            kind="constant",
                            name=target.id,
                            line_start=node.lineno,
                            line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
                            is_exported=not target.id.startswith("_"),
                        )
        return names

    def extract_call_sites(self, tree: ast.AST, source: str) -> list[CallSite]:
        sites: list[CallSite] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            else:
                continue
            sites.append(CallSite(
                callee_name=callee,
                arg_count=len(node.args) + len(node.keywords),
                line=node.lineno,
            ))
        return sites

    def resolves(
        self,
        imp: ImportInfo,
        repo_path: str,
        scaffolded_files: set[str],
        project_manifest: dict,
    ) -> bool:
        module = imp.module
        if not module or module.startswith("."):
            # Relative import — assume scaffolded files cover this.
            return True

        top_level = module.split(".")[0]

        # Standard library.
        if top_level in STDLIB_TOP_LEVEL:
            return True

        requirements = project_manifest.get("requirements", []) or []

        # Third-party packages listed in requirements.txt.
        if requirements:
            req_names = {r.lower().replace("-", "_") for r in requirements}
            if top_level.lower().replace("-", "_") in req_names:
                return True

        # Well-known import names that map to non-identical pip names.
        if top_level in _KNOWN_PACKAGE_NAMES:
            return True

        # PIL / numpy alias resolution: import PIL -> Pillow on disk.
        if top_level in _PIL_ALIASES:
            for r in requirements:
                if r.lower().startswith("pillow"):
                    return True
        if top_level in _NUMPY_ALIASES:
            for r in requirements:
                if r.lower().startswith("numpy"):
                    return True

        # Existing files on disk.
        parts = module.split(".")
        mod_path = os.path.join(repo_path, *parts) + ".py"
        if os.path.isfile(mod_path):
            return True
        pkg_path = os.path.join(repo_path, *parts, "__init__.py")
        if os.path.isfile(pkg_path):
            return True
        if len(parts) > 1:
            parent_path = os.path.join(repo_path, *parts[:-1]) + ".py"
            if os.path.isfile(parent_path):
                return True
            parent_pkg = os.path.join(repo_path, *parts[:-1], "__init__.py")
            if os.path.isfile(parent_pkg):
                return True

        # Scaffolded files (shared + stubs being created in this run).
        for path in scaffolded_files:
            if not path.endswith(".py"):
                continue
            mod_from_path = _module_from_path(path)
            if module == mod_from_path or module.startswith(mod_from_path + "."):
                return True

        return False


def _func_to_callable(node: ast.FunctionDef | ast.AsyncFunctionDef,
                       *, kind: str, drop_self: bool = False) -> CallableInfo:
    args = node.args
    # Position-only params (``def f(a, /, b)``) precede regular args
    # in Python 3.8+. Concatenate so arity counts include both.
    positional = list(getattr(args, "posonlyargs", []) or []) + list(args.args)
    if drop_self and positional and positional[0].arg in ("self", "cls"):
        positional = positional[1:]
    # ``args.defaults`` align to the tail of the combined positional
    # list (positional-only and regular share the same defaults tuple).
    defaults_offset = len(positional) - len(args.defaults)
    params = [
        ParamInfo(name=a.arg, has_default=i >= defaults_offset)
        for i, a in enumerate(positional)
    ]
    return CallableInfo(
        kind=kind,
        name=node.name,
        line_start=node.lineno,
        line_end=getattr(node, "end_lineno", node.lineno) or node.lineno,
        params=params,
        has_varargs=args.vararg is not None,
        has_kwargs=args.kwarg is not None,
        is_exported=not node.name.startswith("_"),
    )


parser: LanguageParser = PythonParser()
