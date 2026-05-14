"""
Claude Code subprocess backend for the coordinator.

Drop-in replacement for the SDK-based ``call_coordinator`` in
``validator/decomposer.py``. Same two-phase split (Phase 1 = plan,
Phase 2 = stub file contents) because Claude truncates large JSON
responses just as readily through the CLI as it does through the SDK.

Differences from the SDK path:

  - No assistant prefill. The SDK path biases the response to start
    with ``"{"`` by pre-seeding the assistant message; ``claude -p``
    only accepts a user prompt, so we rely on the existing prompts
    telling the model to "start your response with the opening {".
    ``parse_json_response`` still tolerates leading prose, fenced
    ``json`` blocks, and trailing chatter.
  - No streaming. The CLI emits a single response. For a large Phase 2
    output we trade the visible-progress bar for a single block of
    output (still cheap enough on Max).

Auth: the CLI reads ``~/.claude/.credentials.json`` (Max OAuth) by
default. ``CC_COORDINATOR_BINARY`` overrides the executable path;
otherwise we search ``PATH`` for ``claude``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from config import COORDINATOR_MODEL
from validator.decomposer import (
    build_file_generation_prompt,
    build_user_message,
    parse_json_response,
)
from validator.prompts import COORDINATOR_SYSTEM_PROMPT


_DEFAULT_BINARY = (os.environ.get("CC_COORDINATOR_BINARY", "")
                    or os.environ.get("MINER_CC_BINARY", "")
                    or "claude")
_DEFAULT_MODEL = os.environ.get("CC_COORDINATOR_MODEL", "") or COORDINATOR_MODEL

# Optional language override for the Phase 2 stub-generation prompt.
# The hand-written prompt in ``validator/decomposer.py`` is Python-baked
# ("You are writing Python stub files... raise NotImplementedError").
# Setting ``COORDINATOR_LANGUAGE=cpp`` (or any other supported target)
# replaces the Python-specific Rules section with one matching the
# requested language. Spec text in ``feature_spec`` is the source of
# truth for build system, header layout, exception type, etc. --- this
# switch only neutralises the Python rules that would otherwise fight
# the spec.
_LANGUAGE = os.environ.get("COORDINATOR_LANGUAGE", "").strip().lower()


# Language-specific stub-generation rules. Slotted in just before the
# "## Output Format" suffix so they override the Python defaults that
# precede them in ``build_file_generation_prompt``.
_CPP_RULES = """

## LANGUAGE OVERRIDE: C++17

Disregard any preceding instructions that mention Python, NotImplementedError,
package imports, or .py files. This decomposition targets C++17.

Stub files:
- Each stub .hpp declares the public API. Each stub .cpp defines the
  function with a body that immediately throws:
      throw std::logic_error("not implemented: <function_or_method_name>");
- Use header guards (``#ifndef ... #define ... #endif``) on every .hpp.
- Use the namespace specified in the spec (typically ``wordle`` or the
  project-name namespace).
- ``#include`` paths MUST be filesystem-relative from the including
  file's own directory:
    - From ``wordle/<x>.cpp`` or ``wordle/<x>.hpp``, include siblings
      WITHOUT a prefix:  ``#include "types.hpp"``, ``#include "scorer.hpp"``.
    - From ``tests/test_<x>.cpp``, use the parent-relative form:
      ``#include "../wordle/types.hpp"``, ``#include "../wordle/scorer.hpp"``.
    - NEVER use project-rooted paths like ``#include "wordle/types.hpp"``
      from inside the wordle/ directory. The Makefile's -I would make
      this work at compile time, but the validator's Phase 1.5 import
      check is filesystem-relative and will reject it.

Test files:
- Plain C++17, ``int main()`` programs that use ``<cassert>``.
- DO NOT use Catch2, doctest, gtest, or any other framework.
- Each test file MUST fail when compiled against the stub bodies
  (because the stubs throw). Do NOT wrap stub calls in try/catch ---
  that would make the test PASS on stubs, which is wrong.
- Each test file has at least 4 distinct assertions about real return
  values from real function calls.
- Tests live under ``tests/`` and compile against the full library
  per the Makefile in shared_files.
- EVERY subtask gets its own ``tests/test_<subtask>.cpp`` file. No
  exceptions: even the ``cli`` subtask, even subtasks whose surface
  is "trivial", must have their own test file. If you can't think of
  what to test for a subtask, write a smoke test that constructs the
  type or calls the main entry point.

Constructors:
- Each public class has exactly ONE constructor signature in the .hpp.
  Do NOT declare overloaded constructors --- BitSwarm's Phase 1.5
  parser registers a single constructor per class. If a class needs
  optional behaviour (e.g. seeded vs random target), use default
  argument values (``Game(const Words& w, std::string target = ""))``)
  rather than two distinct overloads.

Integration tests:
- Single ``int main()`` returning 0 on success, non-zero on any
  failure. Assertions cover end-to-end behavior (see the spec's
  "Integration test contract" section).

Build system:
- The Makefile in shared_files is the source of truth. Stub files MUST
  fit the layout that Makefile expects (``wordle/*.cpp`` for the
  library, ``tests/test_*.cpp`` for tests).
"""


def _language_rules() -> str:
    """Return the language-specific override block, or empty string if
    no override is configured (i.e. Python defaults from the base
    prompt apply)."""
    if _LANGUAGE in ("cpp", "c++"):
        return _CPP_RULES
    return ""


def _run_claude(prompt: str, system_prompt: str, timeout: int = 600) -> str:
    """Invoke ``claude -p`` and return the response text.

    Raises ``RuntimeError`` if the subprocess fails or claude isn't on
    the path. ``ValueError`` if the output isn't JSON-parseable.
    """
    if shutil.which(_DEFAULT_BINARY) is None and not os.path.isfile(_DEFAULT_BINARY):
        raise RuntimeError(
            f"claude CLI not found at '{_DEFAULT_BINARY}'. Install via "
            f"'npm install -g @anthropic-ai/claude-code' or set "
            f"CC_COORDINATOR_BINARY."
        )

    cmd = [
        _DEFAULT_BINARY,
        "-p", prompt,
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        # ``text`` over ``json``: the JSON envelope mode has a size cap
        # on its ``result`` field that silently returns the empty
        # string for large outputs (Phase 2 stub-file generation hits
        # it consistently). Text mode emits the model's response
        # directly to stdout, which ``parse_json_response`` can then
        # extract a JSON object out of regardless of how large or
        # prose-wrapped it is.
        "--output-format", "text",
        # The coordinator does no tool use; pure text generation.
        "--tools", "",
        "--model", _DEFAULT_MODEL,
        "--append-system-prompt", system_prompt,
        # Match the miner: no per-machine drift from CLAUDE.md / hooks /
        # plugins / slash commands.
        "--setting-sources", "",
        "--disable-slash-commands",
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"coordinator subprocess timed out after {timeout}s"
        ) from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        raise RuntimeError(
            f"coordinator subprocess rc={proc.returncode}\n{tail}"
        )

    raw = (proc.stdout or "").strip()
    if not raw:
        raise ValueError(
            f"coordinator subprocess returned empty stdout; "
            f"stderr tail: {(proc.stderr or '')[-400:]}"
        )
    return raw


def _save_debug(text: str, debug_path: str | None) -> None:
    if debug_path is None:
        return
    os.makedirs(os.path.dirname(debug_path), exist_ok=True)
    with open(debug_path, "w") as f:
        f.write(text)


def _run_claude_writing_files(prompt: str, workdir: str,
                               timeout: int = 900) -> tuple[str, str]:
    """Phase 2 runner: give claude a workspace + Write tool and let it
    write the requested files directly to disk.

    Returns ``(stdout, stderr)``. Callers should walk ``workdir`` for
    the actual file content. This sidesteps a claude-code CLI quirk
    where asking for a large JSON response inline produces a
    successful exit (rc=0, stop_reason=end_turn) but empty stdout ---
    presumably because the model tries to use the Write tool, finds it
    disabled, and falls back to nothing.
    """
    if shutil.which(_DEFAULT_BINARY) is None and not os.path.isfile(_DEFAULT_BINARY):
        raise RuntimeError(
            f"claude CLI not found at '{_DEFAULT_BINARY}'. "
            f"Install via 'npm install -g @anthropic-ai/claude-code'."
        )
    os.makedirs(workdir, exist_ok=True)
    cmd = [
        _DEFAULT_BINARY,
        "-p", prompt,
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--output-format", "text",
        # Allow only the file-mutation surface. No Bash so claude can't
        # try to run pytest mid-generation, no MCP, no WebFetch.
        "--tools", "Write,Edit,Read",
        "--model", _DEFAULT_MODEL,
        "--setting-sources", "",
        "--disable-slash-commands",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Phase 2 subprocess timed out after {timeout}s"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"Phase 2 subprocess rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '')[-800:]}"
        )
    return proc.stdout or "", proc.stderr or ""


def _harvest_workspace(workdir: str, expected: list[str]) -> dict[str, str]:
    """Read every file under ``workdir`` whose repo-relative path appears
    in ``expected``. Paths missing from the workspace are silently
    omitted; the validator's Phase 1.5 will catch the gap and trigger
    a coordinator retry."""
    out: dict[str, str] = {}
    for rel in expected:
        full = os.path.join(workdir, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                out[rel] = f.read()
        except OSError:
            continue
    return out


def call_coordinator(repo_path: str, feature_spec: str,
                      previous_errors: list[str] | None = None,
                      debug_dir: str | None = None) -> dict:
    """Same contract as ``validator.decomposer.call_coordinator``.

    Runs the two-phase decomposition under Claude Code subprocesses
    instead of the Anthropic SDK. Returns the merged decomposition
    dict (subtasks + shared_files + stub_files + stub_test_files +
    integration_test_files + requirements_additions).
    """
    # Phase 1: plan (small JSON, fits comfortably in text output).
    print("  [Phase 1, cc] Decomposition plan...", flush=True)
    plan_message = build_user_message(repo_path, feature_spec, previous_errors)
    plan_text = _run_claude(plan_message, COORDINATOR_SYSTEM_PROMPT)
    _save_debug(plan_text, os.path.join(debug_dir, "phase1_plan.txt") if debug_dir else None)

    try:
        decomposition = parse_json_response(plan_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Phase 1 JSON parse error: {exc}") from exc

    subtasks = decomposition.get("subtasks", [])
    if not subtasks:
        raise ValueError("Phase 1 returned no subtasks")
    print(f"  [Phase 1, cc] {len(subtasks)} subtask(s) planned", flush=True)

    # Phase 2: file contents. Inline JSON output works for tiny
    # decompositions but silently fails on real ones (the model tries
    # to use the Write tool, finds it disabled, and exits with empty
    # stdout). Give it a workspace and the Write tool instead, then
    # harvest the files back into the decomposition dict.
    print(f"  [Phase 2, cc] Generating stub files in tempdir "
          f"(language={_LANGUAGE or 'python (default)'})...", flush=True)
    file_prompt = build_file_generation_prompt(decomposition, repo_path, feature_spec)
    # Replace the "## Output Format" suffix that asks for inline JSON
    # with a file-writing instruction.
    if "## Output Format" in file_prompt:
        file_prompt = file_prompt.split("## Output Format")[0]
    # Language override (if any) goes BEFORE the output-format block so
    # it can countermand the Python rules baked into the base prompt.
    file_prompt += _language_rules()
    file_prompt += (
        "## Output Format\n\n"
        "Write each file directly to disk using the Write tool. Paths are\n"
        "relative to your current working directory. After all files listed\n"
        "above (stub_files, stub_test_files, integration_test_files) have\n"
        "been written, stop. Do not print the file contents to stdout.\n"
    )

    # All paths Phase 2 is expected to produce.
    expected_stubs: list[str] = []
    expected_tests: list[str] = []
    for st in subtasks:
        expected_stubs.extend(st.get("stub_files", []) or [])
        expected_tests.extend(st.get("stub_test_files", []) or [])
    integ_files = list(decomposition.get("integration_test_files", {}).keys())
    if not integ_files:
        # Default per language. The spec's "Integration test contract"
        # section names a specific file, but Phase 1 sometimes leaves
        # integration_test_files empty.
        if _LANGUAGE in ("cpp", "c++"):
            integ_files = ["tests/test_integration.cpp"]
        else:
            integ_files = ["tests/test_integration.py"]
    expected_all = expected_stubs + expected_tests + integ_files

    workdir = (os.path.join(debug_dir, "phase2_workspace") if debug_dir
               else tempfile.mkdtemp(prefix="bitswarm_phase2_"))
    cleanup_workdir = workdir if debug_dir is None else None

    try:
        stdout, _stderr = _run_claude_writing_files(file_prompt, workdir, timeout=900)
        _save_debug(
            stdout,
            os.path.join(debug_dir, "phase2_stdout.txt") if debug_dir else None,
        )

        stub_contents = _harvest_workspace(workdir, expected_stubs)
        test_contents = _harvest_workspace(workdir, expected_tests)
        integ_contents = _harvest_workspace(workdir, integ_files)

        decomposition["stub_files"] = stub_contents
        decomposition["stub_test_files"] = test_contents
        decomposition["integration_test_files"] = integ_contents

        print(f"  [Phase 2, cc] harvested "
              f"{len(stub_contents)}/{len(expected_stubs)} stubs, "
              f"{len(test_contents)}/{len(expected_tests)} tests, "
              f"{len(integ_contents)}/{len(integ_files)} integration",
              flush=True)
    finally:
        if cleanup_workdir is not None:
            shutil.rmtree(cleanup_workdir, ignore_errors=True)

    if not decomposition["stub_files"]:
        raise ValueError("Phase 2 produced no stub_files")

    return decomposition
