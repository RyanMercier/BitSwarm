"""
Language-agnostic interface contract + dependency checks.

This module operates only on the dataclasses defined in
``validator/parsers/types.py``. It does not know whether it is looking
at Python, TypeScript, Java, or anything else. The dispatcher in
``validator/validator_checks.py`` groups scaffolded files by detected
language, runs that language's parser to populate a registry of
``CallableInfo`` per module, then hands the cross-file resolution off
to the functions here.

Two checks live here:

1. ``check_interface_contracts`` — every cross-file import references a
   name that the target module actually defines, and class instantiation
   call sites match the constructor's arity.

2. ``check_no_circular_deps`` — the subtask dependency graph is a DAG.

A dependency-fan-out helper is included as ``check_fanout_warning`` for
soft warnings; the dispatcher decides whether to surface or ignore them.
"""
from __future__ import annotations

from dataclasses import dataclass

from validator.parsers.types import CallableInfo, CallSite, ImportInfo


@dataclass
class FileFacts:
    """All extracted facts for a single source file.

    The dispatcher fills one of these per scaffolded file before invoking
    the contract check, so this module doesn't need to call back into a
    parser.
    """
    path: str
    module: str                                  # canonical module ref (parser.module_path_for_file)
    language: str                                # parser.name
    imports: list[ImportInfo]
    defined_names: dict[str, CallableInfo]
    call_sites: list[CallSite]


def check_interface_contracts(facts: list[FileFacts]) -> list[str]:
    """Generalized Phase 1.5 cross-file consistency check.

    Catches:
      - ``from module import name`` where ``name`` is not defined in ``module``
      - Class instantiations whose arg count doesn't match ``__init__`` /
        constructor arity (positional + keyword, ignoring varargs/kwargs).
    """
    errors: list[str] = []

    # Build registry: canonical module ref -> {name: CallableInfo}.
    # Multiple files can share a module (Java/C# packages/namespaces),
    # so accumulate rather than overwrite.
    registry: dict[str, dict[str, CallableInfo]] = {}
    for f in facts:
        if f.module in registry:
            registry[f.module].update(f.defined_names)
        else:
            registry[f.module] = dict(f.defined_names)

    # Check 1: imported name exists in target module.
    for f in facts:
        for imp in f.imports:
            mod = imp.module
            if mod not in registry:
                continue  # external or stdlib; resolution check happens elsewhere
            target_names = registry[mod]
            for name in imp.imported_names:
                if name == "*":
                    continue
                if name not in target_names:
                    available = sorted(n for n in target_names if not n.startswith("_"))
                    errors.append(
                        f"Interface mismatch in {f.path}: imports '{name}' from "
                        f"'{mod}', but '{name}' is not defined there. "
                        f"Defined names: {available}. Either add '{name}' to "
                        f"the module or fix the import."
                    )

    # Check 2: cross-file class instantiation arity.
    #
    # For each file, build local_name -> (module, class_name, CallableInfo).
    # Then for each call site whose callee_name matches a local class,
    # find the class's constructor and verify the arg count.
    _CALLABLE_KINDS = ("class", "function")

    for f in facts:
        # ``local_callables[name] = (origin_module, name, info)``.
        # Holds both classes (arity-checked via constructor) and
        # functions (arity-checked directly).
        local_callables: dict[str, tuple[str, str, CallableInfo]] = {}

        def _admit(origin_mod: str, name: str, info: CallableInfo) -> None:
            if info.kind in _CALLABLE_KINDS:
                local_callables.setdefault(name, (origin_mod, name, info))

        # Definitions in this file are locally visible.
        for own_name, own_info in f.defined_names.items():
            _admit(f.module, own_name, own_info)
        # Same-module siblings (Java/C# same-package, C files in same
        # registry bucket) are implicitly in scope.
        if f.module in registry:
            for n, info in registry[f.module].items():
                _admit(f.module, n, info)
        for imp in f.imports:
            mod = imp.module
            if mod not in registry:
                continue
            for name in imp.imported_names:
                if name == "*":
                    for n, info in registry[mod].items():
                        if info.is_exported:
                            _admit(mod, n, info)
                    continue
                info = registry[mod].get(name)
                if info is not None:
                    _admit(mod, name, info)
            if not imp.imported_names:
                # C# ``using X;``, Java implicit same-package, C
                # ``#include "h"`` — bring every exported symbol into
                # scope.
                for n, info in registry[mod].items():
                    if info.is_exported:
                        _admit(mod, n, info)

        for site in f.call_sites:
            target = local_callables.get(site.callee_name)
            if target is None:
                continue
            module, callable_name, info = target
            if info.kind == "class":
                callee = _constructor_for(info)
                kind_label = "constructor"
            else:
                callee = info
                kind_label = "function"
            if callee is None:
                continue
            if callee.has_varargs or callee.has_kwargs:
                continue
            n = site.arg_count
            if n < callee.required_arg_count or n > callee.max_arg_count:
                errors.append(
                    f"Arity mismatch in {f.path}: '{callable_name}(...)' called with "
                    f"{n} args, but {module}.{callable_name} {kind_label} expects "
                    f"{callee.required_arg_count}-{callee.max_arg_count} args "
                    f"(params: {callee.arg_names}). Fix the call or the signature."
                )

    return errors


