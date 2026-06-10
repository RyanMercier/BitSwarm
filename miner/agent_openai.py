"""
Generic OpenAI-compatible miner backend.

This is the production-friendly counterpart to ``miner/agent.py``
(Anthropic SDK) and ``miner/agent_cc.py`` (Claude Code subprocess). A
miner running this backend can plug in ANY provider that exposes the
OpenAI Chat Completions API: OpenAI itself, DeepSeek, Together,
OpenRouter, Groq, Fireworks, Anthropic via OpenAI-compat, a local
vLLM / llama.cpp / Ollama server, etc. The miner chooses which one by
setting three env vars:

    MINER_BACKEND=openai
    MINER_OPENAI_API_KEY=<provider key>            # or sk-... for OpenAI
    MINER_OPENAI_BASE_URL=https://api.deepseek.com # or .../v1 etc.
    MINER_OPENAI_MODEL=deepseek-chat               # provider's model id

The agent loop is identical in spirit to the Anthropic one: a tool-use
loop over the BitSwarm tool set (``file_read``, ``file_write``, ``bash``,
``list_files``) that stops when stub tests pass or the iteration / API
budget is exhausted. We re-use ``miner/tools.py``, ``miner/recovery.py``
and ``miner/warm_start.py`` so behaviour stays in lock-step with the
Anthropic backend; the only delta is wire-format translation.

Returns the same ``MinerResult`` dataclass that the other backends do
so ``miner/server.py`` can swap backends transparently.
"""
from __future__ import annotations

import json
import os
import time

from config import (
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_MODEL,
)
from miner.agent import MinerResult, _generate_patch
from miner.prompts import MINER_SYSTEM_PROMPT
from miner.recovery import (
    IterationRecord,
    RetryState,
    StopReason,
    build_retry_context,
    extract_error_signature,
    format_test_feedback,
    update_state,
)
from miner.tools import TOOL_DEFINITIONS, configure as configure_tools, run_tool
from miner.warm_start import build_warm_start_message, build_diff_warm_start_message


_MAX_OUTPUT_TOKENS = int(os.environ.get("MINER_OPENAI_MAX_TOKENS", "4096"))
_MAX_API_CALLS = int(os.environ.get("MINER_OPENAI_MAX_API_CALLS", "40"))


def _to_openai_tools(anthropic_tools):
    """Translate Anthropic ``TOOL_DEFINITIONS`` into the OpenAI
    function-calling shape.

    Anthropic uses ``input_schema``; OpenAI nests the same JSON Schema
    under ``function.parameters``. Strict mode is off so providers that
    don't enforce schemas (DeepSeek, Together, some local servers)
    don't reject the call.
    """
    out = []
    for tool in anthropic_tools:
        out.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        })
    return out


def _make_client():
    """Construct an ``openai.OpenAI`` client wired to the chosen
    provider. Imported lazily so installs without the openai package
    still load the rest of the module (e.g. for tests that monkeypatch
    the backend dispatcher).
    """
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "MINER_BACKEND=openai requires the 'openai' package. "
            "Install with: pip install openai"
        ) from exc

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "MINER_BACKEND=openai requires MINER_OPENAI_API_KEY to be set "
            "(or OPENAI_API_KEY for OpenAI). For local servers that don't "
            "check auth, set it to any non-empty string like 'sk-local'."
        )

    kwargs = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        kwargs["base_url"] = OPENAI_BASE_URL
    return OpenAI(**kwargs)


