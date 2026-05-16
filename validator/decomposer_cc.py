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
    build_integration_test_prompt,
    build_user_message,
    parse_json_response,
)
from validator.prompts import COORDINATOR_SYSTEM_PROMPT


_TEST_FIRST = os.environ.get("BITSWARM_TEST_FIRST", "1").strip().lower() in (
    "1", "true", "yes", "on",
)


from validator.lang_profiles import profile_for


_DEFAULT_BINARY = (os.environ.get("CC_COORDINATOR_BINARY", "")
                    or os.environ.get("MINER_CC_BINARY", "")
                    or "claude")
_DEFAULT_MODEL = os.environ.get("CC_COORDINATOR_MODEL", "") or COORDINATOR_MODEL


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
                      debug_dir: str | None = None,
                      language: str | None = None) -> dict:
    """Same contract as ``validator.decomposer.call_coordinator``.

    Runs the two-phase decomposition under Claude Code subprocesses
    instead of the Anthropic SDK. Returns the merged decomposition
    dict (subtasks + shared_files + stub_files + stub_test_files +
    integration_test_files + requirements_additions).

    ``language`` selects the target language profile so Phase 1 plans
    the correct file extensions / project layout. When ``None`` it's
    resolved from ``COORDINATOR_LANGUAGE`` env var / repo auto-detect.
    """
    # Resolve the profile up front so Phase 1's user message gets the
    # language override (otherwise the Python-heavy system prompt
    # silently biases every plan to ``.py`` paths regardless of target).
    profile = profile_for(language=language, repo_path=repo_path)

    # Phase 1: plan (small JSON, fits comfortably in text output).
    print("  [Phase 1, cc] Decomposition plan...", flush=True)
    plan_message = build_user_message(
        repo_path, feature_spec, previous_errors, language=profile.name,
    )
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

    # Phase 1.5 (NEW, test-first): write the integration tests BEFORE
    # any stubs exist. Those tests become the contract Phase 2 has to
    # satisfy. This solves the cross-file constructor / signature
    # drift that bit us on the C++ Wordle run (Game(words, "x") in
    # tests vs Game(string target) in game.hpp).
    if _TEST_FIRST:
        print("  [Phase 1.5, cc] Writing integration tests first...", flush=True)
        integ_workdir = (os.path.join(debug_dir, "phase1_5_workspace")
                          if debug_dir
                          else tempfile.mkdtemp(prefix="bitswarm_phase1_5_"))
        cleanup_integ_workdir = integ_workdir if debug_dir is None else None
        integ_prompt = build_integration_test_prompt(
            decomposition, repo_path, feature_spec, language=profile.name,
        )
        try:
            stdout, _ = _run_claude_writing_files(integ_prompt, integ_workdir, timeout=600)
            _save_debug(
                stdout,
                os.path.join(debug_dir, "phase1_5_stdout.txt") if debug_dir else None,
            )
            integ_files_expected = list(
                decomposition.get("integration_test_files", {}).keys()
            ) or [profile.integration_test_filename]
            integ_contents = _harvest_workspace(integ_workdir, integ_files_expected)
            if integ_contents:
                decomposition["integration_test_files"] = integ_contents
                print(f"  [Phase 1.5, cc] wrote {len(integ_contents)} integration "
                      f"test file(s) as Phase 2 contract", flush=True)
            else:
                print("  [Phase 1.5, cc] no integration tests harvested -- "
                      "Phase 2 will write them along with the stubs", flush=True)
        finally:
            if cleanup_integ_workdir is not None:
                shutil.rmtree(cleanup_integ_workdir, ignore_errors=True)

    # Phase 2: file contents. Inline JSON output works for tiny
    # decompositions but silently fails on real ones (the model tries
    # to use the Write tool, finds it disabled, and exits with empty
    # stdout). Give it a workspace and the Write tool instead, then
    # harvest the files back into the decomposition dict.
    print(f"  [Phase 2, cc] Generating stub files in tempdir "
          f"(language={profile.name})...", flush=True)
    file_prompt = build_file_generation_prompt(
        decomposition, repo_path, feature_spec, language=profile.name,
    )
    # Replace the inline-JSON "## Output Format" suffix (if the base
    # prompt added one) with a file-writing instruction.
    if "## Output Format" in file_prompt:
        file_prompt = file_prompt.split("## Output Format")[0]
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
        integ_files = [profile.integration_test_filename]
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
        # If test-first ran Phase 1.5, those integration tests are
        # already in decomposition["integration_test_files"] and are
        # the contract. Only overwrite with Phase 2's version if Phase
        # 2 produced any AND Phase 1.5 didn't (i.e. test-first off or
        # 1.5 failed to write anything).
        existing_integ = decomposition.get("integration_test_files", {}) or {}
        if integ_contents and not existing_integ:
            decomposition["integration_test_files"] = integ_contents
        elif integ_contents and existing_integ:
            # Phase 2 wrote integration tests anyway. Keep 1.5's
            # version (the authoritative contract); log the discrepancy.
            extra = set(integ_contents) - set(existing_integ)
            if extra:
                # Phase 2 wrote *new* integration test files not in
                # 1.5's set -- merge those in.
                for path in extra:
                    decomposition["integration_test_files"][path] = integ_contents[path]
        # else: Phase 2 produced nothing; Phase 1.5's version stays.

        integ_kept = len(decomposition.get("integration_test_files", {}) or {})
        print(f"  [Phase 2, cc] harvested "
              f"{len(stub_contents)}/{len(expected_stubs)} stubs, "
              f"{len(test_contents)}/{len(expected_tests)} tests, "
              f"{integ_kept} integration "
              f"(from {'phase 1.5' if existing_integ else 'phase 2'})",
              flush=True)
    finally:
        if cleanup_workdir is not None:
            shutil.rmtree(cleanup_workdir, ignore_errors=True)

    if not decomposition["stub_files"]:
        raise ValueError("Phase 2 produced no stub_files")

    return decomposition
