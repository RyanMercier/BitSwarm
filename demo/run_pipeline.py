"""
Run a BitSwarm task end-to-end, in-process, with the Claude Code
backend for BOTH coordinator and miners. Skips the HTTP layer so this
runs as a single script for demos.

Pipeline:
  1. Set up a fresh working repo (copy demo/target_repo).
  2. Run the coordinator (decomposer_cc) on the spec.
  3. Validate the decomposition via Phase 1.5 checks.
  4. Scaffold the stubs + tests into the working repo (git commit as
     ``BitSwarm scaffolding``).
  5. For each subtask in dependency order, copy the scaffolded repo
     into a per-miner workspace and run miner.agent_cc on it.
  6. Run the tiered merge + cross-compile + score pipeline.
  7. Print the final scores + path to the merged repo so you can run
     it interactively.

Usage:
    python demo/run_pipeline.py --spec demo/spec_wordle.txt \\
                                 --out  out/wordle_run

Cost: $0 (uses Claude Code subprocesses via Max OAuth).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Default to subprocess mode for both. Caller can override.
os.environ.setdefault("COORDINATOR_BACKEND", "claude_code")
os.environ.setdefault("MINER_BACKEND", "claude_code")

from miner.agent_cc import execute_subtask  # noqa: E402
from validator.decomposer import decompose  # noqa: E402
from validator.merge import merge_and_test  # noqa: E402
from validator.scaffolder import write_scaffolding  # noqa: E402
from validator.validator_checks import validate_decomposition  # noqa: E402


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


def _setup_working_repo(source: str, dest: str) -> str:
    """Copy ``source`` into ``dest/repo`` and git-init it."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    repo = os.path.join(dest, "repo")
    shutil.copytree(source, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial target repo"],
        cwd=repo, env=GIT_ENV, check=True,
    )
    return repo


def _topological_order(subtasks: list[dict]) -> list[dict]:
    """Order subtasks so dependencies come first. Stable on ties."""
    by_id = {s["subtask_id"]: s for s in subtasks}
    visited: set[str] = set()
    order: list[dict] = []

    def visit(sid: str) -> None:
        if sid in visited or sid not in by_id:
            return
        visited.add(sid)
        for dep in by_id[sid].get("dependencies", []) or []:
            visit(dep)
        order.append(by_id[sid])

    for s in subtasks:
        visit(s["subtask_id"])
    return order


async def _run_miners(decomp: dict, scaffolded_repo: str,
                       miner_workdir: str) -> dict:
    """Run one Claude Code miner per subtask, sequentially.

    Sequential keeps the demo legible (miner output streams in order)
    and dodges any Max-plan rate-limiting from N concurrent claude
    processes. Subtasks within a tier are independent so a parallel
    run is fine in principle.
    """
    os.makedirs(miner_workdir, exist_ok=True)

    subtasks = decomp["subtasks"]
    shared_files = decomp.get("shared_files", {})
    stub_files = decomp.get("stub_files", {})
    test_files = decomp.get("stub_test_files", {})

    # Match how validator/server.py composes allowed_files.
    for st in subtasks:
        stub_f = st.get("stub_files", []) or []
        test_f = st.get("stub_test_files", []) or []
        st["allowed_files"] = list(dict.fromkeys(stub_f + test_f))

    all_subtask_files = {s["subtask_id"]: s["allowed_files"] for s in subtasks}

    results: dict[str, object] = {}
    for st in _topological_order(subtasks):
        sid = st["subtask_id"]
        per_miner = os.path.join(miner_workdir, sid)
        if os.path.exists(per_miner):
            shutil.rmtree(per_miner)
        shutil.copytree(scaffolded_repo, per_miner)

        print(f"\n[pipeline] === miner: {sid} ===")
        started = time.perf_counter()
        # Default 600s is fine for tier-1 subtasks but the heaviest
        # mid-tier ones (game, scorer with subtle bugs) occasionally
        # need more. Override via MINER_TIMEOUT_SECONDS.
        miner_timeout = int(os.environ.get("MINER_TIMEOUT_SECONDS", "1200"))
        result = await execute_subtask(
            subtask=st,
            repo_path=per_miner,
            all_subtask_files=all_subtask_files,
            shared_files=shared_files,
            shared_files_content=shared_files,
            stub_files_content=stub_files,
            test_files_content=test_files,
            all_subtasks=subtasks,
            timeout_seconds=miner_timeout,
        )
        elapsed = time.perf_counter() - started
        results[sid] = result
        print(f"[pipeline] {sid}: {'PASSED' if result.tests_passed else 'FAILED'} "
              f"in {elapsed:.1f}s, patch={len(result.patch)} chars")

    return results