async def execute_subtask(subtask, repo_path, all_subtask_files, shared_files,
                          shared_files_content, stub_files_content,
                          test_files_content, all_subtasks=None,
                          mode: str = "scaffold",
                          target_stubs=None,
                          new_test_files_content=None,
                          shared_additions_content=None):
    """Drop-in replacement for ``miner.agent.execute_subtask`` that
    drives an OpenAI-compatible Chat Completions endpoint instead of
    the Anthropic SDK.

    Supports both scaffold mode (default) and diff mode. In diff mode
    the warm-start references the current file content + target stub
    instead of stub files, and tool config routes interface checks
    through the target stub.
    """
    subtask_id = subtask["subtask_id"]
    allowed_files = subtask["allowed_files"]
    if mode == "diff":
        test_files = subtask.get("new_test_files") or []
    else:
        test_files = subtask["stub_test_files"]

    print(f"  [Miner-OpenAI {subtask_id}] starting (mode={mode}), model={OPENAI_MODEL} "
          f"base_url={OPENAI_BASE_URL or 'api.openai.com (default)'}")

    configure_tools(
        repo_path, allowed_files, test_files,
        mode=mode, target_stubs=target_stubs or {},
    )

    original_stubs = {}
    for path in allowed_files:
        full_path = os.path.join(repo_path, path)
        if os.path.isfile(full_path):
            with open(full_path, "r") as f:
                original_stubs[path] = f.read()

    if mode == "diff":
        warm_start_text = build_diff_warm_start_message(
            subtask=subtask,
            repo_root=repo_path,
            target_stubs=target_stubs or {},
            new_test_files_content=new_test_files_content or {},
            shared_additions_content=shared_additions_content or {},
            all_subtasks=all_subtasks,
        )
    else:
        warm_start_text = build_warm_start_message(
            subtask=subtask,
            repo_root=repo_path,
            shared_files_content=shared_files_content,
            stub_files_content=stub_files_content,
            test_files_content=test_files_content,
            all_subtask_files=all_subtask_files,
            shared_file_paths=list(shared_files.keys()) if isinstance(shared_files, dict) else shared_files,
            all_subtasks=all_subtasks,
        )

    client = _make_client()
    tools_oai = _to_openai_tools(TOOL_DEFINITIONS)

    # System prompt + warm-start go in once. Subsequent turns append
    # assistant messages (with tool_calls) and tool responses.
    messages = [
        {"role": "system", "content": MINER_SYSTEM_PROMPT},
        {"role": "user", "content": warm_start_text},
    ]

    state = RetryState()
    recent_writes: list[str] = []

    def call_api_with_retry():
        """Call the chat completion endpoint with retry on transient
        errors. Different providers raise different exception types, so
        we fall back to string-matching on the error message for the
        common "rate limit" / "overloaded" / "5xx" signals.
        """
        for attempt in range(1, 6):
            try:
                return client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    tools=tools_oai,
                    tool_choice="auto",
                    max_tokens=_MAX_OUTPUT_TOKENS,
                )
            except Exception as e:
                err_str = str(e).lower()
                transient_markers = (
                    "overloaded", "rate limit", "rate_limit", "429",
                    "500", "502", "503", "504", "529", "timeout",
                    "temporarily unavailable", "connection",
                )
                if any(m in err_str for m in transient_markers) and attempt < 5:
                    wait = 15 * attempt
                    print(f"\n  [Miner-OpenAI {subtask_id}] transient error "
                          f"(attempt {attempt}/5), retrying in {wait}s: {e}")
                    time.sleep(wait)
                    continue
                raise

    for _ in range(_MAX_API_CALLS):
        response = call_api_with_retry()
        choice = response.choices[0]
        msg = choice.message

        tool_calls = msg.tool_calls or []
        text_content = msg.content or ""

        # Append assistant turn (we have to record tool_calls verbatim
        # so the subsequent tool-result messages tie back via id).
        assistant_record = {"role": "assistant", "content": text_content}
        if tool_calls:
            assistant_record["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_record)

        if not tool_calls:
            # Model decided to stop calling tools. Treat as end-of-loop.
            break

        for tc in tool_calls:
            tool_name = tc.function.name
            try:
                tool_input = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                tool_input = {}

            result = run_tool(tool_name, tool_input)

            if tool_name == "file_write" and result["success"]:
                path = tool_input.get("path", "")
                if path and path not in recent_writes:
                    recent_writes.append(path)

            output = result["output"]

            is_test_run = (
                tool_name == "bash"
                and "pytest" in tool_input.get("command", "")
            )

            if is_test_run:
                tests_passed = "[exit code: 0]" in output
                record = IterationRecord(
                    iteration=state.iteration_count + 1,
                    files_written=list(recent_writes),
                    test_command=tool_input.get("command", ""),
                    tests_passed=tests_passed,
                    test_output=output,
                    error_summary=extract_error_signature(output),
                    fix_description="",
                )
                state = update_state(state, record)
                recent_writes = []

                if not tests_passed and not state.stop_reason:
                    output = format_test_feedback(output, state.iteration_count)

                if state.hard_reset_triggered and not tests_passed:
                    for path, original in original_stubs.items():
                        full_path = os.path.join(repo_path, path)
                        with open(full_path, "w") as f:
                            f.write(original)
                    output += f"\n\n{build_retry_context(state)}"
                    state.hard_reset_triggered = False

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            })

        if state.stop_reason:
            break

    patch = _generate_patch(repo_path, allowed_files)
    if not patch:
        print(f"  [Miner-OpenAI {subtask_id}] WARNING: empty patch for "
              f"{allowed_files} in {repo_path}")

    tests_passed = state.stop_reason == StopReason.TESTS_PASSED
    test_output = state.history[-1].test_output if state.history else ""

    print(f"  [Miner-OpenAI {subtask_id}] done: "
          f"{'PASSED' if tests_passed else 'FAILED'} "
          f"({state.iteration_count} iterations, reason: {state.stop_reason})")

    return MinerResult(
        subtask_id=subtask_id,
        patch=patch,
        tests_passed=tests_passed,
        test_output=test_output,
        iterations_used=state.iteration_count,
        stop_reason=state.stop_reason,
        files_modified=list(set(
            f for record in state.history for f in record.files_written
        )),
    )
