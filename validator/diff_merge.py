"""
Diff-mode merge and dual-gate scoring.

The production home of the machinery first proven in
``demo/run_pipeline_diff.py`` (which now imports from here). Given a
diff-mode decomposition and per-subtask miner results, this module:

  1. Applies miner patches to a fresh copy of the scaffolded repo in
     dependency order (absolute patch paths, 3-way fallback).
  2. Restores the validator-canonical new test files from the diff
     baseline commit (belt and suspenders on top of patch scoping:
     miners cannot ship edits to the tests they are scored against).
  3. Runs the ADDITIVE gate per subtask: the coordinator's new tests
     must pass on the merged state, in a hermetic environment.
  4. Optionally runs a repair pass for subtasks whose additive gate
     failed (claude-code repair scoped to that subtask's
     modify_files), then re-runs the gate. Controlled by
     BITSWARM_REPAIR_MODE; "off" skips.
  5. Runs the REGRESSION gate: collects failing test node ids before
     and after, and counts only NEWLY failing tests against the
     score. Pre-existing failures pass through without penalty.
  6. Scores: complexity_weight x additive_pass x regression_multiplier,
     with the honesty override (a subtask whose patch is empty cannot
     earn additive credit no matter what the gate says, because an
     empty patch ships nothing).

Environment hygiene matters more than it looks. Every gate runs with
PYTHONNOUSERSITE=1 and ABSOLUTE PYTHONPATH entries (repo/src first,
then repo). Both constraints exist because their absence produced
live false positives during bring-up: user-site editable installs
hijacked imports, and relative PYTHONPATH entries resolved against
the subprocess cwd and silently pointed nowhere, letting imports fall
through to system site-packages.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}

_REPAIR_MODE = (os.environ.get("BITSWARM_REPAIR_MODE", "")
                or ("off" if os.environ.get("BITSWARM_DISABLE_REPAIR", "")
                    .strip().lower() in ("1", "true", "yes") else "patch")
                ).strip().lower()


def isolated_test_env(repo_path: str) -> dict:
    """Subprocess env for hermetic test runs.

    ABSOLUTE paths only: the test subprocess runs with cwd=repo_path
    and Python resolves relative sys.path entries against the
    subprocess cwd, so a relative entry silently points at a
    nonexistent directory and imports fall through to system
    site-packages. PYTHONNOUSERSITE=1 closes the user-site editable
    install channel.
    """
    env = {**os.environ}
    repo_abs = os.path.abspath(repo_path)
    src_dir = os.path.join(repo_abs, "src")
    paths = [p for p in (src_dir, repo_abs) if os.path.isdir(p)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(paths + ([existing] if existing else []))
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_pytest_files(repo_path: str, test_files: list,
                      timeout: int = 300,
                      stop_on_first: bool = True) -> tuple[bool, str]:
    """Run pytest on a list of test files in the hermetic env."""
    if not test_files:
        return True, "(no tests to run)"
    env = isolated_test_env(repo_path)
    args = [sys.executable, "-m", "pytest", *test_files, "--tb=short", "-q"]
    if stop_on_first:
        args.append("-x")
    try:
        result = subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        output = (result.stdout or "") + (
            ("\n[stderr]\n" + result.stderr) if result.stderr else ""
        )
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "[TIMEOUT]"
    except Exception as exc:
        return False, f"[ERROR: {exc}]"


def collect_failing_nodeids(repo_path: str, test_files: list,
                              timeout: int = 600) -> tuple[set, str]:
    """Run pytest and parse the set of FAILED/ERROR node ids.

    Used for before/after comparison in the regression gate, so
    pre-existing failures never penalize the change under test.
    """
    if not test_files:
        return set(), "(no tests to run)"
    env = isolated_test_env(repo_path)
    args = [sys.executable, "-m", "pytest", *test_files,
            "--tb=no", "-q", "--no-header", "-rN"]
    try:
        result = subprocess.run(
            args, cwd=repo_path, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return set(), "[TIMEOUT]"
    except Exception as exc:
        return set(), f"[ERROR: {exc}]"
    output = (result.stdout or "") + (
        ("\n[stderr]\n" + result.stderr) if result.stderr else ""
    )
    failing = set()
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            failing.add(line[len("FAILED "):].split(" - ", 1)[0].strip())
        elif line.startswith("ERROR "):
            failing.add(line[len("ERROR "):].split(" - ", 1)[0].strip())
    return failing, output


def discover_existing_tests(repo_path: str, exclude: list) -> list:
    """Pytest-collectable test files in the repo, minus the
    coordinator's net-new test paths (those are the additive gate)."""
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


