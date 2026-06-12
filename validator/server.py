"""
BitSwarm Validator Server

Orchestrates a full BitSwarm run:
  1. Set up working repo
  2. Decompose via coordinator (validator/decomposer.py)
  3. Scaffold the decomposition into the repo
  4. Distribute subtasks to miners over HTTP (the Phase 3 replacement
     for the POC's local asyncio.gather loop)
  5. Merge + cross-compile + score (validator/merge.py)

Exposes a small FastAPI app with POST /submit and GET /status, plus a
run_task() coroutine that can be driven directly for local development.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from config import SUBTASK_TIMEOUT_SECONDS
from protocol.schemas import (
    MinerResponse,
    ScoreReport,
    StatusResponse,
    TaskAssignment,
)
from protocol.transport import bundle_repo
from validator.decomposer import decompose
from validator.merge import merge_and_test
from validator.scaffolder import write_scaffolding
from validator.validator_checks import validate_decomposition


VALIDATOR_ID = os.environ.get("VALIDATOR_ID") or f"validator-{uuid.uuid4().hex[:8]}"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


@dataclass
class RemoteMinerResult:
    """Shim matching the subset of miner.agent.MinerResult read by merge + scorer."""
    subtask_id: str
    patch: str = ""
    tests_passed: bool = False
    test_output: str = ""
    iterations_used: int = 0
    stop_reason: str = ""
    files_modified: list = field(default_factory=list)
    merge_conflict: bool = False


def _setup_working_repo(source_path: str, workspace_dir: str) -> str:
    """Copy source repo to workspace/repo and git-init it."""
    if os.path.exists(workspace_dir):
        shutil.rmtree(workspace_dir)
    repo_path = os.path.join(workspace_dir, "repo")
    shutil.copytree(source_path, repo_path)

    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial target repo"],
        cwd=repo_path, capture_output=True, env=GIT_ENV,
    )
    return repo_path


async def _send_to_miner(
    client: httpx.AsyncClient,
    miner_url: str,
    assignment: TaskAssignment,
) -> MinerResponse:
    """POST a TaskAssignment to a miner and return its MinerResponse."""
    resp = await client.post(
        f"{miner_url.rstrip('/')}/task",
        json=assignment.model_dump(),
    )
    resp.raise_for_status()
    return MinerResponse(**resp.json())


async def dispatch_to_miners(
    decomposition: dict,
    scaffolded_repo: str,
    miner_urls: list[str],
    subtask_timeout: int = SUBTASK_TIMEOUT_SECONDS,
    task_id: str = "",
) -> dict[str, RemoteMinerResult]:
    """
    Bundle the scaffolded repo, build a TaskAssignment per subtask,
    round-robin distribute across miner_urls, gather responses.

    Returns {subtask_id: RemoteMinerResult}, suitable for merge_and_test.
    """
    if not miner_urls:
        raise ValueError("dispatch_to_miners requires at least one miner_url")

    task_id = task_id or str(uuid.uuid4())
    bundle_b64 = bundle_repo(scaffolded_repo)

    mode = decomposition.get("mode", "scaffold")
    subtasks = decomposition["subtasks"]
    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    test_files = decomposition.get("stub_test_files", {})
    target_stubs = decomposition.get("target_stubs", {}) or {}
    new_test_files_content = decomposition.get("new_test_files", {}) or {}
    shared_additions = decomposition.get("shared_additions", {}) or {}

    # allowed_files per subtask. Scaffold mode: stubs + tests (the
    # miner may adjust its own tests; see orchestrator rationale).
    # Diff mode: modify_files ONLY; new tests are the validator-owned
    # read-only contract and miner edits to them never ship.
    for st in subtasks:
        if mode == "diff":
            st["allowed_files"] = list(dict.fromkeys(
                st.get("modify_files", []) or []))
        else:
            stub_f = st.get("stub_files", [])
            test_f = st.get("stub_test_files", [])
            st["allowed_files"] = list(dict.fromkeys(stub_f + test_f))

    all_subtask_files = {st["subtask_id"]: st["allowed_files"] for st in subtasks}

    # HTTP timeout = subtask timeout + buffer for unbundle + return transit
    http_timeout = subtask_timeout + 60

    async def one(subtask: dict, miner_url: str) -> tuple[str, MinerResponse | Exception]:
        sid = subtask["subtask_id"]
        assignment = TaskAssignment(
            task_id=task_id,
            subtask_id=sid,
            repo_bundle=bundle_b64,
            subtask_description=subtask.get("description", ""),
            allowed_files=subtask["allowed_files"],
            stub_test_files=subtask.get("stub_test_files", []),
            timeout_seconds=subtask_timeout,
            subtask_manifest=subtask,
            shared_files=shared_files,
            all_subtask_files=all_subtask_files,
            stub_files_content=stub_files,
            test_files_content=test_files,
            all_subtasks=subtasks,
            mode=mode,
            target_stubs=target_stubs,
            new_test_files_content=new_test_files_content,
            shared_additions_content=shared_additions,
        )
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as client:
                resp = await _send_to_miner(client, miner_url, assignment)
            return sid, resp
        except Exception as exc:
            return sid, exc

    coros = []
    for i, st in enumerate(subtasks):
        miner_url = miner_urls[i % len(miner_urls)]
        print(f"  [Dispatch] {st['subtask_id']} -> {miner_url}")
        coros.append(one(st, miner_url))

    pairs = await asyncio.gather(*coros)

    miner_results: dict[str, RemoteMinerResult] = {}
    for sid, outcome in pairs:
        if isinstance(outcome, Exception):
            print(f"  [Dispatch] {sid} ERROR: {type(outcome).__name__}: {outcome}")
            miner_results[sid] = RemoteMinerResult(
                subtask_id=sid,
                patch="",
                tests_passed=False,
                test_output=str(outcome),
                stop_reason="http_error",
            )
            continue

        miner_results[sid] = RemoteMinerResult(
            subtask_id=sid,
            patch=outcome.patch,
            tests_passed=outcome.stub_tests_passed,
            test_output=outcome.stub_test_output,
            iterations_used=outcome.iterations_used,
            stop_reason=outcome.stop_reason,
            files_modified=list(outcome.files_modified or []),
        )
        status = "PASSED" if outcome.stub_tests_passed else "FAILED"
        print(f"  [Dispatch] {sid} {status} "
              f"(iters={outcome.iterations_used}, stop={outcome.stop_reason})")

    return miner_results


async def run_task(
    spec: str,
    target_repo_path: str,
    miner_urls: list[str],
    output_dir: str,
    subtask_timeout: int = SUBTASK_TIMEOUT_SECONDS,
    mode: str = "scaffold",
) -> dict:
    """Full pipeline: decompose, scaffold, dispatch to miners, merge, score.

    ``mode``: "scaffold" (build from spec; default) or "diff" (modify
    the existing target repo). Diff mode uses the diff-mode coordinator
    prompts, the diff structural validator (selected automatically by
    decompose), and the dual-gate merge pipeline.
    """
    if not miner_urls:
        raise ValueError("run_task requires at least one miner URL")

    os.makedirs(output_dir, exist_ok=True)
    task_id = str(uuid.uuid4())
    workspace_dir = os.path.join(output_dir, "workspace")

    print(f"\n[Validator] task={task_id} mode={mode} miners={miner_urls}")

    # Step 1: working repo
    print("[1/5] Setting up working repo...")
    repo_path = _setup_working_repo(target_repo_path, workspace_dir)

    # Step 2: install base requirements (best-effort)
    req_file = os.path.join(repo_path, "requirements.txt")
    if os.path.isfile(req_file):
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req_file],
            capture_output=True,
        )

    # Step 3: decompose. Scaffold mode uses the Phase 1.5 validator;
    # diff mode lets decompose() pick the diff structural validator.
    print("[2/5] Running coordinator decomposition...")
    decomposition = decompose(
        repo_path=repo_path,
        feature_spec=spec,
        validate_fn=validate_decomposition if mode == "scaffold" else None,
        debug_dir=os.path.join(output_dir, "debug"),
        mode=mode,
    )
    if decomposition is None:
        raise RuntimeError("coordinator failed to produce a valid decomposition")

    with open(os.path.join(output_dir, "decomposition.json"), "w") as f:
        json.dump(decomposition, f, indent=2)

    # Step 4: scaffold
    print("[3/5] Writing scaffolding...")
    write_scaffolding(decomposition, repo_path)

    scaffolded_snapshot = os.path.join(output_dir, "scaffolded_repo")
    if os.path.exists(scaffolded_snapshot):
        shutil.rmtree(scaffolded_snapshot)
    shutil.copytree(repo_path, scaffolded_snapshot)

    # Step 5: dispatch to miners over HTTP
    print(f"[4/5] Dispatching {len(decomposition['subtasks'])} subtasks "
          f"to {len(miner_urls)} miner(s)...")
    miner_results = await dispatch_to_miners(
        decomposition=decomposition,
        scaffolded_repo=repo_path,
        miner_urls=miner_urls,
        subtask_timeout=subtask_timeout,
        task_id=task_id,
    )

    # Step 6: merge + score
    print("[5/5] Merging and testing...")
    merge_result = await merge_and_test(decomposition, miner_results, repo_path)

    merged_snapshot = os.path.join(output_dir, "merged_repo")
    if os.path.exists(merged_snapshot):
        shutil.rmtree(merged_snapshot)
    shutil.copytree(merge_result["merge_repo"], merged_snapshot)

    total_score = sum(merge_result["scores"].values())

    print("\n[Validator] RESULTS")
    for sid, score in merge_result["scores"].items():
        print(f"  {sid}: {score:.3f}")
    ratio_pct = int(merge_result.get("integration_ratio", 0.0) * 100)
    print(f"  integration: {'PASS' if merge_result['integration_passed'] else 'FAIL'} "
          f"({ratio_pct}%)")
    print(f"  total: {total_score:.3f} / 1.000")

    return {
        "task_id": task_id,
        "scores": merge_result["scores"],
        "integration_passed": merge_result["integration_passed"],
        "integration_ratio": merge_result.get("integration_ratio", 0.0),
        "total_score": total_score,
        "repairs_made": merge_result.get("repairs_made", {}),
        "output_dir": output_dir,
    }


# FastAPI surface


class SubmitRequest(BaseModel):
    spec: str
    target_repo_path: str
    miner_urls: list[str]
    output_dir: str = ""
    subtask_timeout: int = SUBTASK_TIMEOUT_SECONDS


class SubmitResponse(BaseModel):
    task_id: str
    integration_passed: bool
    integration_ratio: float
    scores: dict[str, float]
    total_score: float
    output_dir: str
    repairs_made: dict = Field(default_factory=dict)


class ValidatorState:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.current_task_id: str = ""


vstate = ValidatorState()
app = FastAPI(title="BitSwarm Validator")


@app.get("/status")
async def status_get() -> StatusResponse:
    return StatusResponse(
        available=not vstate.lock.locked(),
        current_task_id=vstate.current_task_id,
        miner_id=VALIDATOR_ID,
    )


@app.post("/submit")
async def submit(req: SubmitRequest) -> SubmitResponse:
    if not os.path.isdir(req.target_repo_path):
        raise HTTPException(status_code=400,
                            detail=f"target_repo_path not found: {req.target_repo_path}")
    if not req.miner_urls:
        raise HTTPException(status_code=400, detail="miner_urls required")

    output_dir = req.output_dir or tempfile.mkdtemp(prefix="bitswarm_run_")

    async with vstate.lock:
        try:
            result = await run_task(
                spec=req.spec,
                target_repo_path=req.target_repo_path,
                miner_urls=req.miner_urls,
                output_dir=output_dir,
                subtask_timeout=req.subtask_timeout,
            )
            vstate.current_task_id = result["task_id"]
            return SubmitResponse(**result)
        finally:
            vstate.current_task_id = ""


@app.post("/score_report")
async def score_report(task_id: str, scores: dict[str, float],
                       integration_passed: bool = False,
                       integration_ratio: float = 0.0) -> ScoreReport:
    return ScoreReport(
        task_id=task_id,
        scores=scores,
        integration_passed=integration_passed,
        integration_ratio=integration_ratio,
        total_score=sum(scores.values()),
    )


def main():
    import uvicorn

    host = os.environ.get("VALIDATOR_HOST", "0.0.0.0")
    port = int(os.environ.get("VALIDATOR_PORT", "8080"))
    uvicorn.run("validator.server:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
