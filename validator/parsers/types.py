"""
Shared types for the multi-language parser layer.

All language parsers implement ``LanguageParser`` and expose the same
five-call surface so ``validator/validator_checks_common.py`` can do
cross-file resolution without caring which language it is looking at.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ParseError(Exception):
    """Raised when a source file cannot be parsed by its language parser."""


@dataclass
class ParamInfo:
    name: str
    has_default: bool = False


@dataclass
class CallableInfo:
    """A top-level (or, for classes, top-level + nested) named definition.

    The ``methods`` map is only populated for class-like kinds. ``params``
    is empty for non-callables (constants, type aliases).
    """
    kind: str                              # 'function' | 'method' | 'class' | 'type' | 'constant' | 'interface' | 'enum'
    name: str
    line_start: int = 0
    line_end: int = 0
    params: list[ParamInfo] = field(default_factory=list)
    methods: dict[str, "CallableInfo"] = field(default_factory=dict)
    has_varargs: bool = False
    has_kwargs: bool = False
    is_exported: bool = True

    @property
    def required_arg_count(self) -> int:
        """Number of params that the caller MUST pass."""
        return sum(1 for p in self.params if not p.has_default)

    @property
    def max_arg_count(self) -> int:
        """Total declared params (caller MAY pass up to this many positionally)."""
        return len(self.params)

    @property
    def arg_names(self) -> list[str]:
        return [p.name for p in self.params]


@dataclass
class ImportInfo:
    """A single import statement.

    ``module`` is the canonical reference to the source: a dotted module
    path for Python/Java, a relative or package path for TS/JS, a header
    name for C/C++. ``imported_names`` lists the named symbols pulled in;
    empty when the whole module/file is brought in by name.
    """
    module: str
    imported_names: list[str] = field(default_factory=list)
    line: int = 0
    is_relative: bool = False
    raw: str = ""


@dataclass
class CallSite:
    callee_name: str
    arg_count: int
    line: int = 0


@runtime_checkable
class LanguageParser(Protocol):
    """All language parsers expose this shape."""

    name: str
    extensions: tuple[str, ...]

    def parse(self, source: str, filepath: str) -> Any:
        """Parse source text into an opaque tree object.

        Returns a value suitable for the parser's other methods. Raises
        ``ParseError`` if the source cannot be parsed at all.
        """
        ...

    def extract_imports(self, tree: Any, source: str,
                        filepath: str = "") -> list[ImportInfo]:
        """Extract imports. ``filepath`` is the importing file's repo-relative
        path; languages with relative imports (TS, JS, Rust) use it to
        normalize ``./foo`` against the importer's directory so the
        resulting ``ImportInfo.module`` is comparable to
        ``module_path_for_file`` outputs. Python ignores it."""
        ...

    def extract_defined_names(self, tree: Any, source: str) -> dict[str, CallableInfo]:
        ...

    def extract_call_sites(self, tree: Any, source: str) -> list[CallSite]:
        ...

    def module_path_for_file(self, filepath: str,
                             tree: Any = None, source: str = "") -> str:
        """Convert a repo-relative file path to the canonical module ref.

        Languages whose canonical ref is encoded in source (Java
        ``package`` declarations, C# ``namespace`` declarations) inspect
        ``tree``/``source`` when provided and fall back to a path-based
        heuristic otherwise. Languages with a strict path-to-module
        mapping (Python, TS) ignore the extra args.

        e.g. for Python: ``pkg/sub/mod.py`` -> ``pkg.sub.mod``.
        """
        ...

    def resolves(
        self,
        imp: ImportInfo,
        repo_path: str,
        scaffolded_files: set[str],
        project_manifest: dict,
    ) -> bool:
        """Decide if an import can be satisfied by the scaffolded repo.

        ``scaffolded_files`` is the union of shared + stub file paths.
        ``project_manifest`` is language-specific (e.g.
        ``{"requirements": [...]}`` for Python).
        """
        ...