def topological_order(subtasks: list) -> list:
    """Subtasks in dependency order (dependencies first), stable."""
    by_id = {s["subtask_id"]: s for s in subtasks}
    visited: set = set()
    order: list = []

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


def find_diff_baseline(repo_path: str) -> str | None:
    """Hash of the 'BitSwarm diff baseline' commit, if present."""
    log = subprocess.run(
        ["git", "log", "--all", "--format=%H %s"],
        capture_output=True, text=True, cwd=repo_path,
    )
    for line in (log.stdout or "").splitlines():
        if "BitSwarm diff baseline" in line:
            return line.split()[0]
    return None


def apply_patches_in_order(decomp: dict, miner_results: dict,
                             merge_repo: str,
                             patch_dir: str | None = None) -> dict:
    """Apply each miner's patch to merge_repo in dependency order.

    Patch files are written at ABSOLUTE paths outside merge_repo
    (git resolves a relative patch argument against its cwd, which is
    merge_repo; a relative path that contains merge_repo doubles up).
    Patches are preserved on disk for post-run inspection either way.
    """
    subtasks = topological_order(decomp["subtasks"])
    applied = {}
    if patch_dir is None:
        patch_dir = tempfile.mkdtemp(prefix="bitswarm_patches_")
    os.makedirs(patch_dir, exist_ok=True)

    for st in subtasks:
        sid = st["subtask_id"]
        result = miner_results.get(sid)
        patch = getattr(result, "patch", "") if result else ""
        if not patch:
            print(f"  [merge] {sid}: empty patch, skipping")
            applied[sid] = False
            continue
        patch_file = os.path.abspath(os.path.join(patch_dir, f"{sid}.diff"))
        with open(patch_file, "w") as f:
            f.write(patch)
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", patch_file],
            cwd=merge_repo, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            proc = subprocess.run(
                ["git", "apply", "--3way", "--whitespace=nowarn", patch_file],
                cwd=merge_repo, capture_output=True, text=True,
            )
        ok = proc.returncode == 0
        applied[sid] = ok
        if ok:
            print(f"  [merge] {sid}: patch applied "
                  f"({len(patch)} chars, saved to {patch_file})")
            subprocess.run(["git", "add", "-A"], cwd=merge_repo,
                            capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", f"apply {sid}", "--allow-empty"],
                cwd=merge_repo, capture_output=True, env=GIT_ENV,
            )
        else:
            tail = (proc.stderr or proc.stdout or "")[-500:]
            print(f"  [merge] {sid}: patch FAILED to apply\n    {tail.strip()}")
            print(f"    (patch preserved at {patch_file} for inspection)")
    return applied


async def _try_repair_subtask(subtask: dict, merge_repo: str,
                                gate_output: str) -> bool:
    """One repair attempt for a subtask whose additive gate failed on
    the merged state. Scoped to the subtask's modify_files. Returns
    True if the gate passes after repair (re-verified hermetically
    here, never trusting the repair stage's own report)."""
    if _REPAIR_MODE == "off":
        return False
    try:
        from validator.repair_cc import repair_miner
    except Exception as exc:
        print(f"  [repair-diff] repair backend unavailable: {exc}")
        return False

    sid = subtask["subtask_id"]
    # Adapt the diff-mode subtask to the shape repair_cc expects.
    adapted = {
        **subtask,
        "allowed_files": subtask.get("modify_files", []) or [],
        "stub_test_files": subtask.get("new_test_files", []) or [],
    }
    try:
        await repair_miner(adapted, merge_repo, gate_output)
    except Exception as exc:
        print(f"  [repair-diff] {sid}: repair attempt errored: {exc}")
        return False

    # Re-verify with OUR gate regardless of what repair claimed.
    passed, _ = run_pytest_files(
        merge_repo, subtask.get("new_test_files", []) or [],
    )
    print(f"  [repair-diff] {sid}: gate after repair: "
          f"{'PASS' if passed else 'FAIL'}")
    return passed


