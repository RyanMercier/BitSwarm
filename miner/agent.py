import os
import subprocess
import time
from dataclasses import dataclass

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL, MINER_MODEL
from miner.prompts import MINER_SYSTEM_PROMPT
from miner.tools import TOOL_DEFINITIONS, run_tool, configure as configure_tools
from miner.recovery import (
    RetryState, IterationRecord, StopReason,
    extract_error_signature, format_test_feedback, build_retry_context,
    update_state,
)
from miner.warm_start import build_warm_start_message


@dataclass
class MinerResult:
    subtask_id: str
    patch: str
    tests_passed: bool
    test_output: str
    iterations_used: int
    stop_reason: StopReason | None
    files_modified: list
    merge_conflict: bool = False  # set by merger if patch doesn't apply cleanly


async def execute_subtask(subtask, repo_path, all_subtask_files, shared_files,
                          shared_files_content, stub_files_content, test_files_content,
                          all_subtasks=None):
    """
    Run the miner agent for a single subtask.

    1. Build warm-start context (cached — same on every iteration)
    2. Initialize RetryState
    3. Call Claude API with tools in a loop
    4. Track test runs, handle recovery
    5. Return patch when done
    """
    subtask_id = subtask["subtask_id"]
    allowed_files = subtask["allowed_files"]
    test_files = subtask["stub_test_files"]

    print(f"  [Miner {subtask_id}] Starting")

    configure_tools(repo_path, allowed_files, test_files)

    # Save original stub contents for hard reset
    original_stubs = {}
    for path in allowed_files:
        full_path = os.path.join(repo_path, path)
        if os.path.isfile(full_path):
            with open(full_path, "r") as f:
                original_stubs[path] = f.read()

    # Build warm-start message text
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

    # Prompt caching: mark the system prompt and warm-start as cacheable.
    # These are identical on every API call for this miner — without caching,
    # their tokens are billed at full price on every turn. With the cache marker,
    # subsequent calls pay ~10% of the normal input rate for these tokens.
    #
    # The cache_control marker goes on the warm-start message (the last static
    # content). Everything after it (tool calls, results, retries) is dynamic
    # and stays outside the cache.
    cached_system = [
        {
            "type": "text",
            "text": MINER_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    cached_first_message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": warm_start_text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }

    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        base_url=ANTHROPIC_BASE_URL,  # None = SDK default (api.anthropic.com)
    )

    # messages[0] is always the cached warm-start.
    # Subsequent tool exchanges are appended as plain dicts.
    messages = [cached_first_message]
    state = RetryState()
    recent_writes = []

    max_api_calls = 40

    def call_api_with_retry(messages):
        """Call the Claude API with retry on transient errors (529, 500, etc.)."""
        for attempt in range(1, 6):
            try:
                return client.messages.create(
                    model=MINER_MODEL,
                    max_tokens=8192,
                    system=cached_system,
                    messages=messages,
                    tools=TOOL_DEFINITIONS,
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                )
            except anthropic.APIStatusError as e:
                if e.status_code in (529, 500, 503) and attempt < 5:
                    wait = 15 * attempt
                    print(f"\n  [Miner {subtask_id}] API overloaded (attempt {attempt}/5), retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                raise
            except Exception as e:
                err_str = str(e)
                if any(kw in err_str for kw in ("overloaded", "529", "500", "503", "timeout")) and attempt < 5:
                    wait = 15 * attempt
                    print(f"\n  [Miner {subtask_id}] Transient error (attempt {attempt}/5), retrying in {wait}s...")
                    time.sleep(wait)
                    continue
                raise

    for _ in range(max_api_calls):
        response = call_api_with_retry(messages)

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        if not tool_use_blocks:
            if text_blocks:
                messages.append({"role": "assistant", "content": response.content})
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in tool_use_blocks:
            tool_name = block.name
            tool_input = block.input
            tool_id = block.id

            result = run_tool(tool_name, tool_input)

            if tool_name == "file_write" and result["success"]:
                path = tool_input.get("path", "")
                if path not in recent_writes:
                    recent_writes.append(path)

            is_test_run = (
                tool_name == "bash"
                and "pytest" in tool_input.get("command", "")
            )

            output = result["output"]

            if is_test_run:
                tests_passed = "[exit code: 0]" in output

                record = IterationRecord(
                    iteration=state.iteration_count + 1,
                    files_written=list(recent_writes),
                    test_command=tool_input["command"],
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

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": output,
            })

        messages.append({"role": "user", "content": tool_results})

        if state.stop_reason:
            break

    patch = _generate_patch(repo_path, allowed_files)
    if not patch:
        print(f"  [Miner {subtask_id}] WARNING: empty patch generated for {allowed_files} in {repo_path}")

    tests_passed = state.stop_reason == StopReason.TESTS_PASSED
    test_output = state.history[-1].test_output if state.history else ""

    print(f"  [Miner {subtask_id}] Done: {'PASSED' if tests_passed else 'FAILED'} "
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


def _generate_patch(repo_path, allowed_files):
    """
    Generate a git patch for the allowed files.

    Diffs the current working tree against the scaffolding commit (tagged
    'BitSwarm scaffolding'). This is robust against any git operations the
    miner may have run during its tool-use loop (git add, git commit, etc.)
    because it always compares current state vs the known baseline.

    Falls back to git diff --cached if no scaffolding commit exists.
    """
    try:
        # Remove stale lock file if present (inherited from workspace copy)
        lock_file = os.path.join(repo_path, ".git", "index.lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)

        # Find the scaffolding commit — it's the one with that specific message
        log_result = subprocess.run(
            ["git", "log", "--all", "--format=%H %s"],
            capture_output=True, text=True, cwd=repo_path,
        )
        scaffolding_hash = None
        for line in log_result.stdout.strip().split("\n"):
            if "BitSwarm scaffolding" in line:
                scaffolding_hash = line.split()[0]
                break

        # Stage everything so new/untracked files are visible
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, cwd=repo_path,
        )

        if scaffolding_hash:
            # Preferred: diff current state against scaffolding commit
            result = subprocess.run(
                ["git", "diff", scaffolding_hash, "--"] + allowed_files,
                capture_output=True, text=True, cwd=repo_path,
            )
        else:
            # Fallback: no scaffolding commit (git commit failed earlier)
            # Use git diff --cached which shows staged changes vs HEAD
            result = subprocess.run(
                ["git", "diff", "--cached", "--"] + allowed_files,
                capture_output=True, text=True, cwd=repo_path,
            )

        return result.stdout
    except Exception as e:
        print(f"    WARNING: patch generation failed: {e}")
        return ""
