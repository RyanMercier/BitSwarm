"""Trace which repo modules the red-team experiment reaches.

Static pass: parse every tracked .py file with ``ast`` and record every
import, including lazy imports inside function bodies (the codebase
uses many of those to keep optional deps out of the import path). Then
take the transitive closure from the experiment entry points.

Runtime pass: import each entry point in a fresh interpreter and dump
the repo-local entries of ``sys.modules``. This catches nothing the
static pass misses for this codebase, but it confirms which edges are
eager (taken at import time) versus lazy (only when a code path runs).

Usage (from the repo root):
    python redteam/tools/trace_imports.py [--json out.json]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENTRY_POINTS = [
    "demo.run_pipeline",
    "demo.run_pipeline_diff",
    "miner.runtime",
    "validator.merge",
    "validator.diff_merge",
    "validator.scorer",
    "validator.holdback",
    "validator.sandbox",
]


def tracked_py_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                          capture_output=True, text=True, check=True).stdout
    return [p for p in out.split() if not p.startswith(("out/", "redteam/"))]


def module_name(path: str) -> str:
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def resolve(name: str, local: set[str]) -> str | None:
    """Map an imported dotted name to the longest local module prefix."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        cand = ".".join(parts[:i])
        if cand in local:
            return cand
    return None


def scan(path: str, mod: str, local: set[str]) -> list[tuple[str, bool]]:
    """Return [(imported_local_module, is_lazy)] for one file."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)

    edges: list[tuple[str, bool]] = []

    def visit(node: ast.AST, lazy: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_lazy = lazy or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if isinstance(child, ast.Import):
                for alias in child.names:
                    tgt = resolve(alias.name, local)
                    if tgt:
                        edges.append((tgt, lazy))
            elif isinstance(child, ast.ImportFrom):
                if child.level:
                    base = mod.split(".")
                    # a package's __init__ resolves relative to itself
                    if not path.endswith("__init__.py"):
                        base = base[:-1]
                    base = base[: len(base) - (child.level - 1)]
                    prefix = ".".join(base + ([child.module] if child.module else []))
                else:
                    prefix = child.module or ""
                for alias in child.names:
                    tgt = resolve(f"{prefix}.{alias.name}", local) or resolve(prefix, local)
                    if tgt:
                        edges.append((tgt, lazy))
            visit(child, child_lazy)

    visit(tree, False)
    return edges


def runtime_modules(entry: str) -> list[str]:
    code = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {ROOT!r})\n"
        f"importlib.import_module({entry!r})\n"
        "prefixes = ('config', 'protocol', 'miner', 'validator', 'harness', "
        "'neurons', 'demo', 'benchmarks')\n"
        "print('\\n'.join(sorted(m for m in sys.modules "
        "if m.split('.')[0] in prefixes)))\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True)
    if res.returncode != 0:
        return [f"IMPORT FAILED: {res.stderr.strip().splitlines()[-1]}"]
    return res.stdout.split()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the full graph here")
    args = ap.parse_args()

    files = tracked_py_files()
    local = {module_name(p) for p in files}
    graph = {module_name(p): scan(p, module_name(p), local) for p in files}

    reach: dict[str, str] = {}  # module -> "eager" | "lazy-only"
    frontier = [(e, False) for e in ENTRY_POINTS]
    while frontier:
        mod, lazy = frontier.pop()
        prev = reach.get(mod)
        kind = "lazy-only" if lazy else "eager"
        if prev == "eager" or prev == kind:
            continue
        reach[mod] = kind
        for tgt, edge_lazy in graph.get(mod, []):
            frontier.append((tgt, lazy or edge_lazy))

    importers: dict[str, set[str]] = {}
    for src, edges in graph.items():
        for tgt, _ in edges:
            importers.setdefault(tgt, set()).add(src)

    runtime = {e: runtime_modules(e) for e in ENTRY_POINTS}

    report = {
        "entry_points": ENTRY_POINTS,
        "reachable": dict(sorted(reach.items())),
        "unreachable": sorted(local - set(reach)),
        "importers": {k: sorted(v) for k, v in sorted(importers.items())},
        "runtime_sys_modules": runtime,
    }
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
    print("REACHABLE from entry points:")
    for mod, kind in report["reachable"].items():
        print(f"  {mod:40s} {kind}")
    print("\nNOT REACHABLE:")
    for mod in report["unreachable"]:
        imp = ", ".join(sorted(importers.get(mod, []))) or "-"
        print(f"  {mod:40s} imported by: {imp}")
    print("\nRUNTIME (eager import closure per entry point):")
    for e, mods in runtime.items():
        print(f"  {e}: {len(mods)} modules: {' '.join(mods)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
