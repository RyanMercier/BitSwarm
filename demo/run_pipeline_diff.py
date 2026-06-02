"""
Run BitSwarm in DIFF MODE against an existing codebase.

Usage:
    python demo/run_pipeline_diff.py \\
        --target /path/to/existing/repo \\
        --spec   /path/to/change_spec.txt \\
        --out    out/diff_run_1

Pipeline:
  1. Copy the target repo into a clean working dir + git-init it
     (preserves the original on disk, lets us iterate).
  2. Coordinator runs in mode='diff': produces subtasks each declaring
     modify_files + new_test_files + behavior_spec, plus a target_stub
     per modify_file.
  3. Validator runs the diff-mode structural validator.
  4. Scaffolder writes the NEW test files (and any new shared types)
     into the working repo. Existing source files are not touched.
     Commits as the diff baseline.
  5. Snapshots the BEFORE state of every file any miner will modify
     so we can score the regression gate vs the original.
  6. For each subtask (in dep order): copy the scaffolded repo into a
     per-miner workspace, run the chosen miner backend in mode='diff'.
     The miner sees current file content + target stub + new tests
     and produces a patch.
  7. Tiered merge: apply patches in dependency order. After each tier
     re-run the new tests against the merged state (additive gate).
  8. Final regression gate: run the project's existing test suite
     against the fully-merged repo. The existing tests are the
     "regression gate" -- anything we change must not break them.
  9. Dual-gate scoring: per-subtask score = complexity_weight
     * additive_pass * regression_multiplier.

Backends:
  - Defaults to COORDINATOR_BACKEND=claude_code, MINER_BACKEND=claude_code
    so the run is free on a Max/Pro/Team subscription. Override either
    via env (MINER_BACKEND=openai with the corresponding OPENAI_* vars,
    for example).

This script intentionally does NOT use the full tiered merge + repair
pipeline from validator/merge.py because that pipeline is currently
scaffold-mode-specific. Diff-mode merge here is a simpler "apply each
tier in order, run new tests, run regression suite at the end."
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

# Default both backends to claude_code (free, what we've been using).
os.environ.setdefault("COORDINATOR_BACKEND", "claude_code")
os.environ.setdefault("MINER_BACKEND", "claude_code")


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


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
    """Copy source repo into dest/repo and git-init (so the diff
    baseline + patch generation has a clean history to work against)."""
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


def _topological_order(subtasks):
    """Return subtasks in dependency order (deps first)."""
    by_id = {s["subtask_id"]: s for s in subtasks}
    visited = set()
    order = []

    def visit(sid):
        if sid in visited or sid not in by_id:
            return
        visited.add(sid)
        for dep in by_id[sid].get("dependencies", []) or []:
            visit(dep)
        order.append(by_id[sid])

    for s in subtasks:
        visit(s["subtask_id"])
    return order


def _discover_existing_tests(repo_path, exclude):
    """Find pytest-collectable test files in the repo, minus any path
    that the coordinator declared as a NEW test file (those are the
    additive gate, not the regression gate)."""
    exclude_abs = set(
        os.path.normpath(os.path.join(repo_path, p)) for p in exclude
    )
    found = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in (
            ".git", "__pycache__", "node_modules", ".venv", "venv",
            ".pytest_cache", ".tox", "dist", "build",
        )]
        for f in files:
            if not (f.startswith("test_") and f.endswith(".py")):
                continue
            full = os.path.normpath(os.path.join(root, f))
            if full in exclude_abs:
                continue
            found.append(os.path.relpath(full, repo_path))
    return sorted(found)


def _run_pytest(repo_path, test_files, timeout=300):
    """Run pytest on a list of test files; return (passed, output)."""
    if not test_files:
        return True, "(no tests to run)"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *test_files, "-x", "--tb=short", "-q"],
            cwd=repo_path, capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + (
            ("\n[stderr]\n" + result.stderr) if result.stderr else ""
        )
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "[TIMEOUT]"
    except Exception as exc:
        return False, f"[ERROR: {exc}]"


async def _run_miners(decomp, scaffolded_repo, miner_workdir, execute_subtask):
    """Run one miner per subtask, sequentially, in DIFF mode."""
    os.makedirs(miner_workdir, exist_ok=True)
    subtasks = decomp["subtasks"]
    target_stubs = decomp.get("target_stubs", {}) or {}
    new_test_files_content = decomp.get("new_test_files", {}) or {}
    shared_additions_content = decomp.get("shared_additions", {}) or {}

    # allowed_files = modify_files + new_test_files
    for st in subtasks:
        modify = st.get("modify_files", []) or []
        new_tests = st.get("new_test_files", []) or []
        st["allowed_files"] = list(dict.fromkeys(modify + new_tests))

    all_subtask_files = {s["subtask_id"]: s["allowed_files"] for s in subtasks}

    results = {}
    miner_timeout = int(os.environ.get("MINER_TIMEOUT_SECONDS", "1200"))

    for st in _topological_order(subtasks):
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


def _apply_patches_in_order(decomp, miner_results, merge_repo):
    """Apply each miner's patch to merge_repo in dependency order."""
    subtasks = _topological_order(decomp["subtasks"])
    applied = {}
    for st in subtasks:
        sid = st["subtask_id"]
        result = miner_results.get(sid)
        if not result or not getattr(result, "patch", ""):
            print(f"  [merge] {sid}: empty patch, skipping")
            applied[sid] = False
            continue
        # Write patch to a temp file then `git apply`
        patch_file = os.path.join(merge_repo, ".bitswarm_patch.diff")
        with open(patch_file, "w") as f:
            f.write(result.patch)
        try:
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", patch_file],
                cwd=merge_repo, capture_output=True, text=True,
            )
            if proc.returncode != 0:
                # Try 3-way
                proc = subprocess.run(
                    ["git", "apply", "--3way", "--whitespace=nowarn", patch_file],
                    cwd=merge_repo, capture_output=True, text=True,
                )
            ok = proc.returncode == 0
            applied[sid] = ok
            if ok:
                print(f"  [merge] {sid}: patch applied")
                # Commit so subsequent tier patches don't conflict
                subprocess.run(["git", "add", "-A"], cwd=merge_repo, capture_output=True)
                subprocess.run(
                    ["git", "commit", "-m", f"apply {sid}", "--allow-empty"],
                    cwd=merge_repo, capture_output=True, env=GIT_ENV,
                )
            else:
                print(f"  [merge] {sid}: patch FAILED to apply\n{proc.stderr[-400:]}")
        finally:
            try:
                os.remove(patch_file)
            except OSError:
                pass
    return applied


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

    # Pre-discover existing tests (everything in tests/ that's NOT a new test file)
    new_test_paths = []
    for st in decomp["subtasks"]:
        for p in st.get("new_test_files", []) or []:
            if p not in new_test_paths:
                new_test_paths.append(p)
    existing_tests = _discover_existing_tests(repo_path, exclude=new_test_paths)
    print(f"\n[pipeline-diff] existing test files (regression gate): {len(existing_tests)}")
    for t in existing_tests[:10]:
        print(f"           - {t}")
    if len(existing_tests) > 10:
        print(f"           ... and {len(existing_tests) - 10} more")
    print(f"[pipeline-diff] new test files (additive gate): {len(new_test_paths)}")
    for t in new_test_paths:
        print(f"           - {t}")

    # Pre-mining regression baseline: confirm existing tests pass on the
    # unmodified-plus-new-tests scaffolded repo. (If they don't, the
    # delta after mining can't be attributed cleanly.)
    print("\n[pipeline-diff] === pre-mining regression baseline ===")
    pre_passed, pre_output = _run_pytest(repo_path, existing_tests, timeout=600)
    print(f"[pipeline-diff] existing tests on unmodified repo: "
          f"{'PASS' if pre_passed else 'FAIL (proceeding anyway)'}")
    if not pre_passed:
        print(pre_output[-1200:])

    print("\n[pipeline-diff] === miners ===")
    miner_results = await _run_miners(
        decomp,
        scaffolded_repo=repo_path,
        miner_workdir=os.path.join(out_dir, "miner_repos"),
        execute_subtask=execute_subtask,
    )

    print("\n[pipeline-diff] === merge + dual-gate scoring ===")
    merge_repo = os.path.join(out_dir, "merge_repo")
    if os.path.exists(merge_repo):
        shutil.rmtree(merge_repo)
    shutil.copytree(scaffolded_snapshot, merge_repo)

    patch_applied = _apply_patches_in_order(decomp, miner_results, merge_repo)

    # Additive gate: per-subtask new tests on the merged state
    print("\n[pipeline-diff] additive gate (per-subtask new tests on merged):")
    additive_results = {}
    for st in decomp["subtasks"]:
        sid = st["subtask_id"]
        new_tests = st.get("new_test_files", []) or []
        passed, output = _run_pytest(merge_repo, new_tests, timeout=300)
        additive_results[sid] = passed
        print(f"  {sid}: {'PASS' if passed else 'FAIL'}")

    # Regression gate: existing tests on the merged state
    print("\n[pipeline-diff] regression gate (existing tests on merged):")
    regression_passed, regression_output = _run_pytest(
        merge_repo, existing_tests, timeout=600,
    )
    print(f"  {'PASS' if regression_passed else 'FAIL'}")
    if not regression_passed:
        print(regression_output[-1500:])

    # Scoring: complexity_weight * additive_pass * regression_multiplier
    # regression_multiplier = 1.0 if all existing tests pass, 0.5 otherwise
    regression_mult = 1.0 if regression_passed else 0.5
    print("\n[pipeline-diff] === SCORES ===")
    total = 0.0
    for st in decomp["subtasks"]:
        sid = st["subtask_id"]
        w = float(st.get("complexity_weight", 0))
        add_ok = additive_results.get(sid, False)
        applied_ok = patch_applied.get(sid, False)
        per_score = w * (1.0 if add_ok else 0.0) * regression_mult
        total += per_score
        print(f"  {sid:25s}  score={per_score:.3f}  "
              f"patch={'OK  ' if applied_ok else 'FAIL'}  "
              f"new_tests={'PASS' if add_ok else 'FAIL'}")
    print(f"  regression_multiplier      {regression_mult:.2f}")
    print(f"  TOTAL                      {total:.3f} / 1.000")

    print(f"\n[pipeline-diff] merged repo: {merge_repo}")
    print(f"[pipeline-diff] decomposition: {os.path.join(out_dir, 'decomposition.json')}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--spec", required=True, help="path to change spec text file")
    parser.add_argument("--target", required=True,
                         help="path to existing target repo (will be copied)")
    parser.add_argument("--out", required=True,
                         help="output directory")
    args = parser.parse_args()
    return asyncio.run(run(args.spec, args.target, args.out))


if __name__ == "__main__":
    sys.exit(main())
