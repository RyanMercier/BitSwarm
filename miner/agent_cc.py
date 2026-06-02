"""
Claude Code subprocess backend for the BitSwarm miner.

This is an alternative to ``miner/agent.py`` (which drives the Anthropic
Python SDK directly). When the miner is configured with
``MINER_BACKEND=claude_code`` it shells out to the ``claude`` CLI in
print mode (``-p``) inside the per-task workspace. The CLI handles the
entire agent loop internally  -  tool use, file edits, test runs,
conversation state  -  and exits when it considers the work done.

Why this exists:

  - For Claude Max / Pro / Team subscribers, the CLI's OAuth auth uses
    the bundled subscription inference, NOT metered API tokens. The
    same logical agent loop runs without per-call billing.
  - The CLI already implements prompt caching, retry, tool execution,
    permission management, etc.  -  there is no benefit to re-doing it in
    Python.
  - For end-to-end smoke tests we get the real agent producing real
    patches without spending any money.

Contract: ``execute_subtask`` here returns the same ``MinerResult``
shape that ``miner/agent.py`` returns, so ``miner/server.py`` can swap
backends transparently.

Auth: the CLI reads ``~/.claude/.credentials.json`` (Max OAuth) by
default. Set ``MINER_CC_BINARY`` to override the CLI path; otherwise we
search ``PATH`` for ``claude``.

Tool surface: only ``Read``, ``Edit``, ``Write``, ``Bash``, ``Glob``,
``Grep`` are enabled. No WebFetch, no MCP, no Task. Permissions are
pre-approved with ``--dangerously-skip-permissions`` since the miner
already runs inside an isolated workspace copy.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from miner.agent import MinerResult, _generate_patch
from miner.recovery import StopReason
from validator.test_runners import run_test


_DEFAULT_BINARY = os.environ.get("MINER_CC_BINARY", "") or "claude"
_DEFAULT_MODEL = os.environ.get("MINER_CC_MODEL", "") or "sonnet"

# Tool allowlist for the subprocess. Local-only  -  no network, no MCP.
_TOOLS = "Read,Edit,Write,Bash,Glob,Grep"

# Optional language switch for the miner prompt + final-test command.
# Resolves to a LanguageProfile via the registry; defaults to Python.
_LANGUAGE = os.environ.get("MINER_LANGUAGE", "").strip().lower()


def _profile():
    # Imported lazily so importing miner/agent_cc.py doesn't drag in
    # the whole parser registry.
    from validator.lang_profiles import profile_for
    return profile_for(language=_LANGUAGE)


def _test_command_for(subtask: dict) -> tuple[str, list[str] | None]:
    """Return ``(display_string, argv_or_None)`` for verifying a subtask.

    ``argv`` is the shell-invocable command to run after the
    subprocess exits. ``None`` means defer to the auto-detecting
    ``validator.test_runners.run_test``. The display string goes into
    the agent prompt so claude runs the same thing iteratively.

    Build-system languages (C, C++) need targeted single-binary runs to
    avoid cross-subtask stubs poisoning the signal when ``make test``
    would link the whole library together. Other languages just need
    the per-file test-runner invocation; defer to auto-detect.
    """
    sid = subtask["subtask_id"]
    test_files = subtask.get("stub_test_files", []) or []
    profile = _profile()
    if profile.name in ("cpp", "c"):
        bin_path = f"tests/test_{sid}"
        display = f"make {bin_path} && ./{bin_path}"
        return display, ["sh", "-c", display]
    if profile.name == "rust":
        display = f"cargo test {sid}"
        return display, ["sh", "-c", display]
    if profile.name == "typescript":
        files = " ".join(test_files) or "<your test file>"
        display = f"npx vitest run {files}"
        return display, ["sh", "-c", display]
    if profile.name == "java":
        display = f"mvn -q -DfailIfNoTests=false test -Dtest=*{sid}*"
        return display, ["sh", "-c", display]
    if profile.name == "csharp":
        display = f'dotnet test --filter "FullyQualifiedName~{sid}"'
        return display, ["sh", "-c", display]
    # python (and unknown): defer to the auto-detecting test runner.
    display = f"pytest {' '.join(test_files) or '<your test file>'} -x --tb=short"
    return display, None


def _build_prompt(subtask: dict,
                   shared_files_content: dict,
                   stub_files_content: dict,
                   test_files_content: dict,
                   all_subtasks: list | None) -> str:
    """Compose the user prompt for the Claude Code session.

    The prompt is intentionally narrow: it lists the files the miner
    must touch, the tests that must pass, and tells the agent to stop
    when the tests pass. The CLI's built-in system prompt handles the
    code-editing conventions, so we don't reproduce the heavy
    BitSwarm MINER_SYSTEM_PROMPT here.
    """
    sid = subtask["subtask_id"]
    description = subtask.get("description", "")
    allowed_files = subtask.get("allowed_files", []) or []
    stub_files = subtask.get("stub_files", []) or []
    test_files = subtask.get("stub_test_files", []) or []

    other_subtasks = []
    if all_subtasks:
        for st in all_subtasks:
            if st.get("subtask_id") == sid:
                continue
            other_subtasks.append(
                f"  - {st.get('subtask_id')}: {st.get('description', '')[:120]}"
            )

    shared_summary = "\n".join(f"  - {p}" for p in sorted(shared_files_content)) \
        if shared_files_content else "  (none)"

    out = [
        f"# BitSwarm subtask: {sid}",
        "",
        description.strip(),
        "",
        "## Stub files to implement",
        *[f"  - {p}" for p in stub_files],
        "",
        "## Test files (must pass when you are done)",
        *[f"  - {p}" for p in test_files],
        "",
        "## Files you may edit (and ONLY these)",
        *[f"  - {p}" for p in allowed_files],
        "",
        "## Shared modules (already implemented, do not modify)",
        shared_summary,
        "",
    ]

    if other_subtasks:
        out += [
            "## Other subtasks (being implemented in parallel  -  do NOT touch their files)",
            *other_subtasks,
            "",
        ]

    test_display, _ = _test_command_for(subtask)
    out += [
        "## Instructions",
        "1. Read the stub files to see the interfaces.",
        "2. Read the test files to understand expected behavior.",
        "3. Replace the placeholder bodies in the stub files (which",
        "   throw / raise a 'not implemented' error) with real",
        "   implementations.",
        "4. Verify the tests for this subtask pass by running:",
        f"     {test_display}",
        "   Fix any failures by iterating on the implementation.",
        "5. Stop when YOUR tests pass. Do not commit. Do not modify",
        "   files outside the allowed list above.",
        "",
        ("If a test depends on another subtask's stub that isn't yet "
         "implemented, mock it (Python: unittest.mock.MagicMock; C++: "
         "a hand-written stub in your own test file). Your job is to "
         "implement YOUR stub, not the other ones."),
    ]
    return "\n".join(out)


@dataclass
class _CCRunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def _run_claude_subprocess(prompt: str, cwd: str, timeout: int) -> _CCRunResult:
    """Invoke ``claude -p`` synchronously.

    Returns the captured output regardless of exit code. Timeouts are
    signalled via ``timed_out`` rather than raised so the caller can
    still emit a valid ``MinerResult``.
    """
    cmd = [
        _DEFAULT_BINARY,
        "-p", prompt,
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--output-format", "text",
        "--tools", _TOOLS,
        "--model", _DEFAULT_MODEL,
        # Skip user/project CLAUDE.md / hooks / plugins so the miner
        # behaves identically on every machine.
        "--setting-sources", "",
        "--disable-slash-commands",
    ]

    try:
        # ``stdin=DEVNULL`` is critical: without it, claude treats the
        # parent's open stdin as a (potentially streaming) input source
        # and prints a 3-second "waiting for stdin" warning, then often
        # exits with a non-zero status even after completing the work.
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
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
            stdout=(exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            stderr=(exc.stderr or b"").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            timed_out=True,
        )


def _run_final_tests(subtask: dict, repo_path: str) -> tuple[bool, str]:
    """Run the subtask's stub tests once after the CLI exits.

    The CLI may already have run them, but we re-run to get a clean
    pass/fail signal independent of whatever Claude said in its final
    message.

    For ``MINER_LANGUAGE=cpp``, we run a single targeted Makefile
    binary (``make tests/test_<sid> && ./tests/test_<sid>``). Default
    is per-file pytest via the test-runner auto-detect.
    """
    display, argv = _test_command_for(subtask)

    if argv is not None:
        # Language-specific single-command path (currently: C++).
        try:
            result = subprocess.run(
                argv, cwd=repo_path,
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            return False, f"--- {display} ---\n[TIMEOUT]\n"
        except Exception as exc:
            return False, f"--- {display} ---\n[ERROR: {exc}]\n"
        output = (result.stdout or "") + (
            f"\n[stderr]\n{result.stderr}" if result.stderr else ""
        )
        passed = result.returncode == 0
        return passed, f"--- {display} ---\n{output}\n"

    combined: list[str] = []
    all_passed = True
    for test_file in subtask.get("stub_test_files", []):
        try:
            result = run_test(test_file, repo_path, timeout=120)
        except subprocess.TimeoutExpired:
            combined.append(f"--- {test_file} ---\n[TIMEOUT]\n")
            all_passed = False
            continue
        except Exception as exc:
            combined.append(f"--- {test_file} ---\n[ERROR: {exc}]\n")
            all_passed = False
            continue
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        combined.append(f"--- {test_file} ---\n{output}\n")
        # pytest exit codes: 0=ok, 1=tests failed, 2=interrupted, 3=internal,
        # 4=usage error, 5=no tests collected. The last one fires for empty
        # ``__init__.py`` files etc.  -  not a real failure.
        if result.returncode not in (0, 5):
            all_passed = False
    return all_passed, "\n".join(combined)


async def execute_subtask(subtask, repo_path, all_subtask_files, shared_files,
                          shared_files_content, stub_files_content,
                          test_files_content, all_subtasks=None,
                          timeout_seconds: int = 600):
    """Drop-in replacement for ``miner.agent.execute_subtask`` that
    drives a Claude Code subprocess instead of the Anthropic SDK loop."""
    sid = subtask["subtask_id"]
    allowed_files = subtask.get("allowed_files", []) or []
    print(f"  [Miner-CC {sid}] starting Claude Code subprocess in {repo_path}")

    if shutil.which(_DEFAULT_BINARY) is None and not os.path.isfile(_DEFAULT_BINARY):
        msg = (
            f"claude CLI not found at '{_DEFAULT_BINARY}'. Install with "
            f"'npm install -g @anthropic-ai/claude-code' or set MINER_CC_BINARY "
            f"to the full path."
        )
        print(f"  [Miner-CC {sid}] {msg}")
        return MinerResult(
            subtask_id=sid, patch="", tests_passed=False,
            test_output=msg, iterations_used=0,
            stop_reason=StopReason.MAX_ITERATIONS,
            files_modified=[],
        )

    prompt = _build_prompt(
        subtask=subtask,
        shared_files_content=shared_files_content or {},
        stub_files_content=stub_files_content or {},
        test_files_content=test_files_content or {},
        all_subtasks=all_subtasks,
    )

    # Run the subprocess in a worker thread so the asyncio loop (e.g.
    # the miner FastAPI server) stays responsive for /status calls
    # while the agent works.
    cc_result = await asyncio.to_thread(
        _run_claude_subprocess, prompt, repo_path, timeout_seconds,
    )

    if cc_result.timed_out:
        print(f"  [Miner-CC {sid}] subprocess timed out after {timeout_seconds}s")
        stop_reason = StopReason.MAX_ITERATIONS
    elif cc_result.returncode != 0:
        print(f"  [Miner-CC {sid}] subprocess exited rc={cc_result.returncode}")
        if cc_result.stdout:
            print(f"    stdout (last 400): {cc_result.stdout[-400:]}")
        if cc_result.stderr:
            print(f"    stderr (last 400): {cc_result.stderr[-400:]}")
        stop_reason = StopReason.MAX_ITERATIONS
    else:
        stop_reason = None  # provisional; refined by test result below

    tests_passed, test_output = _run_final_tests(subtask, repo_path)
    if tests_passed and stop_reason is None:
        stop_reason = StopReason.TESTS_PASSED
    elif stop_reason is None:
        stop_reason = StopReason.MAX_ITERATIONS

    patch = _generate_patch(repo_path, allowed_files)
    if not patch:
        print(f"  [Miner-CC {sid}] WARNING: empty patch generated for {allowed_files}")

    print(f"  [Miner-CC {sid}] done: "
          f"{'PASSED' if tests_passed else 'FAILED'} (reason: {stop_reason})")

    # ``iterations_used`` doesn't have a clean analogue when the CLI
    # owns the loop. Report 1 if anything ran; downstream scoring
    # doesn't read this field today.
    return MinerResult(
        subtask_id=sid,
        patch=patch,
        tests_passed=tests_passed,
        test_output=test_output,
        iterations_used=1 if not cc_result.timed_out else 0,
        stop_reason=stop_reason,
        files_modified=allowed_files,
    )
