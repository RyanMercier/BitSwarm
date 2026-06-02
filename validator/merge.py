"""
Tiered merge pipeline.

Instead of applying all patches at once and hoping integration tests pass,
we merge in DAG order  -  one tier at a time. After each tier's patches land,
we cross-compile (re-run that tier's tests on the merged repo). If tests fail
because of interface mismatches, a repair miner fixes them before the next
tier's patches are applied.

This means tier 2 code (e.g. scene_loader) lands on a repo with REAL, VERIFIED
tier 1 code (e.g. geometry, materials, camera)  -  not stubs.
"""

import os
import shutil
import subprocess
import tempfile

from validator.test_runner import run_stub_tests, run_integration_tests
from validator.scorer import compute_scores


_REPAIR_DISABLED = os.environ.get("BITSWARM_DISABLE_REPAIR", "").strip().lower() in (
    "1", "true", "yes"
)

# Cross-compile-failure handling mode:
#   "patch"   - default, run the SDK/subprocess repair miner that
#               edits the failing miner's existing code in place.
#   "replace" - drop the failing miner's patch and re-mine the subtask
#               with the merged tree as visible context (peer
#               implementations are real, not stubs). Tends to produce
#               cleaner fixes when the original miner was structurally
#               wrong vs. just subtly mismatched.
#   "off"     - skip recovery entirely (alias for BITSWARM_DISABLE_REPAIR=1).
_REPAIR_MODE = (os.environ.get("BITSWARM_REPAIR_MODE", "")
                or ("off" if _REPAIR_DISABLED else "patch")).strip().lower()


def _select_repair_backend():
    """Pick between the SDK-based repair miners and the Claude Code
    subprocess miners.

    Resolution order:
      1. ``REPAIR_BACKEND=claude_code`` / ``sdk`` (explicit override).
      2. If ``MINER_BACKEND=claude_code`` and ``ANTHROPIC_API_KEY`` is
         unset, default to ``claude_code`` so subscription-only users
         don't crash on the SDK call.
      3. Otherwise default to ``sdk`` (matches POC behaviour).
    """
    explicit = os.environ.get("REPAIR_BACKEND", "").strip().lower()
    if explicit == "claude_code":
        from validator.repair_cc import repair_miner, repair_integration_tests
        return repair_miner, repair_integration_tests
    if explicit in ("sdk", "anthropic"):
        from validator.repair import repair_miner, repair_integration_tests
        return repair_miner, repair_integration_tests
    miner_backend = os.environ.get("MINER_BACKEND", "").strip().lower()
    if miner_backend == "claude_code" and not os.environ.get("ANTHROPIC_API_KEY"):
        from validator.repair_cc import repair_miner, repair_integration_tests
        return repair_miner, repair_integration_tests
    from validator.repair import repair_miner, repair_integration_tests
    return repair_miner, repair_integration_tests


repair_miner, repair_integration_tests = _select_repair_backend()


def compute_tiers(subtasks):
    """
    Group subtasks into dependency tiers for ordered merging.

    Tier 0: subtasks with no dependencies on other subtasks
    Tier 1: subtasks whose deps are all in tier 0
    Tier N: subtasks whose deps are all in tiers 0..N-1
    """
    subtask_ids = {st["subtask_id"] for st in subtasks}
    deps = {}
    for st in subtasks:
        # Only consider deps that are actual subtasks (not shared files)
        st_deps = set(st.get("dependencies", [])) & subtask_ids
        deps[st["subtask_id"]] = st_deps

    tiers = []
    assigned = set()

    while assigned < subtask_ids:
        tier = [
            sid for sid in subtask_ids - assigned
            if deps[sid] <= assigned
        ]

        if not tier:
            # Remaining have unresolvable deps  -  dump them in the last tier
            tier = sorted(subtask_ids - assigned)

        tier.sort()  # deterministic
        tiers.append(tier)
        assigned.update(tier)

    return tiers


def validate_patch_scope(patch_text, allowed_files):
    """Check that a patch only touches allowed files. Returns list of unauthorized files."""
    unauthorized = []
    for line in patch_text.split("\n"):
        if line.startswith("diff --git"):
            parts = line.split(" ")
            if len(parts) >= 4:
                b_path = parts[3]
                if b_path.startswith("b/"):
                    b_path = b_path[2:]
                if b_path not in allowed_files:
                    unauthorized.append(b_path)
    return unauthorized


