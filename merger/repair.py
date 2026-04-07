"""
Repair miner: fixes cross-compilation failures in the merged repo.

After patches are applied for a tier, we re-run each miner's tests against
the merged repo (cross-compilation). If tests fail — e.g. because the miner
used a workaround type instead of the real dependency — the repair miner gets
the specific error traceback plus only the dependency files that appear in that
traceback. This keeps context small regardless of overall project size.
"""

import os
import re
import time

import anthropic

from config import ANTHROPIC_API_KEY, MINER_MODEL
from miner.tools import TOOL_DEFINITIONS, run_tool, configure as configure_tools
from merger.test_runner import run_stub_tests


REPAIR_SYSTEM_PROMPT = """\
You are a repair agent fixing integration failures in a merged codebase. \
Multiple engineers implemented different modules in parallel. Their individual \
tests passed, but after merging, some tests fail due to interface mismatches.

You will see:
- The files you may modify (your implementation files)
- The dependency files referenced in the error traceback (read-only, for reference)
- The test failure output showing the exact error

Your job: make the MINIMAL fix to resolve the incompatibility. Common issues:
- Missing attribute on a returned object
- Wrong method signature (argument count mismatch)
- Missing operator implementation (e.g. __sub__ not defined on a type)
- Type mismatch between modules (custom workaround type instead of the real one)
- Incorrect field/attribute name vs what the schema or caller expects

Rules:
- Only modify files in your allowed_files list
- Do NOT rewrite from scratch — make targeted fixes
- Run tests after each fix to verify
- The dependency files shown below are READ-ONLY — study them to understand the real interface
- The error traceback tells you exactly where the mismatch is — start there\
"""


def extract_traceback_files(test_output, repo_path, exclude_files):
    """
    Extract file paths from a pytest traceback that exist in the repo.
    Returns paths relative to repo_path, excluding the miner's own files.
    """
    # Match Python file paths in traceback lines like:
    #   /full/path/to/repo/module.py:42: in func_name
    #   File "/full/path/to/repo/module.py", line 42
    abs_pattern = re.compile(r'(?:File\s+["\'])?((?:/[^\s:"\',]+\.py))')
    # Also match relative paths like:  module/file.py:42
    rel_pattern = re.compile(r'^(\S+\.py):\d+', re.MULTILINE)

    found = set()
    repo_abs = os.path.abspath(repo_path)

    for match in abs_pattern.finditer(test_output):
        fpath = match.group(1)
        if fpath.startswith(repo_abs):
            rel = os.path.relpath(fpath, repo_abs)
            found.add(rel)

    for match in rel_pattern.finditer(test_output):
        rel = match.group(1)
        if os.path.isfile(os.path.join(repo_path, rel)):
            found.add(rel)

    # Also extract from import errors: "from module.sub import X"
    import_pattern = re.compile(
        r"(?:ImportError|ModuleNotFoundError).*?(?:from|import)\s+([\w.]+)"
    )
    for match in import_pattern.finditer(test_output):
        mod = match.group(1).replace(".", "/") + ".py"
        if os.path.isfile(os.path.join(repo_path, mod)):
            found.add(mod)

    # Exclude the miner's own files and test files
    exclude = set(exclude_files)
    return sorted(f for f in found if f not in exclude)


async def repair_miner(subtask, merge_repo, test_output):
    """
    Repair a miner's code in the merged repo to fix cross-compilation failures.

    Context is scoped to: miner's own files + only the dependency files that
    appear in the error traceback. This keeps context small regardless of
    overall project size.

    Returns (tests_passed, test_output).
    """
    subtask_id = subtask["subtask_id"]
    allowed_files = subtask["allowed_files"]
    test_files = subtask["stub_test_files"]

    print(f"    [Repair {subtask_id}] Starting")

    # Scope tools to miner's files only (no free-range repo access)
    configure_tools(merge_repo, allowed_files, test_files)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Read current file contents from merged repo (miner's own files)
    file_contents = ""
    for path in allowed_files:
        full_path = os.path.join(merge_repo, path)
        if os.path.isfile(full_path):
            with open(full_path) as f:
                file_contents += f"\n=== {path} (your file, EDITABLE) ===\n{f.read()}\n"

    # Extract dependency files from the traceback — only these, not the whole repo
    all_own_files = set(allowed_files) | set(test_files)
    dep_files = extract_traceback_files(test_output, merge_repo, all_own_files)

    dep_contents = ""
    for path in dep_files:
        full_path = os.path.join(merge_repo, path)
        if os.path.isfile(full_path):
            with open(full_path) as f:
                dep_contents += f"\n=== {path} (dependency, READ-ONLY) ===\n{f.read()}\n"

    if dep_contents:
        dep_section = f"\n## Dependency files from traceback (READ-ONLY — do not modify):\n{dep_contents}"
    else:
        dep_section = ""

    test_file_list = " ".join(test_files)

    user_message = f"""## Repair Task: {subtask_id}
Files you may modify: {allowed_files}
Tests to pass: {test_files}

## Your current implementation:
{file_contents}
{dep_section}
## Test failure in merged codebase:
```
{test_output}
```

Study the dependency files above to understand the real interface, then fix \
YOUR code to match. Do not modify dependency files.

Run tests with: pytest {test_file_list} -v --tb=short"""

    messages = [{"role": "user", "content": user_message}]

    max_api_calls = 10
    for _ in range(max_api_calls):
        response = _call_api(client, subtask_id, messages)
        if response is None:
            break

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            result = run_tool(block.name, block.input)
            output = result["output"]

            # Check if this was a test run that passed
            is_test = (
                block.name == "bash"
                and "pytest" in block.input.get("command", "")
            )
            if is_test and "[exit code: 0]" in output:
                print(f"    [Repair {subtask_id}] PASSED")
                return True, output

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": tool_results})

    # Final check
    passed, output = run_stub_tests(subtask, merge_repo)
    status = "PASSED" if passed else "FAILED"
    print(f"    [Repair {subtask_id}] Final: {status}")
    return passed, output


INTEGRATION_REPAIR_PROMPT = """\
You are a repair agent fixing integration test failures. The integration tests \
verify that independently-implemented modules work together end-to-end. All \
individual module tests pass, but some integration tests fail because they were \
written before the implementations existed and make incorrect assumptions about \
method signatures, constructor arguments, return types, or mock strategies.

You will see:
- The integration test file (EDITABLE — this is what you fix)
- The real implementation files referenced in the error tracebacks (READ-ONLY)
- The test failure output

Your job: fix the integration tests to match the REAL implementation APIs. Common issues:
- Test calls a method that doesn't exist (wrong name or signature)
- Test constructs an object with wrong arguments
- Test uses mock patching that doesn't match the real code path (e.g. mocking \
builtins.open when the code uses pathlib, or patching Image.Image.save when \
the code calls img.save() on a fromarray return value)
- Test expects an attribute that has a different name in the real implementation

Rules:
- Only modify the integration test file — never change implementation files
- Keep tests meaningful — don't delete tests or make them trivially pass
- Read the real implementation to understand the actual API before fixing the test
- Run tests after each fix to verify\
"""


async def repair_integration_tests(integration_files, merge_repo, test_output):
    """
    Repair integration tests that fail after full merge.

    The integration tests were written at decomposition time before miners ran,
    so they may reference wrong method names, constructor signatures, or use
    broken mock strategies. This repair agent fixes the tests to match the
    real implementations.

    Context is scoped: integration test file (editable) + only the implementation
    files that appear in the traceback (read-only).

    Returns (passed, output, ratio).
    """
    print(f"  [Integration Repair] Starting")

    # Integration test files are editable
    allowed_files = list(integration_files)

    # Also allow reading test files for the tool scope
    configure_tools(merge_repo, allowed_files, allowed_files)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Read integration test contents
    test_contents = ""
    for path in integration_files:
        full_path = os.path.join(merge_repo, path)
        if os.path.isfile(full_path):
            with open(full_path) as f:
                test_contents += f"\n=== {path} (EDITABLE) ===\n{f.read()}\n"

    # Extract implementation files from traceback + imports in the test file
    dep_files = extract_traceback_files(test_output, merge_repo, set(integration_files))

    # Also extract from the test file's imports — the traceback may be sparse
    # (e.g. just "assert None is not None") but the test file imports the modules
    import_pattern = re.compile(r'from\s+([\w.]+)\s+import')
    for path in integration_files:
        full_path = os.path.join(merge_repo, path)
        if os.path.isfile(full_path):
            with open(full_path) as f:
                for line in f:
                    m = import_pattern.match(line)
                    if m:
                        mod = m.group(1).replace(".", "/") + ".py"
                        mod_path = os.path.join(merge_repo, mod)
                        if os.path.isfile(mod_path) and mod not in integration_files:
                            dep_files.append(mod)

    dep_files = sorted(set(dep_files))

    dep_contents = ""
    for path in dep_files:
        full_path = os.path.join(merge_repo, path)
        if os.path.isfile(full_path):
            with open(full_path) as f:
                dep_contents += f"\n=== {path} (READ-ONLY implementation) ===\n{f.read()}\n"

    if dep_contents:
        dep_section = f"\n## Implementation files from traceback (READ-ONLY):\n{dep_contents}"
    else:
        dep_section = ""

    test_file_list = " ".join(integration_files)

    user_message = f"""## Integration Test Repair
Files you may modify: {integration_files}

## Current integration tests:
{test_contents}
{dep_section}
## Test failures:
```
{test_output}
```

Study the real implementation files above to understand the actual APIs, then fix \
the integration tests to use the correct method names, constructor arguments, and \
mock strategies. Do NOT delete or gut tests — fix them to test the real behavior.

Run tests with: pytest {test_file_list} -v --tb=short"""

    messages = [{"role": "user", "content": user_message}]

    max_api_calls = 15  # Integration tests may need more iterations
    for _ in range(max_api_calls):
        response = _call_api(client, "integration", messages,
                             system_prompt=INTEGRATION_REPAIR_PROMPT)
        if response is None:
            break

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            result = run_tool(block.name, block.input)
            output = result["output"]

            is_test = (
                block.name == "bash"
                and "pytest" in block.input.get("command", "")
            )
            if is_test and "[exit code: 0]" in output:
                print(f"  [Integration Repair] PASSED")
                from merger.test_runner import run_integration_tests
                return run_integration_tests(integration_files, merge_repo)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": tool_results})

    # Final check
    from merger.test_runner import run_integration_tests
    passed, output, ratio = run_integration_tests(integration_files, merge_repo)
    status = "PASSED" if passed else f"FAILED ({int(ratio*100)}%)"
    print(f"  [Integration Repair] Final: {status}")
    return passed, output, ratio


def _call_api(client, subtask_id, messages, system_prompt=None):
    """Call API with retry for transient errors."""
    if system_prompt is None:
        system_prompt = REPAIR_SYSTEM_PROMPT
    for attempt in range(1, 4):
        try:
            return client.messages.create(
                model=MINER_MODEL,
                max_tokens=8192,
                system=system_prompt,
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
        except anthropic.APIStatusError as e:
            if e.status_code in (529, 500, 503) and attempt < 3:
                wait = 10 * attempt
                print(f"    [Repair {subtask_id}] API {e.status_code}, retry in {wait}s...")
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 3:
                time.sleep(10 * attempt)
                continue
            raise
    return None
