"""
Run BitSwarm in DIFF MODE against an existing codebase.

Usage:
    python demo/run_pipeline_diff.py \\
        --target /path/to/existing/repo \\
        --spec   /path/to/change_spec.txt \\
        --out    out/diff_run_1

Pipeline:
  1. Copy the target repo into a clean working dir + git-init it.
  2. Coordinator runs in mode='diff': subtasks declaring modify_files
     + new_test_files + behavior_spec, plus a target_stub per
     modify_file (the post-edit public API as real source code).
  3. Diff-mode structural validation (validator/diff_validator.py).
  4. Scaffolder writes the NEW test files (and any new shared types)
     and commits as the diff baseline. Existing source is untouched.
  5. Pre-mining regression baseline: capture which existing tests
     already fail, so the regression gate later counts only NEWLY
     failing tests.
  6. One miner per subtask, in dependency order, each in an isolated
     workspace copy, all in mode='diff' with hermetic patch-replay
     self-verification.
  7. Shared merge + dual-gate scoring via
     validator.diff_merge.merge_and_test_diff: dependency-ordered
     patch application, canonical-test restore, additive gate (with
     repair on failure), regression gate, honesty overrides.

Backends default to COORDINATOR_BACKEND=claude_code and
MINER_BACKEND=claude_code so the run is free on a Max/Pro/Team
subscription. Override via env (e.g. MINER_BACKEND=openai with the
MINER_OPENAI_* vars to run miners on Chutes / DeepSeek / vLLM).
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

# Default both backends to claude_code (free on subscription).
os.environ.setdefault("COORDINATOR_BACKEND", "claude_code")
os.environ.setdefault("MINER_BACKEND", "claude_code")

from validator.diff_merge import (  # noqa: E402
    GIT_ENV,
    collect_failing_nodeids,
    discover_existing_tests,
    merge_and_test_diff,
    topological_order,
)


def _select_miner_backend():
    """Resolve MINER_BACKEND to an async execute_subtask callable."""
    backend = os.environ.get("MINER_BACKEND", "claude_code").strip().lower()
    if backend == "claude_code":
        from miner.agent_cc import execute_subtask
        return execute_subtask, "claude_code"
    if backend == "openai":
        from miner.agent_openai import execute_subtask
        return execute_subtask, "openai"
    if backend in ("", "sdk", "anthropic"):
        from miner.agent import execute_subtask
        return execute_subtask, "sdk"
    raise SystemExit(f"unknown MINER_BACKEND={backend!r}")


def _setup_working_repo(source: str, dest: str) -> str:
    """Copy source repo into dest/repo and git-init it."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    repo = os.path.join(dest, "repo")
    shutil.copytree(source, repo, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".pytest_cache", "node_modules",
        ".venv", "venv", "dist", "build", ".tox", ".mypy_cache",
    ))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial target repo state"],
        cwd=repo, env=GIT_ENV, check=True,
    )
    return repo


async def _run_miners(decomp, scaffolded_repo, miner_workdir, execute_subtask):
    """Run one miner per subtask, sequentially, in DIFF mode."""
    os.makedirs(miner_workdir, exist_ok=True)
    subtasks = decomp["subtasks"]
    target_stubs = decomp.get("target_stubs", {}) or {}
    new_test_files_content = decomp.get("new_test_files", {}) or {}
    shared_additions_content = decomp.get("shared_additions", {}) or {}

    # Patch scope = modify_files ONLY. New test files are the
    # validator-owned contract; miner edits to them never ship.
    for st in subtasks:
        st["allowed_files"] = list(dict.fromkeys(st.get("modify_files", []) or []))

    all_subtask_files = {s["subtask_id"]: s["allowed_files"] for s in subtasks}

    results = {}
    miner_timeout = int(os.environ.get("MINER_TIMEOUT_SECONDS", "1200"))

    for st in topological_order(subtasks):
        sid = st["subtask_id"]
        per_miner = os.path.join(miner_workdir, sid)
        if os.path.exists(per_miner):
            shutil.rmtree(per_miner)
        shutil.copytree(scaffolded_repo, per_miner)

        print(f"\n[pipeline-diff] === miner: {sid} ===")
        started = time.perf_counter()
        result = await execute_subtask(
            subtask=st,
            repo_path=per_miner,
            all_subtask_files=all_subtask_files,
            shared_files=shared_additions_content,
            shared_files_content=shared_additions_content,
            stub_files_content={},   # unused in diff mode
            test_files_content={},   # unused in diff mode
            all_subtasks=subtasks,
            mode="diff",
            target_stubs=target_stubs,
            new_test_files_content=new_test_files_content,
            shared_additions_content=shared_additions_content,
            timeout_seconds=miner_timeout,
        )
        elapsed = time.perf_counter() - started
        results[sid] = result
        print(f"[pipeline-diff] {sid}: "
              f"{'PASSED' if result.tests_passed else 'FAILED'} "
              f"in {elapsed:.1f}s, patch={len(result.patch)} chars")
    return results


