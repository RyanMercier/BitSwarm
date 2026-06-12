"""
Test runner dispatch by detected build system.

Picks a runner based on what is on disk at ``repo_root``:

- ``package.json`` with vitest/jest/mocha in scripts -> npm test
- ``pom.xml`` -> ``mvn test``
- ``build.gradle`` or ``build.gradle.kts`` -> ``gradle test``
- ``*.csproj`` or ``*.sln`` -> ``dotnet test``
- ``Cargo.toml`` -> ``cargo test``
- ``CMakeLists.txt`` -> ``ctest``
- Fallback -> ``pytest``

The runner's interpretation of "the stub test failed" is what
``validator_checks.verify_stub_tests_fail`` keys on: a non-zero exit
code means the stub raised, which means the stub is real. That contract
holds for every runner above: pytest, vitest/jest/mocha, JUnit/TestNG via
mvn or gradle, dotnet test, and cargo test all return non-zero when any
test in the requested file fails. ctest does too, with the proviso that
compile errors also produce a non-zero exit (which is fine: a
compile-time NotImplemented-equivalent still means "stub is real").
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class RunnerSpec:
    name: str          # 'pytest', 'vitest', 'mvn', 'gradle', 'dotnet', 'cargo', 'ctest'
    cmd: list[str]     # executable + args, ``{test}`` placeholder substituted in run()


def detect_runner(repo_root: str) -> RunnerSpec:
    """Sniff ``repo_root`` for a build-system marker and pick a runner."""
    pkg_json = os.path.join(repo_root, "package.json")
    if os.path.isfile(pkg_json):
        runner = _runner_from_package_json(pkg_json)
        if runner is not None:
            return runner

    if os.path.isfile(os.path.join(repo_root, "pom.xml")):
        # -Dtest=<class> filters to a single test class file (Maven Surefire).
        return RunnerSpec("mvn", ["mvn", "-q", "-DfailIfNoTests=false", "test", "-Dtest={test}"])

    for gradle_file in ("build.gradle", "build.gradle.kts"):
        if os.path.isfile(os.path.join(repo_root, gradle_file)):
            return RunnerSpec("gradle", ["gradle", "test", "--tests", "{test}"])

    try:
        repo_entries = os.listdir(repo_root)
    except OSError:
        repo_entries = []
    if any(f.endswith((".csproj", ".sln")) for f in repo_entries):
        return RunnerSpec("dotnet", ["dotnet", "test", "--filter", "FullyQualifiedName~{test}"])

    if os.path.isfile(os.path.join(repo_root, "Cargo.toml")):
        return RunnerSpec("cargo", ["cargo", "test", "{test}"])

    # C/C++: prefer ctest, then a Makefile ``test`` target. Fall through
    # to compile-only check (handled in ``run_test`` below).
    if (os.path.isfile(os.path.join(repo_root, "CMakeLists.txt"))
            or os.path.isfile(os.path.join(repo_root, "CTestTestfile.cmake"))):
        return RunnerSpec("ctest", ["ctest", "--output-on-failure", "-R", "{test}"])
    if _makefile_has_test_target(repo_root):
        return RunnerSpec("make", ["make", "test"])
    if _looks_like_c_project(repo_root):
        return _compile_only_runner(repo_root)

    return RunnerSpec("pytest", [sys.executable, "-m", "pytest", "{test}", "-x", "--tb=short", "-q"])


def _makefile_has_test_target(repo_root: str) -> bool:
    """A Makefile target lives at column 0; recipe lines (which can
    legitimately contain ``echo test:``) are tab-indented. Variable
    assignments (``test := ...``) are not what we want either."""
    path = os.path.join(repo_root, "Makefile")
    if not os.path.isfile(path):
        return False
    try:
        with open(path) as f:
            for line in f:
                # Strip the newline only; keep leading whitespace.
                line = line.rstrip("\n")
                # A target line has no leading whitespace and a single ``:``
                # that's not an assignment (``:=`` or ``::=``).
                if not line or line[0] in (" ", "\t", "#"):
                    continue
                # Match ``test:`` or ``test :`` (but not ``test :=``).
                head = line.split("#", 1)[0].rstrip()
                if head == "test:" or head.startswith("test:") and not head.startswith("test:="):
                    return True
                if head.startswith("test ") and ":" in head and ":=" not in head:
                    # ``test foo:`` or ``test : ...``; only match exact ``test :``.
                    if head.split(":", 1)[0].strip() == "test":
                        return True
    except OSError:
        return False
    return False


def _looks_like_c_project(repo_root: str) -> bool:
    """Walk one level for .c/.cc/.cpp/.h files. Missing-dir is treated
    as 'no C project here' rather than raising upstream."""
    try:
        entries = os.listdir(repo_root)
    except OSError:
        return False
    for entry in entries:
        if entry.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hpp")):
            return True
    return False


def _compile_only_runner(repo_root: str) -> RunnerSpec:
    """Fall back to a per-file compile check.

    For C stubs, ``cc -c -Wall -Werror file.c`` returns non-zero when
    the function bodies reference symbols that aren't yet defined
    (linkage isn't checked, but missing declarations and obvious stub
    bugs are). For C++ we prefer ``c++``.

    The runner spec is a template; ``run_test`` substitutes ``{test}``.
    We don't have a true "test passes when stub fails" signal here, but
    a stub that calls ``abort()`` or ``assert(0)`` will still cause a
    runtime test framework to exit non-zero, which is what
    ``verify_stub_tests_fail`` keys on. For purely compile-only setups
    the user-facing test would be a build-step assertion.
    """
    if _has_cpp_sources(repo_root):
        return RunnerSpec("c++ -c", ["c++", "-c", "-Wall", "-Werror", "{test}"])
    return RunnerSpec("cc -c", ["cc", "-c", "-Wall", "-Werror", "{test}"])


def _has_cpp_sources(repo_root: str) -> bool:
    for root, _, files in os.walk(repo_root):
        for f in files:
            if f.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")):
                return True
    return False


def _runner_from_package_json(pkg_json_path: str) -> RunnerSpec | None:
    try:
        with open(pkg_json_path) as f:
            pkg = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    # ``pkg.get("scripts", {})`` returns ``{}`` if the key is absent,
    # but explicit ``"scripts": null`` returns ``None``. Same for deps.
    # The ``or {}`` guards both cases.
    scripts = pkg.get("scripts") or {}
    test_script = scripts.get("test") or ""
    dev_deps = pkg.get("devDependencies") or {}

    if "vitest" in test_script or "vitest" in dev_deps:
        return RunnerSpec("vitest", ["npx", "--no-install", "vitest", "run", "{test}", "--reporter=verbose"])
    if "jest" in test_script or "jest" in dev_deps:
        return RunnerSpec("jest", ["npx", "--no-install", "jest", "{test}", "--colors=false"])
    if "mocha" in test_script or "mocha" in dev_deps:
        return RunnerSpec("mocha", ["npx", "--no-install", "mocha", "{test}"])

    # package.json exists but no recognized test framework
    return None


def run_test(test_file: str, repo_root: str,
             timeout: int = 60,
             extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a single test file under the auto-detected runner.

    Returns the ``CompletedProcess``. Non-zero exit code is the
    "stub raised" signal in :func:`verify_stub_tests_fail`.

    ``extra_env`` entries are merged over the inherited environment.
    Callers that need hermetic import resolution (diff-mode replay,
    merge-time gates) pass PYTHONNOUSERSITE / PYTHONPATH through here
    so the same runner dispatch serves both casual and pinned runs.
    """
    spec = detect_runner(repo_root)
    cmd = [arg.replace("{test}", test_file) for arg in spec.cmd]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        cmd,
        capture_output=True, text=True, cwd=repo_root, timeout=timeout,
        env=env,
    )


def run_pytest(test_file: str, repo_root: str,
               timeout: int = 60) -> subprocess.CompletedProcess:
    """Direct pytest invocation  -  used by callers that already know the repo
    is Python-only. New callers should prefer :func:`run_test`."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-x", "--tb=short", "-q"],
        capture_output=True, text=True, cwd=repo_root, timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
