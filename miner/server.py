"""
BitSwarm Miner Server

FastAPI server that receives TaskAssignments and returns MinerResponses.

One task at a time per miner, gated by an asyncio.Lock. The agent loop
inside execute_subtask is CPU/IO blocking (sync Anthropic client, pytest
subprocess, etc.), so we run it in a worker thread with its own event
loop to keep /status responsive while a task is in flight.
"""
import asyncio
import os
import shutil
import tempfile
import time
import traceback
import uuid

from fastapi import FastAPI, HTTPException

from miner.agent import execute_subtask
from protocol.schemas import (
    MinerResponse,
    StatusCheck,
    StatusResponse,
    TaskAssignment,
)
from protocol.transport import unbundle_repo


MINER_ID = os.environ.get("MINER_ID") or f"miner-{uuid.uuid4().hex[:8]}"


class MinerState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.current_task_id: str = ""


state = MinerState()
app = FastAPI(title="BitSwarm Miner")


@app.get("/status")
async def status_get() -> StatusResponse:
    return StatusResponse(
        available=not state.lock.locked(),
        current_task_id=state.current_task_id,
        miner_id=MINER_ID,
    )


@app.post("/status")
async def status_post(check: StatusCheck) -> StatusResponse:
    return await status_get()


@app.post("/task")
async def run_task(task: TaskAssignment) -> MinerResponse:
    # Race-free single-task gate: try to acquire without blocking.
    # The previous ``if lock.locked(): ...; async with lock`` form had a
    # TOCTOU window where two concurrent requests could both pass the
    # check, with the second blocking forever on acquire instead of
    # getting a clean 409.
    try:
        await asyncio.wait_for(state.lock.acquire(), timeout=0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=409, detail="miner busy")

    try:
        state.current_task_id = task.task_id or task.subtask_id
        try:
            return await _run_task(task)
        finally:
            state.current_task_id = ""
    finally:
        state.lock.release()


def _run_agent_blocking(task: TaskAssignment):
    """Run the async agent inside a dedicated worker thread with its own loop."""
    subtask = dict(task.subtask_manifest) if task.subtask_manifest else {}
    subtask["subtask_id"] = task.subtask_id
    subtask.setdefault("allowed_files", list(task.allowed_files))
    subtask.setdefault("stub_test_files", list(task.stub_test_files))

    shared_files_content = task.shared_files

    repo_path = subtask.pop("_repo_path")

    return asyncio.run(execute_subtask(
        subtask=subtask,
        repo_path=repo_path,
        all_subtask_files=task.all_subtask_files,
        shared_files=shared_files_content,
        shared_files_content=shared_files_content,
        stub_files_content=task.stub_files_content,
        test_files_content=task.test_files_content,
        all_subtasks=task.all_subtasks or None,
    ))


async def _run_task(task: TaskAssignment) -> MinerResponse:
    workspace = tempfile.mkdtemp(prefix=f"miner_{task.subtask_id}_")
    repo_path = os.path.join(workspace, "repo")
    started = time.perf_counter()

    try:
        unbundle_repo(task.repo_bundle, repo_path)

        # Stash repo_path in the manifest so the threaded runner can read it
        # (keeps the thread entry point signature tidy).
        subtask_manifest = dict(task.subtask_manifest) if task.subtask_manifest else {}
        subtask_manifest["_repo_path"] = repo_path
        task_with_path = task.model_copy(update={"subtask_manifest": subtask_manifest})

        timeout = task.timeout_seconds if task.timeout_seconds > 0 else None

        result = await asyncio.wait_for(
            asyncio.to_thread(_run_agent_blocking, task_with_path),
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


def main():
    import uvicorn

    host = os.environ.get("MINER_HOST", "0.0.0.0")
    port = int(os.environ.get("MINER_PORT", "8081"))
    uvicorn.run("miner.server:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
