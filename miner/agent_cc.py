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
import tempfile
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
    # Prompt comes via stdin to avoid Linux ARG_MAX (warm-start
    # contexts on large existing codebases can exceed 128KB and would
    # crash with E2BIG if passed as a CLI argument). With ``-p`` and
    # no inline prompt, claude reads from stdin and exits when EOF.
    cmd = [
        _DEFAULT_BINARY,
        "-p",
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
        proc = subprocess.run(
            cmd,
            cwd=cwd,
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

    # In diff mode, the per-subtask test gate is new_test_files. In
    # scaffold mode it is stub_test_files. Fall back to either if one
    # is empty, so this works regardless of mode for any subtask shape.
    test_files = (subtask.get("new_test_files") or subtask.get("stub_test_files") or [])

    combined: list[str] = []
    all_passed = True
    for test_file in test_files:
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


def _find_baseline_hash(repo_path: str) -> str | None:
    """Locate the BitSwarm baseline commit in the workspace repo.

    Matches either baseline tag (scaffold mode's "BitSwarm scaffolding"
    or diff mode's "BitSwarm diff baseline")."""
    log_result = subprocess.run(
        ["git", "log", "--all", "--format=%H %s"],
        capture_output=True, text=True, cwd=repo_path,
    )
    for line in (log_result.stdout or "").strip().split("\n"):
        if ("BitSwarm scaffolding" in line
                or "BitSwarm diff baseline" in line):
            return line.split()[0]
    return None


def _hermetic_env(repo_root: str) -> dict:
    """Subprocess env for hermetic test runs: imports resolve to the
    repo on disk (src/ first, then repo root), never to user-site
    editable installs. This MUST match the env the validator's
    scoring gates use, or the miner's local success signal diverges
    from the thing it is scored on."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    src_dir = os.path.join(repo_root, "src")
    paths = [p for p in (src_dir, repo_root) if os.path.isdir(p)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(paths + ([existing] if existing else []))
    return env


def _hermetic_replay_verify(repo_path: str, allowed_files: list,
                              test_files: list,
                              timeout: int = 300) -> tuple[bool, str, str]:
    """The diff-mode success signal: patch-is-the-product verification.

    1. Diff the workspace against the BitSwarm baseline commit,
       scoped to ``allowed_files``. The patch is the only artifact
       that ships; nothing else the agent did in (or outside) the
       workspace counts.
    2. If the patch is empty, verification FAILS by definition: an
       empty patch cannot have implemented a change. This closes the
       "local tests pass but nothing ships" false-positive class
       (e.g. the agent edited an editable-installed copy outside the
       workspace, or the baseline was already contaminated with the
       feature).
    3. Replay the patch onto a pristine checkout of the baseline (a
       temporary git worktree) and run ``test_files`` there with the
       hermetic env. What passes HERE is what the validator's
       additive gate will see.

    Returns ``(tests_passed, output, patch)``.

    Currently Python-only on the test-running side (pytest); the
    worktree replay mechanics are language-agnostic and the runner
    dispatch can be generalized via lang_profiles later.
    """
    if not allowed_files:
        # Guard: "git diff <hash> --" with ZERO pathspecs diffs the
        # whole tree, which would let a subtask with no modify_files
        # ship arbitrary edits. A subtask that modifies nothing has
        # nothing to verify and nothing to score.
        return False, ("[hermetic-verify] subtask has no modify_files; "
                        "nothing can ship, score is zero by construction"), ""

    baseline = _find_baseline_hash(repo_path)
    if baseline is None:
        return False, ("[hermetic-verify] no BitSwarm baseline commit found "
                        "in workspace; cannot verify"), ""

    # Stage everything so untracked files appear in the diff, then
    # produce the scoped patch.
    lock_file = os.path.join(repo_path, ".git", "index.lock")
    if os.path.exists(lock_file):
        os.remove(lock_file)
    subprocess.run(["git", "add", "-A"], capture_output=True, cwd=repo_path)
    diff = subprocess.run(
        ["git", "diff", baseline, "--"] + allowed_files,
        capture_output=True, text=True, cwd=repo_path,
    )
    patch = diff.stdout or ""

    if not patch.strip():
        return False, (
            "[hermetic-verify] EMPTY PATCH: no changes to "
            f"{allowed_files} relative to the baseline commit. An empty "
            "patch ships nothing and cannot implement the change. If "
            "your tests passed locally, they were passing against "
            "something other than your workspace edits (a pre-installed "
            "copy of the package, or a contaminated baseline)."
        ), ""

    # Replay onto a pristine worktree at the baseline.
    replay_dir = tempfile.mkdtemp(prefix="bitswarm_replay_")
    worktree_path = os.path.join(replay_dir, "wt")
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_path, baseline],
            capture_output=True, text=True, cwd=repo_path,
        )
        if add.returncode != 0:
            return False, (f"[hermetic-verify] worktree add failed: "
                            f"{(add.stderr or '')[-300:]}"), patch

        patch_file = os.path.join(replay_dir, "candidate.diff")
        with open(patch_file, "w") as f:
            f.write(patch)
        apply = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", patch_file],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if apply.returncode != 0:
            return False, (f"[hermetic-verify] patch failed to apply to "
                            f"pristine baseline: {(apply.stderr or '')[-300:]}"), patch

        if not test_files:
            return True, "[hermetic-verify] patch applies cleanly (no tests specified)", patch

        result = subprocess.run(
            [sys.executable, "-m", "pytest", *test_files, "-q", "--tb=short"],
            capture_output=True, text=True, cwd=worktree_path,
            timeout=timeout, env=_hermetic_env(worktree_path),
        )
        output = (result.stdout or "") + (
            ("\n[stderr]\n" + result.stderr) if result.stderr else ""
        )
        passed = result.returncode == 0
        return passed, f"[hermetic-verify on pristine baseline]\n{output}", patch
    except subprocess.TimeoutExpired:
        return False, "[hermetic-verify] TIMEOUT running tests on replay", patch
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            capture_output=True, cwd=repo_path,
        )
        subprocess.run(["git", "worktree", "prune"],
                        capture_output=True, cwd=repo_path)
        shutil.rmtree(replay_dir, ignore_errors=True)


def _build_diff_prompt(subtask: dict,
                        target_stubs: dict,
                        new_test_files_content: dict,
                        shared_additions_content: dict,
                        all_subtasks: list | None,
                        repo_path: str) -> str:
    """Compose the user prompt for a diff-mode Claude Code session.

    Mirrors _build_prompt but for the modification flow: lists files
    to MODIFY, embeds the CURRENT content + TARGET STUB for each,
    lists the new tests that must pass, and tells the agent how the
    diff-mode contract works.
    """
    sid = subtask["subtask_id"]
    description = subtask.get("description", "")
    behavior_spec = subtask.get("behavior_spec", "")
    modify_files = subtask.get("modify_files", []) or []
    new_test_files = subtask.get("new_test_files", []) or []

    other_subtasks = []
    if all_subtasks:
        for st in all_subtasks:
            if st.get("subtask_id") == sid:
                continue
            other_subtasks.append(
                f"  - {st.get('subtask_id')}: {st.get('description', '')[:120]}"
            )

    out = [
        f"# BitSwarm DIFF subtask: {sid}",
        "",
        description.strip(),
        "",
        "## Behavior spec",
        behavior_spec.strip(),
        "",
        "## Files you must modify (the ONLY files you may edit)",
        *[f"  - {p}" for p in modify_files],
        "",
        "## New tests that must pass when you are done (READ-ONLY contract)",
        *[f"  - {p}" for p in new_test_files],
        "",
        "## How your work is scored (read this carefully)",
        "Your deliverable is a git patch: the diff of the files listed",
        "under 'Files you must modify', relative to the repo's baseline",
        "commit. After you finish, the validator:",
        "  1. extracts that patch from this workspace,",
        "  2. applies it to a PRISTINE copy of the baseline,",
        "  3. runs the new tests there in an ISOLATED environment",
        "     (PYTHONNOUSERSITE=1, PYTHONPATH=src so imports resolve to",
        "     the repo's own source, never to any installed copy).",
        "Consequences:",
        "  - Edits to files outside 'Files you must modify' DO NOT SHIP.",
        "  - Edits to the new test files DO NOT SHIP (the validator uses",
        "    its own canonical copies). Do not weaken or modify them.",
        "  - 'pip install' anything: useless, the verification env",
        "    ignores user-site packages entirely.",
        "  - If your modify_files diff is empty, you score ZERO no",
        "    matter what your local test run says.",
        "",
    ]

    # Embed CURRENT content of every modify_file so the agent does
    # not need a file_read round-trip for each.
    for path in modify_files:
        full = os.path.join(repo_path, path)
        current = ""
        if os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    current = f.read()
            except OSError:
                pass
        out += [
            f"## CURRENT (unmodified) content of {path}",
            "```",
            current,
            "```",
            "",
        ]

    # Embed TARGET STUB for every modify_file
    for path in modify_files:
        stub = target_stubs.get(path, "")
        out += [
            f"## TARGET STUB for {path} (post-edit public API)",
            "```",
            stub or "(no stub; preserve current public API + add what behavior_spec requires)",
            "```",
            "",
        ]

    # Embed NEW TEST FILE content
    for path in new_test_files:
        content = new_test_files_content.get(path, "")
        if not content:
            full = os.path.join(repo_path, path)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    pass
        out += [
            f"## NEW TEST FILE: {path}",
            "```",
            content,
            "```",
            "",
        ]

    if shared_additions_content:
        out += ["## Shared additions (read-only, may import from)"]
        for path, content in shared_additions_content.items():
            out += [f"### {path}", "```", content, "```", ""]

    if other_subtasks:
        out += [
            "## Other subtasks (being implemented in parallel -- do NOT touch their files)",
            *other_subtasks,
            "",
        ]

    test_files_str = " ".join(new_test_files) or "<your test files>"
    out += [
        "## Instructions",
        "1. Read each CURRENT file above. Understand what is there.",
        "2. Read each TARGET STUB. Understand what the post-edit shape should be.",
        "3. Read the NEW TEST FILES. They are the source of truth for the",
        "   new behavior. If the feature they test appears to ALREADY exist",
        "   in the current files, do not assume you are done: verify with",
        "   the exact command below, and remember an empty diff scores zero.",
        "4. MODIFY each file in 'Files you must modify' to match its target",
        "   stub AND make the new tests pass. Use the Edit or Write tool",
        "   with paths relative to this directory. Never edit files outside",
        "   this directory tree.",
        "5. Verify with EXACTLY this command (it reproduces the validator's",
        "   isolated environment; a bare 'pytest' may import the wrong copy",
        "   of the package and mislead you):",
        f"     PYTHONNOUSERSITE=1 PYTHONPATH=src:. python -m pytest {test_files_str} -x --tb=short",
        "   Fix any failures by iterating on the implementation.",
        "6. The EXISTING project test suite (whatever was already in the repo)",
        "   MUST CONTINUE TO PASS. Do not break existing tests.",
        "7. Stop when the new tests pass under the command above. Do not",
        "   commit. Do not modify the new test files.",
    ]
    return "\n".join(out)


async def execute_subtask(subtask, repo_path, all_subtask_files, shared_files,
                          shared_files_content, stub_files_content,
                          test_files_content, all_subtasks=None,
                          timeout_seconds: int = 600,
                          mode: str = "scaffold",
                          target_stubs=None,
                          new_test_files_content=None,
                          shared_additions_content=None):
    """Drop-in replacement for ``miner.agent.execute_subtask`` that
    drives a Claude Code subprocess instead of the Anthropic SDK loop.

    Supports both scaffold mode (default; existing behavior) and diff
    mode. In diff mode the prompt is built via ``_build_diff_prompt``
    and the post-run test verification uses the subtask's
    ``new_test_files`` instead of ``stub_test_files``.
    """
    sid = subtask["subtask_id"]
    if mode == "diff":
        # Diff mode: the patch scope is modify_files ONLY. The new
        # test files are the validator-owned contract; the miner reads
        # them but its edits to them never ship (the validator scores
        # against its own canonical copies). Letting the patch carry
        # test edits would let a worker weaken its own gate.
        allowed_files = subtask.get("modify_files") or []
    else:
        allowed_files = subtask.get("allowed_files", []) or []
    print(f"  [Miner-CC {sid}] starting Claude Code subprocess (mode={mode}) in {repo_path}")

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

    if mode == "diff":
        prompt = _build_diff_prompt(
            subtask=subtask,
            target_stubs=target_stubs or {},
            new_test_files_content=new_test_files_content or {},
            shared_additions_content=shared_additions_content or {},
            all_subtasks=all_subtasks,
            repo_path=repo_path,
        )
    else:
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

    if mode == "diff":
        # Diff mode: the ONLY success signal is patch-is-the-product.
        # Generate the patch, replay it onto a pristine baseline in a
        # temp worktree, run the new tests there in the hermetic env.
        # This is the exact computation the validator's additive gate
        # performs, so the miner's local result can't diverge from its
        # score. An empty patch fails by definition.
        new_tests = subtask.get("new_test_files") or []
        tests_passed, test_output, patch = _hermetic_replay_verify(
            repo_path, allowed_files, new_tests,
        )
        if not tests_passed:
            print(f"  [Miner-CC {sid}] hermetic replay FAILED:")
            for line in test_output.splitlines()[:8]:
                print(f"    {line}")
    else:
        tests_passed, test_output = _run_final_tests(subtask, repo_path)
        patch = _generate_patch(repo_path, allowed_files)
        if not patch:
            print(f"  [Miner-CC {sid}] WARNING: empty patch generated for {allowed_files}")

    if tests_passed and stop_reason is None:
        stop_reason = StopReason.TESTS_PASSED
    elif stop_reason is None:
        stop_reason = StopReason.MAX_ITERATIONS

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