def _apply_patch(sid, result, subtask, merge_repo, merge_dir):
    """
    Validate and apply a single miner's patch.
    Returns (applied: bool, conflict: bool).
    """
    if result is None:
        print(f"    {sid}: NO RESULT (miner did not return)")
        return False, False
    if not result.patch:
        print(f"    {sid}: EMPTY PATCH (patch len={len(result.patch) if result.patch else 0})")
        return False, False

    # Scope check
    unauthorized = validate_patch_scope(result.patch, subtask["allowed_files"])
    if unauthorized:
        print(f"    {sid}: SCOPE VIOLATION  -  touched {unauthorized}")
        result.merge_conflict = True
        return False, True

    # Write patch to temp file
    patch_file = os.path.join(merge_dir, f"{sid}.patch")
    with open(patch_file, "w") as f:
        f.write(result.patch)

    # Dry-run check
    check = subprocess.run(
        ["git", "apply", "--check", patch_file],
        cwd=merge_repo, capture_output=True, text=True,
    )
    if check.returncode != 0:
        print(f"    {sid}: CONFLICT  -  {check.stderr.strip()}")
        result.merge_conflict = True
        return False, True

    # Apply
    subprocess.run(
        ["git", "apply", patch_file],
        cwd=merge_repo, capture_output=True,
    )
    print(f"    {sid}: patch applied")
    return True, False


async def _try_drop_and_replace(decomposition, subtask, miner_results,
                                  merge_repo):
    """Run the drop-and-replace flow for one failing subtask.

    Returns ``(passed, output)`` if a clean replacement landed in the
    merged repo, or ``None`` if the replacement couldn't be produced /
    applied. ``None`` signals the caller to fall back to patch-style
    repair.
    """
    from validator.drop_replace import (
        apply_replacement_patch,
        drop_and_replace_subtask,
        find_scaffolding_hash,
    )
    sid = subtask["subtask_id"]
    scaffolding_hash = find_scaffolding_hash(merge_repo)
    if scaffolding_hash is None:
        return None
    new_result, status = await drop_and_replace_subtask(
        decomposition=decomposition,
        subtask=subtask,
        merge_repo=merge_repo,
    )
    print(f"    [drop+replace {sid}] {status}")
    if new_result is None or not getattr(new_result, "patch", ""):
        return None
    new_patch = new_result.patch
    miner_results[sid] = new_result  # so scoring sees the replacement
    if not apply_replacement_patch(
        merge_repo, scaffolding_hash,
        subtask.get("allowed_files", []) or [], new_patch,
    ):
        return None
    # Commit so the next tier sees the replaced state, then verify.
    subprocess.run(["git", "add", "-A"], cwd=merge_repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"replace {sid}"],
        cwd=merge_repo, capture_output=True,
    )
    passed, output = run_stub_tests(subtask, merge_repo)
    return passed, output