async def run(spec_path: str, target_repo: str, out_dir: str) -> int:
    spec = open(spec_path).read()

    print(f"[pipeline] spec:        {spec_path}")
    print(f"[pipeline] target_repo: {target_repo}")
    print(f"[pipeline] out_dir:     {out_dir}")
    print(f"[pipeline] coord backend: {os.environ.get('COORDINATOR_BACKEND')}")
    print(f"[pipeline] miner backend: {os.environ.get('MINER_BACKEND')}")
    print()

    os.makedirs(out_dir, exist_ok=True)
    workspace = os.path.join(out_dir, "workspace")
    repo_path = _setup_working_repo(target_repo, workspace)

    print("[pipeline] === coordinator ===")
    # Phase 1.5 validation is Python-tuned and produces false positives
    # on C++ (overloaded-constructor arity, header-name conventions).
    # For non-Python runs we skip validation and rely on the miners +
    # integration tests + tiered merge to catch real bugs.
    language = os.environ.get("COORDINATOR_LANGUAGE", "").strip().lower()
    validate_fn = validate_decomposition if language in ("", "python") else None
    if validate_fn is None:
        print(f"[pipeline] (Phase 1.5 validation disabled for language={language})")
    decomp = decompose(
        repo_path=repo_path,
        feature_spec=spec,
        validate_fn=validate_fn,
        debug_dir=os.path.join(out_dir, "debug"),
    )
    if decomp is None:
        print("[pipeline] FAIL: coordinator returned None after all retries")
        return 1

    print(f"[pipeline] coordinator produced {len(decomp['subtasks'])} subtasks")
    for st in decomp["subtasks"]:
        print(f"           - {st['subtask_id']}: {st.get('description', '')[:80]}")
    with open(os.path.join(out_dir, "decomposition.json"), "w") as f:
        json.dump(decomp, f, indent=2)

    print("\n[pipeline] === scaffold ===")
    write_scaffolding(decomp, repo_path)

    # Snapshot the scaffolded repo BEFORE miners modify it.
    scaffolded_snapshot = os.path.join(out_dir, "scaffolded_repo")
    if os.path.exists(scaffolded_snapshot):
        shutil.rmtree(scaffolded_snapshot)
    shutil.copytree(repo_path, scaffolded_snapshot)

    # Pre-flight: confirm the scaffold compiles before spending miner
    # time. Catches Phase 2 interface drift in 30 seconds instead of
    # after a full mining round.
    print("\n[pipeline] === pre-flight ===")
    from validator.preflight import preflight
    preflight_errors = preflight(decomp, repo_path, language=language or None)
    if preflight_errors:
        print(f"[pipeline] pre-flight: {len(preflight_errors)} error(s)")
        for e in preflight_errors:
            print(f"  ! {e}")
        if os.environ.get("BITSWARM_STRICT_PREFLIGHT", "").strip().lower() in (
            "1", "true", "yes"
        ):
            print("[pipeline] BITSWARM_STRICT_PREFLIGHT=1 set -> aborting")
            return 2
        print("[pipeline] continuing anyway (set BITSWARM_STRICT_PREFLIGHT=1 to abort)")
    else:
        print("[pipeline] pre-flight: clean")

    print("\n[pipeline] === miners ===")
    miner_results = await _run_miners(
        decomp,
        scaffolded_repo=repo_path,
        miner_workdir=os.path.join(out_dir, "miner_repos"),
    )

    print("\n[pipeline] === merge + score ===")
    merge_result = await merge_and_test(decomp, miner_results, repo_path)

    merged = os.path.join(out_dir, "merged_repo")
    if os.path.exists(merged):
        shutil.rmtree(merged)
    shutil.copytree(merge_result["merge_repo"], merged)

    print("\n[pipeline] === SCORES ===")
    total = 0.0
    for sid, score in merge_result["scores"].items():
        stub_ok = merge_result["stub_results"].get(sid, False)
        patch_ok = merge_result["patch_applied"].get(sid, False)
        print(f"  {sid:25s}  score={score:.3f}  "
              f"patch={'OK  ' if patch_ok else 'FAIL'}  "
              f"stubs={'PASS' if stub_ok else 'FAIL'}")
        total += score

    integ_ok = merge_result["integration_passed"]
    ratio = merge_result.get("integration_ratio", 1.0 if integ_ok else 0.0)
    print(f"  integration_tests        "
          f"{'PASS' if integ_ok else 'FAIL'} ({int(ratio*100)}%)")
    print(f"  TOTAL                    {total:.3f} / 1.000")

    print(f"\n[pipeline] merged repo: {merged}")
    print(f"[pipeline] decomposition: {os.path.join(out_dir, 'decomposition.json')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--target",
                         default=os.path.join(HERE, "target_repo"),
                         help="empty starter repo (default demo/target_repo)")
    parser.add_argument("--out", required=True,
                         help="output directory")
    args = parser.parse_args()
    return asyncio.run(run(args.spec, args.target, args.out))


if __name__ == "__main__":
    sys.exit(main())
