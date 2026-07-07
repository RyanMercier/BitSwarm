"""
Transport-agnostic miner runtime.

The logic that was embedded in the FastAPI server (backend selection,
workspace setup, agent invocation, response shaping) extracted so
that both transports share one implementation:

  - miner/server.py   (HTTP / FastAPI, the docker-compose dev path)
  - neurons/miner.py  (Bittensor axon, the subnet path)

``run_assignment`` is the single entry point: give it a
TaskAssignment, get back a MinerResponse. It owns the workspace
lifecycle (unbundle repo, run agent, clean up) and never trusts the
agent's self-report beyond what the backend's own hermetic
verification produced.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import traceback
import uuid

from config import MINER_BACKEND
from protocol import PROTOCOL_VERSION
from protocol.schemas import MinerResponse, TaskAssignment
from protocol.transport import unbundle_repo


MINER_ID = os.environ.get("MINER_ID") or f"miner-{uuid.uuid4().hex[:8]}"

# Ceiling on the wall-clock a single assignment may hold this miner,
# regardless of what the validator requested. Protects the miner from
# a hostile or buggy validator sending timeout_seconds=10**9 and
# parking the miner forever.
MAX_TASK_SECONDS = int(os.environ.get("BITSWARM_MAX_TASK_SECONDS", "3600"))


def select_backend():
    """Resolve ``MINER_BACKEND`` to an ``execute_subtask`` callable.

    Backends are imported lazily so a miner picks up only the deps it
    needs (no openai package required for SDK runs; no anthropic SDK
    required for subprocess runs, etc.).

    Recognised values:
      - "sdk" / "anthropic" (default): metered Anthropic API call loop
      - "claude_code":                 ``claude`` CLI subprocess (subscription auth)
      - "openai":                      any OpenAI-compatible Chat Completions endpoint
    """
    if MINER_BACKEND == "claude_code":
        from miner.agent_cc import execute_subtask as _impl
        print(f"[Miner {MINER_ID}] backend=claude_code (subprocess, no API spend)")
        return _impl
    if MINER_BACKEND == "openai":
        from miner.agent_openai import execute_subtask as _impl
        from config import OPENAI_BASE_URL, OPENAI_MODEL
        print(f"[Miner {MINER_ID}] backend=openai "
              f"(provider={OPENAI_BASE_URL or 'api.openai.com'}, model={OPENAI_MODEL})")
        return _impl
    if MINER_BACKEND in ("", "sdk", "anthropic"):
        from miner.agent import execute_subtask as _impl
        print(f"[Miner {MINER_ID}] backend=sdk (Anthropic API, metered)")
        return _impl
    raise RuntimeError(
        f"Unknown MINER_BACKEND={MINER_BACKEND!r}. "
        f"Set to 'sdk' (default), 'claude_code', or 'openai'."
    )


def _run_agent_blocking(task: TaskAssignment, execute_subtask):
    """Run the async agent inside a dedicated worker thread loop."""
    subtask = dict(task.subtask_manifest) if task.subtask_manifest else {}
    subtask["subtask_id"] = task.subtask_id
    subtask.setdefault("allowed_files", list(task.allowed_files))
    subtask.setdefault("stub_test_files", list(task.stub_test_files))

    shared_files_content = task.shared_files
    repo_path = subtask.pop("_repo_path")

    kwargs = {}
    if getattr(task, "mode", "scaffold") == "diff":
        kwargs = {
            "mode": "diff",
            "target_stubs": task.target_stubs,
            "new_test_files_content": task.new_test_files_content,
            "shared_additions_content": task.shared_additions_content,
        }

    return asyncio.run(execute_subtask(
        subtask=subtask,
        repo_path=repo_path,
        all_subtask_files=task.all_subtask_files,
        shared_files=shared_files_content,
        shared_files_content=shared_files_content,
        stub_files_content=task.stub_files_content,
        test_files_content=task.test_files_content,
        all_subtasks=task.all_subtasks or None,
        **kwargs,
    ))


async def run_assignment(task: TaskAssignment,
                          execute_subtask=None) -> MinerResponse:
    """Execute one TaskAssignment end to end and shape the response.

    Owns the workspace lifecycle. ``execute_subtask`` is injectable
    for tests; defaults to the configured backend.
    """
    if execute_subtask is None:
        execute_subtask = select_backend()

    version = getattr(task, "protocol_version", 1)
    if version != PROTOCOL_VERSION:
        return MinerResponse(
            task_id=task.task_id,
            subtask_id=task.subtask_id,
            patch="",
            stub_tests_passed=False,
            error_message=(
                f"protocol version mismatch: assignment is v{version}, "
                f"this miner speaks v{PROTOCOL_VERSION}. Upgrade the "
                f"older side."),
            stop_reason="protocol_mismatch",
        )

    workspace = tempfile.mkdtemp(prefix=f"miner_{task.subtask_id}_")
    repo_path = os.path.join(workspace, "repo")
    started = time.perf_counter()

    try:
        unbundle_repo(task.repo_bundle, repo_path)

        subtask_manifest = dict(task.subtask_manifest) if task.subtask_manifest else {}
        subtask_manifest["_repo_path"] = repo_path
        task_with_path = task.model_copy(update={"subtask_manifest": subtask_manifest})

        requested = task.timeout_seconds if task.timeout_seconds > 0 else MAX_TASK_SECONDS
        timeout = min(requested, MAX_TASK_SECONDS)

        result = await asyncio.wait_for(
            asyncio.to_thread(_run_agent_blocking, task_with_path, execute_subtask),
            timeout=timeout,
        )

        elapsed = time.perf_counter() - started
        stop_reason = str(result.stop_reason) if result.stop_reason else ""
        return MinerResponse(
            task_id=task.task_id,
            subtask_id=task.subtask_id,
            patch=result.patch or "",
            stub_tests_passed=bool(result.tests_passed),
            stub_test_output=result.test_output or "",
            files_modified=list(result.files_modified or []),
            execution_time_seconds=elapsed,
            iterations_used=int(result.iterations_used or 0),
            stop_reason=stop_reason,
        )

    except asyncio.TimeoutError:
        elapsed = time.perf_counter() - started
        return MinerResponse(
            task_id=task.task_id,
            subtask_id=task.subtask_id,
            patch="",
            stub_tests_passed=False,
            error_message=f"timeout after {task.timeout_seconds}s",
            execution_time_seconds=elapsed,
            stop_reason="timeout",
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        tb = traceback.format_exc()
        return MinerResponse(
            task_id=task.task_id,
            subtask_id=task.subtask_id,
            patch="",
            stub_tests_passed=False,
            error_message=f"{type(exc).__name__}: {exc}\n{tb}",
            execution_time_seconds=elapsed,
            stop_reason="error",
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