async def merge_and_test_diff(decomposition: dict, miner_results: dict,
                                base_repo_path: str,
                                out_dir: str | None = None,
                                pre_failing: set | None = None) -> dict:
    """Diff-mode merge pipeline + dual-gate scoring.

    ``base_repo_path`` is the scaffolded repo (original code + the
    committed diff baseline with the validator's new test files).
    ``pre_failing`` is the set of already-failing existing-test node
    ids captured before mining; computed here if not supplied.

    Returns a dict carrying both diff-specific keys and the
    scaffold-compatible aliases the validator server reads
    (merge_repo, scores, integration_passed, integration_ratio).
    """
    subtasks = decomposition["subtasks"]

    new_test_paths: list = []
    for st in subtasks:
        for p in st.get("new_test_files", []) or []:
            if p not in new_test_paths:
                new_test_paths.append(p)
    existing_tests = discover_existing_tests(base_repo_path,
                                              exclude=new_test_paths)

    if pre_failing is None:
        pre_failing, _ = collect_failing_nodeids(base_repo_path,
                                                  existing_tests)

    # Fresh merge repo from the scaffolded baseline.
    if out_dir:
        merge_repo = os.path.join(out_dir, "merge_repo")
        patch_dir = os.path.join(out_dir, "patches")
        if os.path.exists(merge_repo):
            shutil.rmtree(merge_repo)
    else:
        tmp = tempfile.mkdtemp(prefix="bitswarm_diff_merge_")
        merge_repo = os.path.join(tmp, "merge_repo")
        patch_dir = os.path.join(tmp, "patches")
    shutil.copytree(base_repo_path, merge_repo)

    patch_applied = apply_patches_in_order(
        decomposition, miner_results, merge_repo, patch_dir=patch_dir,
    )

    # Restore canonical new test files from the diff baseline so the
    # gates always run the coordinator's tests.
    baseline = find_diff_baseline(merge_repo)
    if baseline and new_test_paths:
        subprocess.run(
            ["git", "checkout", baseline, "--"] + new_test_paths,
            capture_output=True, cwd=merge_repo,
        )

    # Additive gate per subtask, with one repair attempt on failure.
    print("  [diff-merge] additive gate (per-subtask new tests on merged):")
    additive_results: dict = {}
    for st in subtasks:
        sid = st["subtask_id"]
        new_tests = st.get("new_test_files", []) or []
        passed, output = run_pytest_files(merge_repo, new_tests)
        if not passed:
            print(f"    {sid}: FAIL")
            for line in output.splitlines()[-15:]:
                print(f"      {line}")
            if patch_applied.get(sid):
                passed = await _try_repair_subtask(st, merge_repo, output)
        additive_results[sid] = passed
        print(f"    {sid}: {'PASS' if passed else 'FAIL'}")

    # Regression gate: only NEWLY failing tests count.
    post_failing, _ = collect_failing_nodeids(merge_repo, existing_tests)
    newly_failing = post_failing - pre_failing
    carried = post_failing & pre_failing
    print(f"  [diff-merge] regression gate: {len(newly_failing)} newly-failing; "
          f"{len(carried)} pre-existing carried over; "
          f"{len(pre_failing - post_failing)} coincidentally fixed")
    if newly_failing:
        for nid in sorted(newly_failing)[:10]:
            print(f"    NEW REGRESSION: {nid}")
    regression_passed = len(newly_failing) == 0
    regression_mult = 1.0 if regression_passed else 0.5

    # Scoring with the empty-patch honesty override.
    scores: dict = {}
    total = 0.0
    for st in subtasks:
        sid = st["subtask_id"]
        w = float(st.get("complexity_weight", 0))
        add_ok = additive_results.get(sid, False)
        result = miner_results.get(sid)
        patch_len = len(getattr(result, "patch", "") or "") if result else 0
        if add_ok and patch_len == 0:
            add_ok = False  # empty patch shipped nothing; gate result is a false positive
        score = w * (1.0 if add_ok else 0.0) * regression_mult
        scores[sid] = score
        total += score

    all_additive = all(additive_results.get(st["subtask_id"], False)
                       for st in subtasks)
    return {
        "merge_repo": merge_repo,
        "scores": scores,
        "total": total,
        "patch_applied": patch_applied,
        "additive_results": additive_results,
        "newly_failing": sorted(newly_failing),
        "pre_failing": sorted(pre_failing),
        "regression_passed": regression_passed,
        # Scaffold-compatible aliases for validator/server.py.
        "stub_results": additive_results,
        "integration_passed": regression_passed and all_additive,
        "integration_ratio": 1.0 if (regression_passed and all_additive)
                              else (0.5 if regression_passed or all_additive
                                    else 0.0),
        "repairs_made": {},
    }
