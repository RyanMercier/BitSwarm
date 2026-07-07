import os
import re
import subprocess
import sys


_LANGUAGE = (os.environ.get("MINER_LANGUAGE", "")
              or os.environ.get("COORDINATOR_LANGUAGE", "")).strip().lower()


def _argv_for(test_file: str) -> list[str]:
    """Build the shell-invocable test command for a single test file.

    Mirrors ``miner/agent_cc.py:_test_command_for`` so the merge-time
    cross-compile runs the same command the miner used to verify its
    own work. Without this dispatch every non-Python / non-C++ language
    fell through to ``pytest`` and immediately failed (running e.g.
    ``pytest tests/test_words.c`` is nonsense).

    Returns the argv list passed to ``subprocess.run``. The shell
    wrappers (`sh -c`) are kept so build-then-run pipelines like
    ``make X && ./X`` stay atomic.
    """
    base = os.path.basename(test_file)
    stem, ext = os.path.splitext(base)

    # C / C++: build the matching test binary via the project Makefile
    # then run it. ``tests/test_words.c`` -> ``make tests/test_words &&
    # ./tests/test_words``.
    if _LANGUAGE in ("c",) and ext == ".c":
        bin_path = test_file[: -len(ext)]
        return ["sh", "-c", f"make {bin_path} && ./{bin_path}"]
    if _LANGUAGE in ("cpp", "c++") and ext in (".cpp", ".cc", ".cxx"):
        bin_path = test_file[: -len(ext)]
        return ["sh", "-c", f"make {bin_path} && ./{bin_path}"]

    # TypeScript: vitest runs the file directly.
    if _LANGUAGE in ("typescript", "ts", "javascript", "js") and ext in (
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ):
        return ["sh", "-c", f"npx vitest run {test_file}"]

    # Java: ``mvn test -Dtest=ClassName``. Path is something like
    # ``src/test/java/wordle/WordsTest.java`` -> filter by ``WordsTest``.
    if _LANGUAGE == "java" and ext == ".java":
        return [
            "sh", "-c",
            f"mvn -q -DfailIfNoTests=false test -Dtest={stem}",
        ]

    # C#: ``dotnet test --filter FullyQualifiedName~ClassName``. Path
    # ``tests/WordsServiceTests.cs`` -> filter by ``WordsServiceTests``.
    if _LANGUAGE in ("csharp", "cs", "dotnet") and ext == ".cs":
        return [
            "sh", "-c",
            f'dotnet test --filter "FullyQualifiedName~{stem}"',
        ]

    # Rust: ``cargo test --test <stem>`` runs the matching file under
    # ``tests/`` as an integration test target. ``tests/test_words.rs``
    # -> ``cargo test --test test_words``.
    if _LANGUAGE in ("rust", "rs") and ext == ".rs":
        return ["sh", "-c", f"cargo test --test {stem}"]

    # Default: pytest (Python and anything unset).
    return [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"]


def run_test_file(test_file, repo_path):
    """Run a single test file and return (passed, output).

    Routed through validator.sandbox: merge-time tests execute
    miner-supplied code, so on a production validator they run in a
    network-less container (see BITSWARM_SANDBOX)."""
    from validator.sandbox import run as sandboxed_run
    try:
        result = sandboxed_run(
            _argv_for(test_file), repo_path, timeout=180,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        passed = result.returncode == 0
        return passed, output
    except subprocess.TimeoutExpired:
        return False, f"[TIMEOUT: test {test_file} exceeded 180s]"
    except Exception as e:
        return False, f"[ERROR running {test_file}: {e}]"


def parse_test_counts(output):
    """Parse pytest output to extract (passed, total) counts.
    Counts XPASS (unexpectedly passing xfail) as passed since that means the code works."""
    passed = 0
    total = 0
    for line in output.split("\n"):
        if "PASSED" in line or "XPASS" in line:
            passed += 1
            total += 1
        elif "FAILED" in line or "ERROR" in line:
            total += 1
        elif "XFAIL" in line:
            passed += 1
            total += 1
    return passed, total


def run_stub_tests(subtask, repo_path):
    """Run all stub tests for a subtask. Returns (all_passed, combined_output)."""
    all_passed = True
    combined_output = ""
    for test_file in subtask["stub_test_files"]:
        passed, output = run_test_file(test_file, repo_path)
        combined_output += f"\n--- {test_file} ---\n{output}\n"
        if not passed:
            all_passed = False
    return all_passed, combined_output


def run_integration_tests(integration_files, repo_path):
    """Run all integration test files. Returns (all_passed, combined_output, pass_ratio)."""
    if not integration_files:
        return True, "(no integration tests)", 1.0

    all_passed = True
    combined_output = ""
    total_passed = 0
    total_tests = 0
    for test_file in integration_files:
        passed, output = run_test_file(test_file, repo_path)
        combined_output += f"\n--- {test_file} ---\n{output}\n"
        if not passed:
            all_passed = False
        p, t = parse_test_counts(output)
        total_passed += p
        total_tests += t

    if total_tests > 0:
        pass_ratio = total_passed / total_tests
    else:
        # Non-pytest runners (a C++ assert-based test exits 0 silently,
        # ditto cargo / ctest / mocha in some modes) don't emit
        # PASSED/FAILED markers. Fall back to the binary signal: if
        # every test binary exited 0, treat as 100% passing.
        pass_ratio = 1.0 if all_passed else 0.0
    return all_passed, combined_output, pass_ratio
