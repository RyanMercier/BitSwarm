"""Run the experiment matrix with resume.

    python -m redteam.run --models claude-sonnet-cc qwen2.5-coder-32b-awq
    python -m redteam.run --dry-run

Every (task, subtask, model, condition, sample) cell is one
``redteam.agent_run`` subprocess. Run ids are deterministic, so a rerun
skips every id already in results/runs.jsonl. Runs that failed for
infrastructure reasons (CLI rate limit, endpoint down, harness
exception) go to results/run_errors.jsonl instead and are retried on
the next invocation. Order is sample-major, so stopping early leaves a
balanced set of conditions.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESULTS = os.path.join(ROOT, "redteam", "results")
RUNS = os.path.join(RESULTS, "runs.jsonl")
ERRORS = os.path.join(RESULTS, "run_errors.jsonl")
LOGS = os.path.join(RESULTS, "work", "logs")

from redteam.agent_run import CONDITIONS, ELICITATION, IMPOSSIBLE, MODELS, make_run_id  # noqa: E402
from redteam.grade import impossible_spec  # noqa: E402

DEFAULT_CONCURRENCY = {"claude-sonnet-cc": 3, "qwen2.5-coder-32b-awq": 6}
INFRA_MARKERS = ("rate limit", "usage limit", "overloaded", "529", "Connection refused",
                 "APIConnectionError", "Failed to authenticate", "ECONNREFUSED")


def frozen_tasks() -> list[str]:
    base = os.path.join(ROOT, "redteam", "tasks")
    return sorted(t for t in os.listdir(base)
                  if os.path.isfile(os.path.join(base, t, "task.json")))


def done_ids() -> set[str]:
    if not os.path.isfile(RUNS):
        return set()
    return {json.loads(line)["run_id"] for line in open(RUNS) if line.strip()}


def cells(args) -> list[dict]:
    out = []
    tasks = args.tasks or frozen_tasks()
    subtasks = {}
    for t in tasks:
        meta = json.load(open(os.path.join(ROOT, "redteam", "tasks", t, "task.json")))
        subtasks[t] = [s["subtask_id"] for s in meta["subtasks"]]
    for sample in range(args.samples):
        for cond in args.conditions:
            for t in tasks:
                for sid in subtasks[t]:
                    if args.subtasks and sid not in args.subtasks:
                        continue
                    # Impossible conditions exist only where the task
                    # defines a contradicting test for this subtask.
                    if cond in IMPOSSIBLE and sid not in impossible_spec(t):
                        continue
                    for model in args.models:
                        out.append({"task": t, "subtask": sid, "model": model,
                                    "condition": cond, "sample": sample,
                                    "run_id": make_run_id(t, sid, model, cond, sample)})
    return out


def is_infra_error(rec: dict, log_tail: str) -> bool:
    if rec.get("error"):
        return True
    if rec.get("model") == "claude-sonnet-cc" and rec.get("cli_is_error"):
        return True
    if rec.get("turns", 0) == 0 and any(m.lower() in log_tail.lower() for m in INFRA_MARKERS):
        return True
    return False


async def remove_containers(run_id: str) -> None:
    """Force-remove sandbox containers labeled with this run id."""
    try:
        ps = await asyncio.create_subprocess_exec(
            "docker", "ps", "-aq", "--filter", f"label=bitswarm.owner={run_id}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:  # no docker on this host: nothing to clean up
        return
    out, _ = await ps.communicate()
    ids = out.decode().split()
    if ids:
        rm = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", *ids,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await rm.wait()


async def run_cell(cell: dict, sem: asyncio.Semaphore, lock: asyncio.Lock,
                   timeout: int, stats: dict) -> None:
    async with sem:
        os.makedirs(LOGS, exist_ok=True)
        log_path = os.path.join(LOGS, f"{cell['run_id']}.log")
        started = time.time()
        cmd = [sys.executable, "-m", "redteam.agent_run", "--task", cell["task"],
               "--subtask", cell["subtask"], "--model", cell["model"],
               "--condition", cell["condition"], "--sample", str(cell["sample"])]
        # Sandbox containers carry the run id, so the ones a killed run
        # leaves behind (killing the docker client does not stop them)
        # can be removed.
        env = {**os.environ, "BITSWARM_SANDBOX_LABEL": cell["run_id"]}
        with open(log_path, "w") as log:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT, stdout=log,
                                                        stderr=asyncio.subprocess.STDOUT,
                                                        env=env)
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                await remove_containers(cell["run_id"])
        text = open(log_path, errors="replace").read()
        rec_line = next((ln for ln in reversed(text.splitlines()) if ln.startswith("RECORD ")),
                        None)
        async with lock:
            if rec_line is None:
                entry = {"run_id": cell["run_id"], **cell,
                         "error": "no record (crash or timeout)", "log_tail": text[-3000:]}
                with open(ERRORS, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                stats["error"] += 1
                status = "ERROR"
            else:
                rec = json.loads(rec_line[len("RECORD "):])
                if is_infra_error(rec, text[-5000:]):
                    rec["log_tail"] = text[-3000:]
                    with open(ERRORS, "a") as f:
                        f.write(json.dumps(rec) + "\n")
                    stats["error"] += 1
                    status = "INFRA-ERROR"
                else:
                    with open(RUNS, "a") as f:
                        f.write(json.dumps(rec) + "\n")
                    stats["ok"] += 1
                    status = rec.get("label_auto", "?")
            print(f"[{time.strftime('%H:%M:%S')}] {status:18s} {cell['run_id']} "
                  f"({time.time() - started:.0f}s) ok={stats['ok']} err={stats['error']} "
                  f"left={stats['total'] - stats['ok'] - stats['error']}", flush=True)


async def main_async(args) -> int:
    todo = [c for c in cells(args) if c["run_id"] not in done_ids()]
    print(f"{len(todo)} runs to do ({len(done_ids())} already recorded)")
    if args.dry_run:
        for c in todo[:20]:
            print("  ", c["run_id"])
        return 0
    sems = {m: asyncio.Semaphore(args.concurrency.get(m, 2)) for m in args.models}
    lock = asyncio.Lock()
    stats = {"ok": 0, "error": 0, "total": len(todo)}
    await asyncio.gather(*(run_cell(c, sems[c["model"]], lock, args.timeout, stats)
                           for c in todo))
    print(f"done: ok={stats['ok']} errors={stats['error']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=list(MODELS), choices=sorted(MODELS))
    # Elicitation and impossible conditions run only when named explicitly.
    ap.add_argument("--conditions", nargs="+",
                    default=[c for c in CONDITIONS
                             if c not in ELICITATION + IMPOSSIBLE],
                    choices=CONDITIONS)
    ap.add_argument("--tasks", nargs="*")
    ap.add_argument("--subtasks", nargs="*")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--concurrency", type=json.loads, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
