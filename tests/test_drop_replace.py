"""
Tests for the drop-and-replace recovery path.

Doesn't run a real Claude subprocess: ``execute_subtask`` is
monkeypatched to a stub that produces a deterministic MinerResult.
What we DO exercise:
  - find_scaffolding_hash against a real git repo
  - _revert_files_to_scaffolding reverts only the named files
  - drop_and_replace_subtask passes the right args to execute_subtask
  - apply_replacement_patch reverts + applies cleanly
  - merge.py's BITSWARM_REPAIR_MODE wiring picks the right recovery path
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass

import pytest

from validator import drop_replace


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME":     "BitSwarm-Test",
    "GIT_AUTHOR_EMAIL":    "test@local",
    "GIT_COMMITTER_NAME":  "BitSwarm-Test",
    "GIT_COMMITTER_EMAIL": "test@local",
}


def _init_scaffolded_repo(path: str) -> str:
    """Build a tiny git repo with a 'BitSwarm scaffolding' commit.

    Layout:
      pkg/widget.py     ->   def widget(): raise NotImplementedError
      pkg/peer.py       ->   def peer(): raise NotImplementedError
      tests/test_widget.py -> exercises widget

    Returns the scaffolding commit hash.
    """
    os.makedirs(os.path.join(path, "pkg"), exist_ok=True)
    os.makedirs(os.path.join(path, "tests"), exist_ok=True)
    with open(os.path.join(path, "pkg", "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(path, "pkg", "widget.py"), "w") as f:
        f.write("def widget():\n    raise NotImplementedError\n")
    with open(os.path.join(path, "pkg", "peer.py"), "w") as f:
        f.write("def peer():\n    raise NotImplementedError\n")
    with open(os.path.join(path, "tests", "__init__.py"), "w") as f:
        f.write("")
    with open(os.path.join(path, "tests", "test_widget.py"), "w") as f:
        f.write(
            "from pkg.widget import widget\n"
            "def test_widget():\n    assert widget() == 'ok'\n"
        )
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path,
                    capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "BitSwarm scaffolding"],
        cwd=path, env=GIT_ENV, check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


# ---- find_scaffolding_hash ----

def test_find_scaffolding_hash_when_present(tmp_path):
    sha = _init_scaffolded_repo(str(tmp_path))
    found = drop_replace.find_scaffolding_hash(str(tmp_path))
    assert found == sha


def test_find_scaffolding_hash_missing(tmp_path):
    # Repo with no scaffolding commit -> None.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"],
                    cwd=tmp_path, env=GIT_ENV, capture_output=True)
    assert drop_replace.find_scaffolding_hash(str(tmp_path)) is None


def test_find_scaffolding_hash_not_a_repo(tmp_path):
    assert drop_replace.find_scaffolding_hash(str(tmp_path)) is None


# ---- _revert_files_to_scaffolding ----

def test_revert_reverts_only_listed_files(tmp_path):
    sha = _init_scaffolded_repo(str(tmp_path))
    # Simulate a tier merge: widget got an implementation, peer also got one.
    with open(tmp_path / "pkg" / "widget.py", "w") as f:
        f.write("def widget():\n    return 'something'\n")
    with open(tmp_path / "pkg" / "peer.py", "w") as f:
        f.write("def peer():\n    return 'peer_impl'\n")

    # Revert only widget.
    drop_replace._revert_files_to_scaffolding(
        str(tmp_path), sha, ["pkg/widget.py"],
    )

    # widget is back to stub; peer is untouched (still its impl).
    assert "raise NotImplementedError" in (tmp_path / "pkg" / "widget.py").read_text()
    assert "return 'peer_impl'" in (tmp_path / "pkg" / "peer.py").read_text()


def test_revert_handles_missing_scaffolding_file(tmp_path):
    sha = _init_scaffolded_repo(str(tmp_path))
    # File not in scaffolding -> delete-on-revert.
    with open(tmp_path / "pkg" / "new.py", "w") as f:
        f.write("def new(): pass\n")
    drop_replace._revert_files_to_scaffolding(str(tmp_path), sha, ["pkg/new.py"])
    assert not os.path.exists(tmp_path / "pkg" / "new.py")


# ---- drop_and_replace_subtask ----

@dataclass
class _StubMinerResult:
    subtask_id: str
    patch: str
    tests_passed: bool
    test_output: str = ""
    iterations_used: int = 1
    stop_reason: str = "tests_passed"
    files_modified: list = None
    merge_conflict: bool = False


def _make_stub_execute(record: dict, patch: str = "DUMMY PATCH",
                        passed: bool = True):
    """Returns an awaitable stub that records its call args + returns
    a deterministic MinerResult."""
    async def stub(subtask, repo_path, all_subtask_files, shared_files,
                    shared_files_content, stub_files_content,
                    test_files_content, all_subtasks=None,
                    timeout_seconds=600):
        record["called"] = True
        record["subtask"] = subtask
        record["repo_path"] = repo_path
        record["all_subtask_files"] = all_subtask_files
        record["shared_files"] = shared_files
        record["all_subtasks"] = all_subtasks
        record["widget_state_at_call"] = (
            open(os.path.join(repo_path, "pkg", "widget.py")).read()
            if os.path.exists(os.path.join(repo_path, "pkg", "widget.py"))
            else None
        )
        record["peer_state_at_call"] = (
            open(os.path.join(repo_path, "pkg", "peer.py")).read()
            if os.path.exists(os.path.join(repo_path, "pkg", "peer.py"))
            else None
        )
        return _StubMinerResult(
            subtask_id=subtask["subtask_id"],
            patch=patch,
            tests_passed=passed,
            files_modified=subtask.get("allowed_files", []),
        )
    return stub


def test_drop_and_replace_passes_merged_peer_to_miner(tmp_path):
    """The miner workspace should have peer.py at its merged-state
    impl (not the scaffolding stub), and widget.py reverted to its
    scaffolding stub."""
    sha = _init_scaffolded_repo(str(tmp_path))
    # Simulate the merged tier state: peer is real, widget is broken.
    (tmp_path / "pkg" / "peer.py").write_text(
        "def peer():\n    return 'real_peer'\n"
    )
    (tmp_path / "pkg" / "widget.py").write_text(
        "def widget():\n    return 'broken_widget'\n"  # broken impl
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "tier 0 patches"],
                    cwd=tmp_path, env=GIT_ENV, capture_output=True)

    record: dict = {}
    decomp = {
        "subtasks": [
            {"subtask_id": "widget", "allowed_files": ["pkg/widget.py"]},
            {"subtask_id": "peer", "allowed_files": ["pkg/peer.py"]},
        ],
        "shared_files": {},
        "stub_files": {},
        "stub_test_files": {},
    }
    subtask = decomp["subtasks"][0]

    result, status = asyncio.run(drop_replace.drop_and_replace_subtask(
        decomposition=decomp,
        subtask=subtask,
        merge_repo=str(tmp_path),
        execute_subtask=_make_stub_execute(record),
    ))

    assert record.get("called"), "execute_subtask was never invoked"
    # Widget should be at scaffolding state in the miner's workspace.
    assert "raise NotImplementedError" in record["widget_state_at_call"]
    # Peer should be at its merged-real-impl state.
    assert "real_peer" in record["peer_state_at_call"]
    assert result is not None
    assert "passed" in status


def test_drop_and_replace_returns_none_without_scaffolding(tmp_path):
    """No scaffolding commit -> return None instead of crashing."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "x.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"],
                    cwd=tmp_path, env=GIT_ENV, capture_output=True)

    decomp = {"subtasks": [{"subtask_id": "x", "allowed_files": []}],
               "shared_files": {}, "stub_files": {}, "stub_test_files": {}}
    result, status = asyncio.run(drop_replace.drop_and_replace_subtask(
        decomposition=decomp,
        subtask=decomp["subtasks"][0],
        merge_repo=str(tmp_path),
        execute_subtask=_make_stub_execute({}),
    ))
    assert result is None
    assert "scaffolding" in status.lower()


