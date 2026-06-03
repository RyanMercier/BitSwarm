"""
Claude Code subprocess backend for the repair miners.

Mirrors ``validator/repair.py`` (which uses the Anthropic SDK with an
explicit tool-use loop) but delegates the agent loop to a ``claude
-p`` subprocess running in the merged-repo workspace. Same purpose:
fix cross-compile / integration-test failures after merge.

Two entry points, matching repair.py's surface:

  ``repair_miner(subtask, merge_repo, test_output)``
    -> (tests_passed: bool, test_output: str)
    Fix a single subtask's code so its own tests pass against the
    merged tree.

  ``repair_integration_tests(integration_files, merge_repo, test_output)``
    -> (passed: bool, output: str, ratio: float)
    Fix the integration test files themselves to match the real
    implementations (the tests were written at scaffold time and may
    have outdated assumptions).

Both run in the same isolated workspace pattern as ``agent_cc``:
``cwd=merge_repo``, tool surface limited to ``Read,Edit,Write,Bash``,
no MCP, no slash commands, OAuth via the user's Claude subscription.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass


_DEFAULT_BINARY = (os.environ.get("REPAIR_CC_BINARY", "")
                    or os.environ.get("MINER_CC_BINARY", "")
                    or "claude")
_DEFAULT_MODEL = (os.environ.get("REPAIR_CC_MODEL", "")
                   or os.environ.get("MINER_CC_MODEL", "")
                   or "sonnet")

# Allow the same tool surface as the miner. Bash is necessary so claude
# can run the test command to verify its fix iteratively.
_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"


@dataclass
class _CCRunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _run_claude_in_workspace(prompt: str, workspace: str,
                              timeout: int = 600) -> _CCRunResult:
    """Spawn ``claude -p`` with ``workspace`` as cwd."""
    # Prompt via stdin to avoid Linux ARG_MAX on large repo contexts.
    cmd = [
        _DEFAULT_BINARY,
        "-p",
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--output-format", "text",
        "--tools", _TOOLS,
        "--model", _DEFAULT_MODEL,
        "--setting-sources", "",
        "--disable-slash-commands",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=workspace,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return _CCRunResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _CCRunResult(
            returncode=-1,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            timed_out=True,
        )


def _test_command_for_subtask(subtask: dict) -> str:
    """Mirror the miner-side test-command-by-language helper.

    Kept local rather than imported to avoid a circular dependency
    between miner/agent_cc and validator/repair_cc.
    """
    from validator.lang_profiles import profile_for
    sid = subtask["subtask_id"]
    test_files = subtask.get("stub_test_files", []) or []
    language = os.environ.get("MINER_LANGUAGE", "").strip().lower() \
        or os.environ.get("COORDINATOR_LANGUAGE", "").strip().lower()
    profile = profile_for(language=language)
    if profile.name in ("c", "cpp"):
        return f"make tests/test_{sid} && ./tests/test_{sid}"
    if profile.name == "rust":
        return f"cargo test {sid}"
    if profile.name == "typescript":
        return f"npx vitest run {' '.join(test_files) or '<test_file>'}"
    if profile.name == "java":
        return f"mvn -q test -Dtest=*{sid}*"
    if profile.name == "csharp":
        return f'dotnet test --filter "FullyQualifiedName~{sid}"'
    return f"pytest {' '.join(test_files) or '<test_file>'} -x --tb=short"


async def repair_miner(subtask: dict, merge_repo: str,
                        test_output: str) -> tuple[bool, str]:
    """Run a repair-style Claude Code subprocess against the merged
    repo. Returns ``(passed, output)``.

    On a fresh subprocess, claude reads the failing files, the test
    output, and the relevant dependencies (its own choice via Read
    tool), then iterates on fixes until the test command passes.
    """
    sid = subtask["subtask_id"]
    allowed_files = subtask.get("allowed_files", []) or []
    test_files = subtask.get("stub_test_files", []) or []
    test_command = _test_command_for_subtask(subtask)

    if shutil.which(_DEFAULT_BINARY) is None and not os.path.isfile(_DEFAULT_BINARY):
        msg = (
            f"claude CLI not found at '{_DEFAULT_BINARY}'. Install via "
            "'npm install -g @anthropic-ai/claude-code' or set "
            "REPAIR_CC_BINARY."
        )
        print(f"    [Repair-CC {sid}] {msg}")
        return False, msg

    prompt = "\n".join([
        f"# BitSwarm repair: subtask {sid}",
        "",
        ("These tests are failing in the MERGED tree after all subtasks "
         "landed. The other subtasks are now real (not stubs); your "
         "implementation needs to adjust to their actual interfaces. "
         "Make the MINIMAL fix to get the tests passing."),
        "",
        "## Files you may edit",
        *[f"  - {p}" for p in allowed_files],
        "",
        "## Test files that must pass",
        *[f"  - {p}" for p in test_files],
        "",
        "## Failing test output",
        "```",
        test_output[:4000],   # cap so the prompt stays tractable
        "```",
        "",
        "## Workflow",
        "1. Read the failing test output to understand the mismatch.",
        "2. Read the dependency files referenced in the traceback to see",
        "   the REAL interface (do not edit these dependency files).",
        "3. Edit only the files in the allowed list above; make the",
        "   smallest change that resolves the failure.",
        "4. Run the tests to verify:",
        f"     {test_command}",
        "5. Stop when the tests pass. Do not commit. Do not modify",
        "   files outside the allowed list.",
    ])

    print(f"    [Repair-CC {sid}] starting subprocess")
    result = await asyncio.to_thread(
        _run_claude_in_workspace, prompt, merge_repo, 600,
    )
    if result.timed_out:
        print(f"    [Repair-CC {sid}] subprocess timed out")
    elif result.returncode != 0:
        print(f"    [Repair-CC {sid}] subprocess rc={result.returncode}")

    # Re-run the tests one final time to get an authoritative signal.
    from validator.test_runner import run_stub_tests
    passed, output = run_stub_tests(subtask, merge_repo)
    print(f"    [Repair-CC {sid}] final: {'PASSED' if passed else 'FAILED'}")
    return passed, output


async def repair_integration_tests(integration_files: list[str],
                                    merge_repo: str,
                                    test_output: str) -> tuple[bool, str, float]:
    """Fix integration test files post-merge. Returns ``(passed, output, ratio)``.

    Mirrors the SDK version: the integration tests are editable, the
    implementation files are off-limits (they're what the tests should
    align to).
    """
    print("    [Integration Repair-CC] starting")

    if shutil.which(_DEFAULT_BINARY) is None and not os.path.isfile(_DEFAULT_BINARY):
        return False, "claude CLI not on PATH", 0.0

    prompt = "\n".join([
        "# BitSwarm integration-test repair",
        "",
        ("These integration tests fail against the fully merged tree. "
         "The implementations are correct -- the tests were written at "
         "scaffold time with assumptions that no longer match. Edit the "
         "TESTS to match the real APIs. Never edit implementation files."),
        "",
        "## Test files you may edit",
        *[f"  - {p}" for p in integration_files],
        "",
        "## Failing test output",
        "```",
        test_output[:4000],
        "```",
        "",
        "## Workflow",
        "1. Read the failing tests + the implementation files they import.",
        "2. Edit the tests so they call the real APIs correctly.",
        "3. Keep the tests meaningful -- do NOT delete assertions or",
        "   make them trivially pass.",
        "4. Run the tests to verify (your test framework's command).",
        "5. Stop when the tests pass.",
    ])

    result = await asyncio.to_thread(
        _run_claude_in_workspace, prompt, merge_repo, 600,
    )
    if result.timed_out:
        print("    [Integration Repair-CC] subprocess timed out")
    elif result.returncode != 0:
        print(f"    [Integration Repair-CC] subprocess rc={result.returncode}")

    from validator.test_runner import run_integration_tests
    passed, output, ratio = run_integration_tests(integration_files, merge_repo)
    status = "PASSED" if passed else f"FAILED ({int(ratio * 100)}%)"
    print(f"    [Integration Repair-CC] final: {status}")
    return passed, output, ratio