def _constructor_for(cls: CallableInfo) -> CallableInfo | None:
    """Return the constructor CallableInfo for a class, or None.

    Different languages spell their constructor differently:
      - Python:     __init__
      - TS/JS:      constructor
      - Java/C#:    same-name-as-class method
      - C++:        same-name-as-class method
      - Rust:       ``new`` (the Rust parser registers ``new`` under
                    BOTH its real name and the class name, so the
                    ``cls.name in cls.methods`` branch below finds it
                    without introducing a generic ``new``-as-constructor
                    rule that would mis-resolve unrelated Python/TS
                    classes that happen to define a method named ``new``)
    """
    if "__init__" in cls.methods:
        return cls.methods["__init__"]
    if "constructor" in cls.methods:
        return cls.methods["constructor"]
    if cls.name in cls.methods:
        return cls.methods[cls.name]
    # Fall back: if the class itself has params (e.g. records, dataclasses,
    # Rust tuple structs), treat the class info itself as the constructor.
    if cls.params:
        return cls
    return None


def check_no_circular_deps(subtasks: list[dict]) -> list[str]:
    """Confirm the subtask dependency graph has no cycles."""
    graph: dict[str, list[str]] = {}
    for st in subtasks:
        sid = st["subtask_id"]
        graph[sid] = list(st.get("dependencies", []))

    visited: set[str] = set()
    in_stack: set[str] = set()
    errors: list[str] = []

    def dfs(node: str, path: list[str]) -> None:
        if node in in_stack:
            cycle = path[path.index(node):] + [node]
            errors.append(f"Circular dependency: {' -> '.join(cycle)}")
            return
        if node in visited:
            return
        visited.add(node)
        in_stack.add(node)
        for dep in graph.get(node, []):
            dfs(dep, path + [node])
        in_stack.discard(node)

    for node in graph:
        if node not in visited:
            dfs(node, [])

    return errors


def check_fanout_warning(facts: list[FileFacts], threshold: int = 4) -> list[str]:
    """Soft warning: any module imported by >= threshold other modules is a
    fan-out hub and probably belongs in ``shared_files``.

    Returned strings are advisory; the dispatcher decides whether to attach
    them to the error list or just log them.
    """
    fanout: dict[str, set[str]] = {}
    for f in facts:
        for imp in f.imports:
            if imp.module in {ff.module for ff in facts}:
                fanout.setdefault(imp.module, set()).add(f.module)

    warnings: list[str] = []
    for mod, importers in fanout.items():
        if len(importers) >= threshold:
            warnings.append(
                f"Fan-out warning: '{mod}' is imported by {len(importers)} subtask "
                f"modules ({sorted(importers)}). Consider moving its contents to "
                f"shared_files so all subtasks pull from the same definition."
            )
    return warnings