# ---- apply_replacement_patch ----

def test_apply_replacement_replaces_in_place(tmp_path):
    sha = _init_scaffolded_repo(str(tmp_path))
    # Simulate prior broken miner output landing in merge_repo.
    (tmp_path / "pkg" / "widget.py").write_text(
        "def widget():\n    return 'OLD_BROKEN'\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "tier 0"],
                    cwd=tmp_path, env=GIT_ENV, capture_output=True)

    # The new patch: switch widget to a working impl.
    new_patch = subprocess.run(
        ["git", "diff", sha, "--", "pkg/widget.py"],
        cwd=tmp_path, capture_output=True, text=True,
    ).stdout
    # Modify the patch to represent the "good" version.
    new_patch = new_patch.replace("OLD_BROKEN", "GOOD_IMPL")

    ok = drop_replace.apply_replacement_patch(
        str(tmp_path), sha, ["pkg/widget.py"], new_patch,
    )
    assert ok
    assert "GOOD_IMPL" in (tmp_path / "pkg" / "widget.py").read_text()
    assert "OLD_BROKEN" not in (tmp_path / "pkg" / "widget.py").read_text()


def test_apply_replacement_empty_patch_returns_false(tmp_path):
    sha = _init_scaffolded_repo(str(tmp_path))
    ok = drop_replace.apply_replacement_patch(
        str(tmp_path), sha, ["pkg/widget.py"], "",
    )
    assert ok is False


# ---- merge.py BITSWARM_REPAIR_MODE wiring ----

def test_merge_picks_patch_mode_by_default(monkeypatch):
    monkeypatch.delenv("BITSWARM_REPAIR_MODE", raising=False)
    monkeypatch.delenv("BITSWARM_DISABLE_REPAIR", raising=False)
    import importlib
    from validator import merge
    importlib.reload(merge)
    assert merge._REPAIR_MODE == "patch"


def test_merge_picks_replace_mode_when_set(monkeypatch):
    monkeypatch.setenv("BITSWARM_REPAIR_MODE", "replace")
    import importlib
    from validator import merge
    importlib.reload(merge)
    assert merge._REPAIR_MODE == "replace"


def test_merge_picks_off_when_disable_repair_set(monkeypatch):
    monkeypatch.delenv("BITSWARM_REPAIR_MODE", raising=False)
    monkeypatch.setenv("BITSWARM_DISABLE_REPAIR", "1")
    import importlib
    from validator import merge
    importlib.reload(merge)
    assert merge._REPAIR_MODE == "off"
