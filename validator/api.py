"""
Task submission API.

The user-facing front door of a validator: a small FastAPI service
that accepts feature specs over HTTP, drops them into the task inbox
that the validator neuron polls, and serves back status, the scored
result, and the verified patch. It shares nothing with the neuron but
the two directories, so either process can restart without the other
noticing.

Auth is API-key based: set BITSWARM_API_KEYS to a comma-separated
list and give one key per user. Requests present it as the
``X-API-Key`` header. The service refuses to start keyless unless
BITSWARM_API_ALLOW_ANON=1 (development only).

Run it next to the validator neuron:

    export BITSWARM_API_KEYS=key1,key2
    python -m validator.api --inbox ./task_inbox --output ./validator_runs \
        --port 8100

Submit work:

    curl -X POST http://validator:8100/tasks \
      -H "X-API-Key: key1" -H "Content-Type: application/json" \
      -d '{"spec": "Build a wordle clone with tests", "mode": "scaffold",
           "repo_bundle": "<base64 git bundle>"}'

Poll GET /tasks/{id} until status is "done", then fetch the verified
change from GET /tasks/{id}/patch.
"""
from __future__ import annotations

import argparse
import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from validator import inbox as inbox_mod


class TaskSubmission(BaseModel):
    spec: str
    mode: str = "scaffold"
    target_repo: str | None = Field(
        default=None,
        description="Absolute path on the validator host (operator use)")
    repo_bundle: str | None = Field(
        default=None,
        description="Base64 git bundle of the repo to build on")
    subtask_timeout: int = 1200


def _api_keys() -> set:
    raw = os.environ.get("BITSWARM_API_KEYS", "")
    return {k.strip() for k in raw.split(",") if k.strip()}


def create_app(inbox_dir: str, output_dir: str) -> FastAPI:
    keys = _api_keys()
    anon_ok = os.environ.get("BITSWARM_API_ALLOW_ANON", "") == "1"
    if not keys and not anon_ok:
        raise RuntimeError(
            "no API keys configured. Set BITSWARM_API_KEYS=key1,key2 "
            "(or BITSWARM_API_ALLOW_ANON=1 for local development)."
        )
    os.makedirs(inbox_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    uploads_dir = os.path.join(output_dir, "uploads")

    app = FastAPI(title="BitSwarm validator API", version="1.0")

    def require_key(x_api_key: str = Header(default="")):
        if keys and x_api_key not in keys:
            raise HTTPException(status_code=401, detail="invalid API key")

    @app.get("/health")
    def health():
        entries = os.listdir(inbox_dir) if os.path.isdir(inbox_dir) else []
        return {
            "status": "ok",
            "pending": sum(1 for e in entries if e.endswith(".json")),
            "working": sum(1 for e in entries if e.endswith(".working")),
        }

    @app.post("/tasks", dependencies=[Depends(require_key)])
    def submit(sub: TaskSubmission):
        if sub.mode not in ("scaffold", "diff"):
            raise HTTPException(400, "mode must be 'scaffold' or 'diff'")
        if not sub.spec.strip():
            raise HTTPException(400, "spec is empty")
        if bool(sub.target_repo) == bool(sub.repo_bundle):
            raise HTTPException(
                400, "provide exactly one of target_repo or repo_bundle")

        import uuid
        task_id = uuid.uuid4().hex

        if sub.repo_bundle:
            from protocol.transport import unbundle_repo
            target = os.path.join(uploads_dir, task_id, "repo")
            try:
                unbundle_repo(sub.repo_bundle, target)
            except Exception as exc:
                raise HTTPException(400, f"bad repo bundle: {exc}")
        else:
            target = sub.target_repo
            if not os.path.isdir(target):
                raise HTTPException(
                    400, f"target_repo does not exist on this host: {target}")

        inbox_mod.submit_task(
            inbox_dir, spec=sub.spec, target_repo=target, mode=sub.mode,
            subtask_timeout=sub.subtask_timeout, task_id=task_id,
        )
        return {"task_id": task_id, "status": "pending"}

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_key)])
    def status(task_id: str):
        st = inbox_mod.task_status(inbox_dir, task_id)
        if st == "unknown":
            raise HTTPException(404, "no such task")
        body = {"task_id": task_id, "status": st}
        if st == "done":
            result = inbox_mod.load_result(output_dir, task_id)
            if result is not None:
                body["result"] = result
        return body

    @app.get("/tasks/{task_id}/patch",
             dependencies=[Depends(require_key)])
    def patch(task_id: str):
        text = inbox_mod.load_patch(output_dir, task_id)
        if text is None:
            raise HTTPException(
                404, "no patch artifact for this task (not finished, "
                     "failed, or produced no change)")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(text, media_type="text/x-diff")

    return app


def main():
    parser = argparse.ArgumentParser(description="BitSwarm validator API")
    parser.add_argument("--inbox", default="./task_inbox")
    parser.add_argument("--output", default="./validator_runs")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    import uvicorn
    uvicorn.run(create_app(args.inbox, args.output),
                host=args.host, port=args.port)


if __name__ == "__main__":
    main()