async def run(spec_path, target_repo, out_dir):
    from validator.decomposer import decompose
    from validator.scaffolder import write_scaffolding
    execute_subtask, miner_backend = _select_miner_backend()

    print(f"[pipeline-diff] spec:           {spec_path}")
    print(f"[pipeline-diff] target_repo:    {target_repo}")
    print(f"[pipeline-diff] out_dir:        {out_dir}")
    print(f"[pipeline-diff] coord backend:  {os.environ.get('COORDINATOR_BACKEND')}")
    print(f"[pipeline-diff] miner backend:  {miner_backend}")

    if not os.path.isfile(spec_path):
        print(f"[pipeline-diff] FAIL: spec not found: {spec_path}")
        return 2
    if not os.path.isdir(target_repo):
        print(f"[pipeline-diff] FAIL: target repo not found: {target_repo}")
        return 2

    spec = open(spec_path).read()
    os.makedirs(out_dir, exist_ok=True)

    workspace = os.path.join(out_dir, "workspace")
    repo_path = _setup_working_repo(target_repo, workspace)

    print("\n[pipeline-diff] === coordinator (diff mode) ===")
    decomp = decompose(
        repo_path=repo_path,
        feature_spec=spec,
        debug_dir=os.path.join(out_dir, "debug"),
        mode="diff",
    )
    if decomp is None:
        print("[pipeline-diff] FAIL: coordinator returned None after all retries")
        return 1
    print(f"[pipeline-diff] coordinator produced {len(decomp['subtasks'])} subtask(s)")
    for st in decomp["subtasks"]:
        print(f"           - {st['subtask_id']}: {st.get('description', '')[:80]}")
    with open(os.path.join(out_dir, "decomposition.json"), "w") as f:
        json.dump(decomp, f, indent=2)

    print("\n[pipeline-diff] === scaffold (diff baseline) ===")
    write_scaffolding(decomp, repo_path)

    scaffolded_snapshot = os.path.join(out_dir, "scaffolded_repo")
    if os.path.exists(scaffolded_snapshot):
        shutil.rmtree(scaffolded_snapshot)
    shutil.copytree(repo_path, scaffolded_snapshot)

    new_test_paths = []
    for st in decomp["subtasks"]:
        for p in st.get("new_test_files", []) or []:
            if p not in new_test_paths:
                new_test_paths.append(p)
    existing_tests = discover_existing_tests(repo_path, exclude=new_test_paths)
    print(f"\n[pipeline-diff] existing test files (regression gate): "
          f"{len(existing_tests)}")
    print(f"[pipeline-diff] new test files (additive gate): {len(new_test_paths)}")
    for t in new_test_paths:
        print(f"           - {t}")

    print("\n[pipeline-diff] === pre-mining regression baseline ===")
    pre_failing, _ = collect_failing_nodeids(repo_path, existing_tests,
                                              timeout=600)
    print(f"[pipeline-diff] existing tests on unmodified repo: "
          f"{len(pre_failing)} pre-existing failure(s)")
    for nid in sorted(pre_failing)[:10]:
        print(f"           - {nid}")

    print("\n[pipeline-diff] === miners ===")
    miner_results = await _run_miners(
        decomp,
        scaffolded_repo=repo_path,
        miner_workdir=os.path.join(out_dir, "miner_repos"),
        execute_subtask=execute_subtask,
    )

    print("\n[pipeline-diff] === merge + dual-gate scoring ===")
    merge_result = await merge_and_test_diff(
        decomp, miner_results, scaffolded_snapshot,
        out_dir=out_dir, pre_failing=pre_failing,
    )

    print("\n[pipeline-diff] === SCORES ===")
    for st in decomp["subtasks"]:
        sid = st["subtask_id"]
        add_ok = merge_result["additive_results"].get(sid, False)
        applied = merge_result["patch_applied"].get(sid, False)
        print(f"  {sid:25s}  score={merge_result['scores'][sid]:.3f}  "
              f"patch={'OK  ' if applied else 'FAIL'}  "
              f"new_tests={'PASS' if add_ok else 'FAIL'}")
    reg = merge_result["regression_passed"]
    n_new = len(merge_result["newly_failing"])
    print(f"  regression_multiplier      {1.0 if reg else 0.5:.2f} "
          f"({'no new regressions' if reg else f'{n_new} new regression(s)'})")
    print(f"  TOTAL                      {merge_result['total']:.3f} / 1.000")

    print(f"\n[pipeline-diff] merged repo: {merge_result['merge_repo']}")
    print(f"[pipeline-diff] decomposition: "
          f"{os.path.join(out_dir, 'decomposition.json')}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spec", required=True, help="path to change spec text file")
    parser.add_argument("--target", required=True,
                         help="path to existing target repo (will be copied)")
    parser.add_argument("--out", required=True, help="output directory")
    args = parser.parse_args()
    return asyncio.run(run(args.spec, args.target, args.out))


if __name__ == "__main__":
    sys.exit(main())
