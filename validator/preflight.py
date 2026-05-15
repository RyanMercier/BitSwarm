"""
Pre-flight: confirm the scaffolded repo compiles before miners run.

Phase 2 stub generation can produce inconsistent files even when Phase
1.5 (the AST contract check) and the self-critique pass both let them
through -- the failure modes vary by language. Pre-flight is a final,
cheap, language-aware compile step. If the scaffold doesn't even
compile against its own stubs, there's no point spending 10 minutes
on miners; report the error and trigger a coordinator retry.

What "compile" means per language:
  - python:     ``python -c "import <every stub_module>; import <integration_test>"``
                catches syntax errors and import-resolution failures.
  - typescript: ``npx tsc --noEmit`` against the scaffolded files.
  - cpp / c:    ``make tests/test_integration`` (which compiles the
                whole library + the integration test against it).
  - rust:       ``cargo check`` (compile-only, fast).
  - java/csharp: skipped -- their build systems are heavy and the
                Phase 1.5 contract check is the primary defence.

Returns a list of error strings, same convention as
``validate_decomposition``. Empty list = ready to mine.

Disable with ``BITSWARM_SKIP_PREFLIGHT=1``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Iterable


def is_disabled() -> bool:
    return os.environ.get("BITSWARM_SKIP_PREFLIGHT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _modules_for_python_files(paths: Iterable[str]) -> list[str]:
    """``pkg/sub/x.py`` -> ``pkg.sub.x``; skips ``__init__.py``."""
    out: list[str] = []
    for p in paths:
        if not p.endswith(".py"):
            continue
        base = p[:-3].replace("/", ".").replace("\\", ".")
        if base.endswith(".__init__"):
            base = base[:-9]
        if base:
            out.append(base)
    return out


def _preflight_python(decomposition: dict, repo_path: str) -> list[str]:
    """``python -c "import a; import b; ..."`` for every scaffolded
    module. Surfaces SyntaxError / ImportError immediately."""
    paths: set[str] = set()
    paths.update(decomposition.get("shared_files", {}).keys())
    paths.update(decomposition.get("stub_files", {}).keys())
    paths.update(decomposition.get("integration_test_files", {}).keys())
    mods = _modules_for_python_files(paths)
    if not mods:
        return []

    code = "; ".join(f"import {m}" for m in mods)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return [f"Pre-flight (Python imports): timed out after 60s"]
    if proc.returncode == 0:
        return []
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    # Keep the last 4 lines -- typically the actual exception.
    tail = "\n".join(err[-4:]) if err else "(no output)"
    return [f"Pre-flight (Python imports) failed:\n{tail}"]


def _preflight_typescript(decomposition: dict, repo_path: str) -> list[str]:
    """``npx --no-install tsc --noEmit`` over the scaffolded TS files."""
    if not _has_executable("npx", repo_path):
        return []  # toolchain unavailable -> skip silently
    try:
        proc = subprocess.run(
            ["npx", "--no-install", "tsc", "--noEmit", "--allowJs"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ["Pre-flight (tsc --noEmit): timed out after 120s"]
    except FileNotFoundError:
        return []
    if proc.returncode == 0:
        return []
    err = (proc.stdout or proc.stderr or "").strip().splitlines()
    tail = "\n".join(err[-6:]) if err else "(no output)"
    return [f"Pre-flight (tsc) found type errors:\n{tail}"]


def _preflight_cmake_make(decomposition: dict, repo_path: str,
                           target: str = "tests/test_integration") -> list[str]:
    """Run ``make <target>``. The integration target compiles + links the
    whole library, so type-level mismatches surface here even though the
    binary will fail at runtime on stubs."""
    if not os.path.isfile(os.path.join(repo_path, "Makefile")):
        return []
    try:
        proc = subprocess.run(
            ["make", target],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return [f"Pre-flight (make {target}): timed out after 120s"]
    except FileNotFoundError:
        return []
    if proc.returncode == 0:
        return []
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = "\n".join(err[-10:]) if err else "(no output)"
    return [f"Pre-flight (make {target}) failed:\n{tail}"]


def _preflight_rust(decomposition: dict, repo_path: str) -> list[str]:
    if not _has_executable("cargo", repo_path):
        return []
    try:
        proc = subprocess.run(
            ["cargo", "check", "--tests"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return ["Pre-flight (cargo check): timed out after 180s"]
    except FileNotFoundError:
        return []
    if proc.returncode == 0:
        return []
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = "\n".join(err[-10:]) if err else "(no output)"
    return [f"Pre-flight (cargo check) failed:\n{tail}"]


def _has_executable(name: str, repo_path: str) -> bool:
    """Lightweight ``which`` that doesn't bring in shutil churn."""
    paths = os.environ.get("PATH", "").split(os.pathsep)
    return any(os.path.isfile(os.path.join(d, name))
                or os.path.isfile(os.path.join(d, name + ".exe"))
                for d in paths if d)


def preflight(decomposition: dict, repo_path: str,
              language: str | None = None) -> list[str]:
    """Dispatch the pre-flight check for the resolved language.

    Returns a list of error strings (empty = ready to mine).
    """
    if is_disabled():
        return []
    from validator.lang_profiles import profile_for
    profile = profile_for(language=language, repo_path=repo_path)

    if profile.name == "python":
        return _preflight_python(decomposition, repo_path)
    if profile.name == "typescript":
        return _preflight_typescript(decomposition, repo_path)
    if profile.name in ("cpp", "c"):
        return _preflight_cmake_make(decomposition, repo_path)
    if profile.name == "rust":
        return _preflight_rust(decomposition, repo_path)
    # java / csharp: skipped (build systems too heavy for a "pre" step)
    return []
