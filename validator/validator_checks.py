"""
Phase 1.5 decomposition validator.

This file is the dispatcher: it groups scaffolded files by detected
language, runs each language's parser to extract imports / definitions
/ call sites, then hands the cross-file resolution off to
``validator_checks_common``. Test verification is delegated to
``validator/test_runners.py``.

Behavior for Python is unchanged from the POC: the language-specific
logic now lives in ``validator/parsers/python.py`` but produces the
same errors with the same wording.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

from validator import parsers
from validator.parsers.python import parser as python_parser
from validator.parsers.types import ParseError
from validator.test_runners import run_test
from validator.validator_checks_common import (
    FileFacts,
    check_interface_contracts,
    check_no_circular_deps,
)


# Back-compat shims: these names were imported by other modules in the POC.
# Re-export them so external callers don't break.
STDLIB_TOP_LEVEL = __import__("validator.parsers.python", fromlist=["STDLIB_TOP_LEVEL"]).STDLIB_TOP_LEVEL


def extract_imports(source: str) -> list[str]:
    """Legacy helper used by external code.

    Returns just the module strings, matching the POC signature.
    Prefer the parser-protocol methods for new code.
    """
    try:
        tree = python_parser.parse(source, "<string>")
    except ParseError:
        return []
    return [imp.module for imp in python_parser.extract_imports(tree, source)]


def resolves(module: str, repo_root: str, shared_files, stub_files=None,
             requirements=None) -> bool:
    """Legacy helper preserved for callers that pre-date the parser refactor."""
    from validator.parsers.types import ImportInfo

    scaffolded: set[str] = set()
    if isinstance(shared_files, dict):
        scaffolded |= set(shared_files.keys())
    elif shared_files:
        scaffolded |= set(shared_files)
    if isinstance(stub_files, dict):
        scaffolded |= set(stub_files.keys())
    elif stub_files:
        scaffolded |= set(stub_files)

    manifest = {"requirements": list(requirements or [])}
    return python_parser.resolves(
        ImportInfo(module=module, imported_names=[], line=0, is_relative=False),
        repo_root, scaffolded, manifest,
    )


def extract_defined_names(source: str) -> dict[str, dict]:
    """Legacy helper used by external code. Returns dicts in the POC shape."""
    try:
        tree = python_parser.parse(source, "<string>")
    except ParseError:
        return {}
    callables = python_parser.extract_defined_names(tree, source)
    out: dict[str, dict] = {}
    for name, info in callables.items():
        if info.kind == "class":
            methods_out: dict[str, dict] = {}
            for mname, m in info.methods.items():
                methods_out[mname] = {
                    "kind": "method",
                    "min_args": m.required_arg_count,
                    "max_args": m.max_arg_count,
                    "arg_names": m.arg_names,
                    "has_varargs": m.has_varargs,
                    "has_kwargs": m.has_kwargs,
                }
            out[name] = {"kind": "class", "methods": methods_out}
        elif info.kind == "function":
            out[name] = {
                "kind": "function",
                "min_args": info.required_arg_count,
                "max_args": info.max_arg_count,
                "arg_names": info.arg_names,
            }
        elif info.kind == "constant":
            out[name] = {"kind": "variable"}
        else:
            out[name] = {"kind": info.kind}
    return out


def validate_decomposition(decomposition: dict, repo_path: str) -> list[str]:
    """Run all validation checks on the decomposition.

    Returns a list of error strings (empty means valid).
    """
    errors: list[str] = []

    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    stub_test_files = decomposition.get("stub_test_files", {})
    integration_test_files = decomposition.get("integration_test_files", {})
    subtasks = decomposition.get("subtasks", [])
    requirements_additions = decomposition.get("requirements_additions", [])

    # Read existing requirements
    req_path = os.path.join(repo_path, "requirements.txt")
    existing_reqs: list[str] = []
    if os.path.isfile(req_path):
        with open(req_path) as f:
            existing_reqs = [
                line.strip() for line in f
                if line.strip() and not line.startswith("#")
            ]
    all_reqs = existing_reqs + requirements_additions

    all_file_contents: dict[str, str] = {
        **shared_files,
        **stub_files,
        **stub_test_files,
        **integration_test_files,
    }

    # Check 1: every file parses under its language parser.
    parsed_trees: dict[str, tuple[object, object]] = {}  # path -> (parser, tree)
    _scaffolded_for_detect = set(all_file_contents)
    for path, content in all_file_contents.items():
        parser = parsers.detect(path, scaffolded_files=_scaffolded_for_detect)
        if parser is None:
            continue  # unknown extension — skip parse check
        try:
            tree = parser.parse(content, path)
        except ParseError as e:
            errors.append(f"SyntaxError in {path}: {e}")
            continue
        parsed_trees[path] = (parser, tree)

    # Check 1b: common alias-without-import bugs (Python-specific lint).
    ALIAS_CHECKS = [
        ("np.", "import numpy as np", "numpy"),
        ("pd.", "import pandas as pd", "pandas"),
    ]
    for path, content in all_file_contents.items():
        if not path.endswith(".py"):
            continue
        for alias_prefix, required_import, pkg in ALIAS_CHECKS:
            if alias_prefix in content and required_import not in content:
                errors.append(
                    f"{path} uses '{alias_prefix}' but is missing '{required_import}' "
                    f"-- add 'import {pkg} as {alias_prefix[:-1]}' at the top of the file"
                )

    # Build a hint table: bare module name -> full dotted module path.
    all_known_files = set(shared_files) | set(stub_files)
    bare_to_full: dict[str, str] = {}
    for fpath in all_known_files:
        parser = parsers.detect(fpath, scaffolded_files=_scaffolded_for_detect)
        if parser is None:
            continue
        parsed = parsed_trees.get(fpath)
        if parsed is not None:
            _, tree = parsed
            full = parser.module_path_for_file(fpath, tree=tree,
                                               source=all_file_contents.get(fpath, ""))
        else:
            full = parser.module_path_for_file(fpath)
        if not full:
            continue
        bare = full.split(".")[-1]
        if bare not in bare_to_full:
            bare_to_full[bare] = full

    def import_fix_hint(module: str) -> str:
        top = module.split(".")[0]
        if top in bare_to_full:
            return (f" -- did you mean '{bare_to_full[top]}'? Use full package path "
                    f"e.g. 'from {bare_to_full[top]} import ...'")
        return (" -- define it in a shared file, use the full package path "
                "(e.g. 'from mypackage.module import MyClass'), or add it to "
                "requirements_additions")

    scaffolded_paths: set[str] = set(shared_files) | set(stub_files)
    manifest = {"requirements": all_reqs}

    # Check 2: imports in stub files resolve.
    for path, content in stub_files.items():
        if path not in parsed_trees:
            continue
        parser, tree = parsed_trees[path]
        for imp in parser.extract_imports(tree, content, path):
            if not parser.resolves(imp, repo_path, scaffolded_paths, manifest):
                errors.append(
                    f"Unresolved import in {path}: '{imp.module}'"
                    + import_fix_hint(imp.module)
                )

    # Check 3: imports in test files resolve.
    for path, content in stub_test_files.items():
        if path not in parsed_trees:
            continue
        parser, tree = parsed_trees[path]
        for imp in parser.extract_imports(tree, content, path):
            if not parser.resolves(imp, repo_path, scaffolded_paths, manifest):
                errors.append(
                    f"Unresolved import in test {path}: '{imp.module}'"
                    + import_fix_hint(imp.module)
                )

    # Check 3b: existing repo files that import from a scaffolded package must
    # still resolve after scaffolding lands. Python-only for now: walking and
    # parsing an arbitrary multi-language repo is too expensive at validation
    # time, and the catch is most useful for the Python case where main.py
    # already imports from raytracer.renderer etc.
    scaffolded_packages: set[str] = set()
    for fpath in scaffolded_paths:
        parts = fpath.split("/")
        if len(parts) > 1:
            scaffolded_packages.add(parts[0])

    if scaffolded_packages:
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "venv", "node_modules")]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, repo_path)
                if rel in all_file_contents:
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                try:
                    tree = python_parser.parse(content, rel)
                except ParseError:
                    continue
                for imp in python_parser.extract_imports(tree, content, rel):
                    top = imp.module.split(".")[0]
                    if top not in scaffolded_packages:
                        continue
                    if not python_parser.resolves(imp, repo_path, scaffolded_paths, manifest):
                        errors.append(
                            f"Existing repo file '{rel}' imports '{imp.module}' which "
                            f"will not exist after scaffolding. Either create a "
                            f"stub/shared file for this module, or update '{rel}' in "
                            f"shared_files to use the correct import path."
                        )

    # Check 4: no file-path overlaps between subtasks.
    all_paths: list[str] = []
    for st in subtasks:
        for f in st.get("stub_files", []):
            if f in all_paths:
                errors.append(f"File path overlap: {f} assigned to multiple subtasks")
            all_paths.append(f)

    # Check 5: complexity weights sum to 1.0.
    total_weight = sum(st.get("complexity_weight", 0) for st in subtasks)
    if abs(total_weight - 1.0) > 0.01:
        errors.append(f"Complexity weights sum to {total_weight}, expected 1.0")

    # Check 6: no circular dependencies.
    errors.extend(check_no_circular_deps(subtasks))

    # Check 7: every subtask has its stub + test content provided.
    provided_stubs = set(stub_files.keys())
    provided_tests = set(stub_test_files.keys())

    for st in subtasks:
        sid = st["subtask_id"]
        if not st.get("stub_test_files"):
            errors.append(f"Subtask '{sid}' has no stub test files")
            continue

        for f in st.get("stub_files", []):
            if f not in provided_stubs:
                errors.append(
                    f"Subtask '{sid}' lists stub file '{f}' but its content is missing "
                    f"from the stub_files dict. You must provide the full file content "
                    f"(with NotImplementedError bodies) for every stub file you list."
                )

        for f in st.get("stub_test_files", []):
            if f not in provided_tests:
                errors.append(
                    f"Subtask '{sid}' lists test file '{f}' but its content is missing "
                    f"from the stub_test_files dict. You must provide the full test file "
                    f"content for every test file you list."
                )

    # Check 8: cross-file interface contracts (per-language).
    if not errors:
        errors.extend(_check_interface_contracts_dispatched(parsed_trees, all_file_contents))

    # Check 9: stub tests must FAIL when run (only if no prior errors).
    if not errors:
        errors.extend(verify_stub_tests_fail(decomposition, repo_path, subtasks))

    return errors


def _check_interface_contracts_dispatched(
    parsed_trees: dict[str, tuple[object, object]],
    all_file_contents: dict[str, str],
) -> list[str]:
    """Group parsed files by language, build per-language FileFacts, run common check."""
    # Group facts by language.
    facts_by_lang: dict[str, list[FileFacts]] = {}
    for path, (parser, tree) in parsed_trees.items():
        content = all_file_contents[path]
        f = FileFacts(
            path=path,
            module=parser.module_path_for_file(path, tree=tree, source=content),
            language=parser.name,
            imports=parser.extract_imports(tree, content, path),
            defined_names=parser.extract_defined_names(tree, content),
            call_sites=parser.extract_call_sites(tree, content),
        )
        facts_by_lang.setdefault(parser.name, []).append(f)

    errors: list[str] = []
    for facts in facts_by_lang.values():
        errors.extend(check_interface_contracts(facts))
    return errors


def verify_stub_tests_fail(decomposition: dict, repo_path: str,
                           subtasks: list[dict]) -> list[str]:
    """Write scaffolding to disk and confirm stub tests fail (stubs raise)."""
    errors: list[str] = []
    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    stub_test_files = decomposition.get("stub_test_files", {})
    integration_test_files = decomposition.get("integration_test_files", {})
    requirements_additions = decomposition.get("requirements_additions", [])

    # Write all files to disk
    all_to_write = {**shared_files, **stub_files, **stub_test_files, **integration_test_files}
    for path, content in all_to_write.items():
        full_path = os.path.join(repo_path, path)
        # ``os.path.dirname`` returns ``""`` for top-level filenames;
        # ``os.makedirs("")`` raises FileNotFoundError on some platforms.
        parent = os.path.dirname(full_path) or "."
        os.makedirs(parent, exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)

    # __init__.py for Python package dirs
    for path in all_to_write:
        if not path.endswith(".py"):
            continue
        parts = path.split("/")
        for i in range(1, len(parts)):
            init_path = os.path.join(repo_path, *parts[:i], "__init__.py")
            if not os.path.isfile(init_path):
                with open(init_path, "w") as f:
                    f.write("")

    # Update requirements.txt
    if requirements_additions:
        req_path = os.path.join(repo_path, "requirements.txt")
        if not os.path.isfile(req_path):
            with open(req_path, "w") as f:
                f.write("")
        with open(req_path, "r") as f:
            existing = f.read()
        with open(req_path, "a") as f:
            for req in requirements_additions:
                if req not in existing:
                    f.write(f"{req}\n")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q"] + requirements_additions,
            capture_output=True, cwd=repo_path,
        )

    # Run each stub test under the detected runner and confirm non-zero exit.
    for st in subtasks:
        for test_file in st.get("stub_test_files", []):
            try:
                result = run_test(test_file, repo_path)
                if result.returncode == 0:
                    errors.append(
                        f"Stub test {test_file} PASSED on scaffolding -- "
                        f"tests should FAIL on NotImplementedError stubs. "
                        f"The test is probably a no-op or doesn't call the stub."
                    )
            except subprocess.TimeoutExpired:
                errors.append(f"Stub test {test_file} timed out")
            except Exception as e:
                errors.append(f"Error running stub test {test_file}: {e}")

    return errors


# Kept around because some POC callers reach in directly.
def run_pytest(test_file: str, repo_root: str) -> subprocess.CompletedProcess:
    """Deprecated: use ``validator.test_runners.run_test``."""
    from validator.test_runners import run_pytest as _run_pytest
    return _run_pytest(test_file, repo_root)
