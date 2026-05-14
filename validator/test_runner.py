import os
import re
import subprocess
import sys


_LANGUAGE = (os.environ.get("MINER_LANGUAGE", "")
              or os.environ.get("COORDINATOR_LANGUAGE", "")).strip().lower()


def _argv_for(test_file: str) -> list[str]:
    """Build the shell-invocable test command for a single test file.

    For C++ runs (``MINER_LANGUAGE=cpp`` or ``COORDINATOR_LANGUAGE=cpp``),
    derive the Makefile target from the file name: ``tests/test_x.cpp``
    -> ``make tests/test_x && ./tests/test_x``. For everything else,
    invoke pytest on the file directly.
    """
    if _LANGUAGE in ("cpp", "c++") and test_file.endswith(".cpp"):
        bin_path = test_file[:-4]  # drop ".cpp"
        return ["sh", "-c", f"make {bin_path} && ./{bin_path}"]
    return [sys.executable, "-m", "pytest", test_file, "-v", "--tb=short"]


def run_test_file(test_file, repo_path):
    """Run a single test file and return (passed, output)."""
    try:
        result = subprocess.run(
            _argv_for(test_file),
            capture_output=True, text=True, cwd=repo_path, timeout=180,
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