async def merge_and_test(decomposition, miner_results, base_repo_path):
    """
    Tiered merge pipeline:

    1. Group subtasks into dependency tiers
    2. For each tier:
       a. Apply all patches in this tier
       b. Cross-compile: re-run each miner's tests on the merged repo
       c. Repair: if cross-compile fails, give the miner the merged context to fix
    3. Run integration tests on the fully merged + repaired repo
    4. Compute scores
    """
    subtasks = decomposition["subtasks"]
    integration_files = list(decomposition.get("integration_test_files", {}).keys())

    # Create a fresh copy for merging
    merge_dir = tempfile.mkdtemp(prefix="bitswarm_merge_")
    merge_repo = os.path.join(merge_dir, "repo")
    shutil.copytree(base_repo_path, merge_repo, dirs_exist_ok=True)

    # Remove stale lock file if present (inherited from workspace copy)
    lock_file = os.path.join(merge_repo, ".git", "index.lock")
    if os.path.exists(lock_file):
        os.remove(lock_file)

    # Ensure it's a git repo with a clean commit
    if not os.path.isdir(os.path.join(merge_repo, ".git")):
        subprocess.run(["git", "init"], cwd=merge_repo, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=merge_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "scaffolding"],
            cwd=merge_repo, capture_output=True,
        )

    subtask_map = {st["subtask_id"]: st for st in subtasks}
    tiers = compute_tiers(subtasks)

    patch_applied = {}
    stub_results = {}
    repairs_made = {}

    for tier_idx, tier_sids in enumerate(tiers):
        print(f"  [Tier {tier_idx}] Merging: {tier_sids}")

        # ── Phase 1: Apply patches for this tier ───────────────────────
        for sid in tier_sids:
            result = miner_results.get(sid)
            subtask = subtask_map[sid]
            applied, conflict = _apply_patch(
                sid, result, subtask, merge_repo, merge_dir
            )
            patch_applied[sid] = applied
            if conflict and result:
                result.merge_conflict = True

        # Commit this tier's patches so the working tree is clean
        subprocess.run(["git", "add", "-A"], cwd=merge_repo, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"tier {tier_idx} patches", "--allow-empty"],
            cwd=merge_repo, capture_output=True,
        )

        # ── Phase 2: Cross-compile  -  run each miner's tests on merged repo ─
        for sid in tier_sids:
            if not patch_applied.get(sid):
                stub_results[sid] = False
                continue

            subtask = subtask_map[sid]
            passed, output = run_stub_tests(subtask, merge_repo)

            if passed:
                stub_results[sid] = True
                print(f"    {sid} cross-compile: PASSED")
                continue

            # ── Phase 3: Recover (repair or drop-and-replace) ──────────
            if _REPAIR_MODE == "off":
                print(f"    {sid} cross-compile: FAILED (recovery disabled)")
                stub_results[sid] = False
                repairs_made[sid] = False
                continue

            repair_passed = False
            repair_output = output

            if _REPAIR_MODE == "replace":
                print(f"    {sid} cross-compile: FAILED -- drop-and-replace")
                replaced = await _try_drop_and_replace(
                    decomposition, subtask, miner_results, merge_repo,
                )
                if replaced is not None:
                    repair_passed, repair_output = replaced
                else:
                    print(f"    {sid} drop-and-replace: failed to produce a "
                          f"working patch, falling back to patch-style repair")

            if not repair_passed:
                if _REPAIR_MODE == "patch" or (
                    _REPAIR_MODE == "replace" and replaced is None
                ):
                    print(f"    {sid} cross-compile: FAILED -- repairing")
                    repair_passed, repair_output = await repair_miner(
                        subtask, merge_repo, repair_output
                    )

            stub_results[sid] = repair_passed
            repairs_made[sid] = repair_passed

            if repair_passed:
                # Commit repair so next tier sees clean state
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=merge_repo, capture_output=True,
                )
                subprocess.run(
                    ["git", "commit", "-m", f"repair {sid}"],
                    cwd=merge_repo, capture_output=True,
                )
            else:
                print(f"    {sid} repair: FAILED")

    # ── Phase 4: Integration tests on fully merged + repaired repo ──────
    integration_passed, integration_output, integration_ratio = \
        run_integration_tests(integration_files, merge_repo)

    if integration_passed:
        print(f"  Integration tests: PASSED")
    elif _REPAIR_DISABLED:
        pct = int(integration_ratio * 100)
        print(f"  Integration tests: FAILED ({pct}% passed) (repair disabled)")
        repairs_made["_integration"] = False
    else:
        pct = int(integration_ratio * 100)
        print(f"  Integration tests: FAILED ({pct}% passed)  -  repairing")

        # ── Phase 4b: Repair integration tests ────────────────────────
        integration_passed, integration_output, integration_ratio = \
            await repair_integration_tests(
                integration_files, merge_repo, integration_output
            )

        if integration_passed:
            subprocess.run(["git", "add", "-A"], cwd=merge_repo, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "repair integration tests"],
                cwd=merge_repo, capture_output=True,
            )
            repairs_made["_integration"] = True
        else:
            repairs_made["_integration"] = False
            pct = int(integration_ratio * 100)
            print(f"  Integration tests after repair: FAILED ({pct}% passed)")

    # ── Phase 5: Score ──────────────────────────────────────────────────
    scores = compute_scores(
        subtasks, miner_results, stub_results, integration_passed,
        integration_pass_ratio=integration_ratio,
    )

    return {
        "merge_repo": merge_repo,
        "patch_applied": patch_applied,
        "stub_results": stub_results,
        "integration_passed": integration_passed,
        "integration_output": integration_output,
        "integration_ratio": integration_ratio,
        "scores": scores,
        "repairs_made": repairs_made,
    }
