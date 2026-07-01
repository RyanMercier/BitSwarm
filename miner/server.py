"""
BitSwarm Miner Server (HTTP transport)

FastAPI server that receives TaskAssignments and returns
MinerResponses. The actual execution lives in miner/runtime.py, which
this server shares with the Bittensor axon transport
(neurons/miner.py).

One task at a time per miner, gated by an asyncio.Lock. The agent
loop is CPU/IO blocking (sync Anthropic client, pytest subprocess,
etc.), so the runtime runs it in a worker thread with its own event
loop and /status stays responsive while a task is in flight.
"""
import asyncio
import os

from fastapi import FastAPI, HTTPException

from miner.runtime import MINER_ID, run_assignment, select_backend
from protocol.schemas import (
    MinerResponse,
    StatusCheck,
    StatusResponse,
    TaskAssignment,
)


# Resolved once at import so a misconfigured MINER_BACKEND fails the
# process at startup, not on the first task. Also the seam the
# backend-dispatch tests assert against.
execute_subtask = select_backend()


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
    try:
        await asyncio.wait_for(state.lock.acquire(), timeout=0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=409, detail="miner busy")

    try:
        state.current_task_id = task.task_id or task.subtask_id
        try:
            return await run_assignment(task, execute_subtask)
        finally:
            state.current_task_id = ""
    finally:
        state.lock.release()


def main():
    import uvicorn

    host = os.environ.get("MINER_HOST", "0.0.0.0")
    port = int(os.environ.get("MINER_PORT", "8081"))
    uvicorn.run("miner.server:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
