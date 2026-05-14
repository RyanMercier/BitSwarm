"""
Parser registry: extension -> language parser.

Languages are registered here as they come online. Phase A shipped
Python. Phase B added TypeScript. Phase C added Java and C#. Phase D
adds C and C++.

The C/C++ split has a wrinkle: ``.h`` files are ambiguous. We follow
the spec's sibling rule: if there's a sibling ``.cpp``/``.cc``/``.cxx``
in the same directory, treat the header as C++; otherwise C. Callers
that have the full set of scaffolded paths should pass it as
``scaffolded_files`` so the disambiguation works; without context the
header defaults to C (the stricter subset; the C++ parser would
silently accept C-only constructs, but the C parser surfaces real C++
syntax as parse errors, which is the safer default for unknown
context).
"""
from __future__ import annotations

import os

from validator.parsers.python import parser as _python_parser
from validator.parsers.types import (
    CallableInfo,
    CallSite,
    ImportInfo,
    LanguageParser,
    ParamInfo,
    ParseError,
)

# Tree-sitter-backed parsers degrade gracefully if their grammar wheel
# is missing. Python-only validation must never break because of a
# missing optional dependency.

def _safe_load(import_path: str, attr: str = "parser"):
    try:
        module = __import__(import_path, fromlist=[attr])
        return getattr(module, attr, None)
    except Exception:
        return None


_typescript_parser = _safe_load("validator.parsers.typescript")
_java_parser = _safe_load("validator.parsers.java")
_csharp_parser = _safe_load("validator.parsers.csharp")
_c_parser = _safe_load("validator.parsers.c")
_cpp_parser = _safe_load("validator.parsers.cpp")
_rust_parser = _safe_load("validator.parsers.rust")

PARSERS: list[LanguageParser] = [_python_parser]
for _p in (_typescript_parser, _java_parser, _csharp_parser,
            _c_parser, _cpp_parser, _rust_parser):
    if _p is not None:
        PARSERS.append(_p)


# Build the extension map. ``.h`` is owned by C by default; the
# disambiguation lives in ``detect()`` so callers with sibling context
# can override.
EXT_TO_PARSER: dict[str, LanguageParser] = {}
for _p in PARSERS:
    for _ext in _p.extensions:
        # First registration wins, except for .h: prefer C as the default
        # and let detect() upgrade to C++ when siblings demand.
        if _ext == ".h":
            EXT_TO_PARSER.setdefault(_ext, _c_parser if _c_parser is not None else _p)
            continue
        EXT_TO_PARSER.setdefault(_ext, _p)


_CPP_SIBLING_EXTS = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")


def detect(filepath: str,
           scaffolded_files: set[str] | None = None) -> LanguageParser | None:
    """Return the parser that handles ``filepath``.

    For ``.h`` files, ``scaffolded_files`` lets the caller disambiguate
    C vs C++: if any sibling shares the same stem with a C++ extension,
    the header is treated as C++. Without context, ``.h`` falls back to
    the C parser (registered above as the ``.h`` default).
    """
    _, ext = os.path.splitext(filepath)
    if ext == ".h" and scaffolded_files and _cpp_parser is not None:
        stem = os.path.splitext(filepath)[0].replace("\\", "/")
        for sib in scaffolded_files:
            sib_norm = sib.replace("\\", "/")
            sib_stem, sib_ext = os.path.splitext(sib_norm)
            if sib_stem == stem and sib_ext in _CPP_SIBLING_EXTS:
                return _cpp_parser
    return EXT_TO_PARSER.get(ext)


def supported_extensions() -> tuple[str, ...]:
    return tuple(sorted(EXT_TO_PARSER.keys()))


__all__ = [
    "CallableInfo",
    "CallSite",
    "ImportInfo",
    "LanguageParser",
    "ParamInfo",
    "ParseError",
    "PARSERS",
    "EXT_TO_PARSER",
    "detect",
    "supported_extensions",
]
