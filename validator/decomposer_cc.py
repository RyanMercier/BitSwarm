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
        # ``json`` output wraps the response in a JSON envelope with a
        # ``result`` field. Easier to extract than text mode when the
        # response itself is also JSON.
        "--output-format", "json",
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

    raw = proc.stdout or ""
    # JSON envelope from --output-format json. Shape:
    #   {"type": "result", "result": "...claude's response...", ...}
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Some claude versions stream multiple JSON objects; take the last.
        candidate = raw.strip().split("\n")[-1].strip()
        try:
            envelope = json.loads(candidate)
        except json.JSONDecodeError:
            raise ValueError(
                f"coordinator output was not JSON: {raw[:400]}..."
            ) from exc

    result = envelope.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ValueError(
            f"coordinator response envelope missing 'result' field: "
            f"{json.dumps(envelope)[:400]}..."
        )
    return result


def _save_debug(text: str, debug_path: str | None) -> None:
    if debug_path is None:
        return
    os.makedirs(os.path.dirname(debug_path), exist_ok=True)
    with open(debug_path, "w") as f:
        f.write(text)


def call_coordinator(repo_path: str, feature_spec: str,
                      previous_errors: list[str] | None = None,
                      debug_dir: str | None = None) -> dict:
    """Same contract as ``validator.decomposer.call_coordinator``.

    Runs the two-phase decomposition under Claude Code subprocesses
    instead of the Anthropic SDK. Returns the merged decomposition
    dict (subtasks + shared_files + stub_files + stub_test_files +
    integration_test_files + requirements_additions).
    """
    # Phase 1: plan
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

    # Phase 2: file contents
    print("  [Phase 2, cc] Generating stub files...", flush=True)
    file_prompt = build_file_generation_prompt(decomposition, repo_path, feature_spec)
    # Phase 2 produces a single JSON object with stub contents. Use a
    # tighter system prompt so the model doesn't add prose.
    files_system = ("You are a Python code generator. Output ONLY a single "
                     "JSON object. No prose. No code fences. Start with {.")
    files_text = _run_claude(file_prompt, files_system, timeout=900)
    _save_debug(files_text, os.path.join(debug_dir, "phase2_files.txt") if debug_dir else None)

    try:
        file_contents = parse_json_response(files_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Phase 2 JSON parse error: {exc}") from exc

    decomposition["stub_files"] = file_contents.get("stub_files", {})
    decomposition["stub_test_files"] = file_contents.get("stub_test_files", {})
    decomposition["integration_test_files"] = file_contents.get("integration_test_files", {})

    return decomposition
