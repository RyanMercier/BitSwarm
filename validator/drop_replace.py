"""
Drop-and-replace at merge time.

When a miner's tier-N+1 cross-compile fails in the merged tree, the
default repair miner patches the *symptoms* (it sees the failing
output and tries to fix the miner's existing code to match). That
works for small surface drift but loses badly when the original
miner's code is structurally wrong for the merged context --- e.g.
the miner picked a `Words` wrapper class but the rest of the project
landed on `std::vector<std::string>`. A repair patch bandages over
the disagreement; the cleaner fix is to *throw out the failing
patch and re-mine the subtask with the merged tree as visible
context.*

That's what this module does. Workflow inside ``merge_and_test``'s
cross-compile-failure handler:

  1. Find the ``BitSwarm scaffolding`` commit in the merged repo.
  2. Copy the merged repo (with all tier-N patches applied) to a
     fresh workspace.
  3. ``git checkout <scaffold_hash> -- <miner_allowed_files>`` to
     revert ONLY this miner's files to their stub state. Peer
     subtasks stay at their real merged implementations.
  4. Run ``execute_subtask`` in that workspace. Miner sees the real
     peer code, writes new stubs, runs tests against the real peers,
     iterates until tests pass.
  5. Return the new ``MinerResult``. Its patch is a diff against the
     scaffolding commit covering only ``allowed_files``, just like a
     first-pass miner result.

The caller (``merge.py``) then applies the new patch in place of the
old one and re-runs cross-compile to confirm.

Picks the agent backend by lazy-importing ``miner.server`` (which
already resolves ``MINER_BACKEND=sdk|claude_code``). No extra env var
to remember.

Enable in ``merge.py`` via ``BITSWARM_REPAIR_MODE=replace`` (current
default is ``patch`` = use the SDK-style repair miner).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from typing import Any


def find_scaffolding_hash(repo_path: str) -> str | None:
    """Locate the commit tagged ``BitSwarm scaffolding`` in the repo's git log.

    The scaffolder always commits the freshly-scaffolded files with
    that exact message, so locating it is reliable as long as the
    merge repo preserves git history (it does --- the merge pipeline
    git-inits the working copy and commits each tier on top).
    """
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--format=%H %s"],
            cwd=repo_path,
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    for line in (result.stdout or "").splitlines():
        if "BitSwarm scaffolding" in line:
            return line.split()[0]
    return None


def _revert_files_to_scaffolding(repo_path: str, scaffolding_hash: str,
                                   files: list[str]) -> None:
    """``git checkout <hash> -- <files>`` for every file in the miner's
    allowed_files list. Best-effort: missing-from-scaffolding files
    are skipped (rare; usually means a miner added a file the spec
    didn't anticipate)."""
    if not files:
        return
    # Pre-emptively drop a stale index lock from the copied repo.
    lock = os.path.join(repo_path, ".git", "index.lock")
    if os.path.exists(lock):
        try:
            os.remove(lock)
        except OSError:
            pass
    for path in files:
        # Check if the file exists at the scaffolding commit before
        # trying to revert it. ``git cat-file`` returns non-zero if
        # the path doesn't exist there.
        existed = subprocess.run(
            ["git", "cat-file", "-e", f"{scaffolding_hash}:{path}"],
            cwd=repo_path, capture_output=True,
        )
        if existed.returncode != 0:
            # Not in scaffolding; just delete the file so the miner
            # can re-create it cleanly.
            full = os.path.join(repo_path, path)
            if os.path.isfile(full):
                try:
                    os.unlink(full)
                except OSError:
                    pass
            continue
        subprocess.run(
            ["git", "checkout", scaffolding_hash, "--", path],
            cwd=repo_path, capture_output=True,
        )


def _execute_subtask_callable():
    """Lazy-import the configured execute_subtask.

    ``miner.server._select_backend()`` runs at module load and emits
    a backend banner line. Deferring the import to call time keeps
    drop-and-replace cheap when no one's using it and also makes the
    function easy to monkeypatch in tests.
    """
    from miner.server import execute_subtask
    return execute_subtask


async def drop_and_replace_subtask(
    decomposition: dict[str, Any],
    subtask: dict[str, Any],
    merge_repo: str,
    miner_workdir: str | None = None,
    execute_subtask=None,
) -> tuple[Any | None, str]:
    """Re-mine ``subtask`` with the current merged tree as context.

    Returns ``(MinerResult | None, status_message)``.

      - ``MinerResult`` is the same dataclass the first-pass miner
        emits (or compatible shape).
      - ``status_message`` is a human-readable summary for the merge
        log: 'success', 'no scaffolding commit', 'execute_subtask
        unavailable', etc.

    ``execute_subtask`` is overridable for testing; defaults to the
    backend selected by ``miner.server``.
    """
    sid = subtask["subtask_id"]
    allowed_files = subtask.get("allowed_files", []) or []

    scaffolding_hash = find_scaffolding_hash(merge_repo)
    if scaffolding_hash is None:
        return None, "no BitSwarm scaffolding commit in merged repo"

    if execute_subtask is None:
        try:
            execute_subtask = _execute_subtask_callable()
        except Exception as exc:
            return None, f"execute_subtask unavailable: {exc}"

    cleanup = miner_workdir is None
    if miner_workdir is None:
        miner_workdir = tempfile.mkdtemp(prefix=f"replace_{sid}_")
    workspace = os.path.join(miner_workdir, "repo")

    try:
        if os.path.exists(workspace):
            shutil.rmtree(workspace)
        shutil.copytree(merge_repo, workspace)
        _revert_files_to_scaffolding(workspace, scaffolding_hash, allowed_files)

        # Build the same arg shape the pipeline uses for first-pass mining.
        all_subtask_files = {
            st["subtask_id"]: st.get("allowed_files", []) or []
            for st in (decomposition.get("subtasks") or [])
        }
        shared_files = decomposition.get("shared_files", {}) or {}
        stub_files = decomposition.get("stub_files", {}) or {}
        test_files = decomposition.get("stub_test_files", {}) or {}
        all_subtasks = decomposition.get("subtasks", []) or []

        print(f"    [drop+replace {sid}] re-mining in merged context "
              f"({workspace})")
        result = await execute_subtask(
            subtask=subtask,
            repo_path=workspace,
            all_subtask_files=all_subtask_files,
            shared_files=shared_files,
            shared_files_content=shared_files,
            stub_files_content=stub_files,
            test_files_content=test_files,
            all_subtasks=all_subtasks,
        )
        status = "passed" if getattr(result, "tests_passed", False) else "failed"
        return result, f"re-mined; tests {status}"
    finally:
        if cleanup:
            shutil.rmtree(miner_workdir, ignore_errors=True)


def apply_replacement_patch(merge_repo: str, scaffolding_hash: str,
                              allowed_files: list[str], new_patch: str) -> bool:
    """Apply a re-mined patch to ``merge_repo``, replacing the failing
    miner's previous contribution.

    Steps: revert the miner's files to scaffolding state in the
    merged repo, then ``git apply`` the new patch. Returns True if
    apply succeeded.
    """
    if not new_patch:
        return False
    _revert_files_to_scaffolding(merge_repo, scaffolding_hash, allowed_files)

    # Drop any leftover index lock from the revert step.
    lock = os.path.join(merge_repo, ".git", "index.lock")
    if os.path.exists(lock):
        try:
            os.remove(lock)
        except OSError:
            pass

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False,
    ) as f:
        f.write(new_patch)
        patch_path = f.name
    try:
        check = subprocess.run(
            ["git", "apply", "--check", patch_path],
            cwd=merge_repo, capture_output=True, text=True,
        )
        if check.returncode != 0:
            return False
        applied = subprocess.run(
            ["git", "apply", patch_path],
            cwd=merge_repo, capture_output=True, text=True,
        )
        return applied.returncode == 0
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass
