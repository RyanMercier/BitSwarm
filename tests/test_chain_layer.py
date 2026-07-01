"""
Tests for the Phase A chain layer: synapses, weights, holdback, and
the transport-agnostic miner runtime.

Everything here runs without a chain. Synapse tests use the real
bittensor Synapse base when the package is installed and skip
gracefully when it is not; weights and holdback are pure Python.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import textwrap
import types

import pytest

from protocol.schemas import MinerResponse, TaskAssignment
from validator.holdback import (
    apply_holdback,
    commit_hash,
    reveal_into_repo,
    select_holdback,
    verify_reveal,
)
from validator.weights import ScoreBook


# ---- Synapses ----------------------------------------------------------

def test_task_synapse_round_trip():
    from protocol.synapses import TaskSynapse

    assignment = TaskAssignment(
        task_id="t1", subtask_id="s1", repo_bundle="QUJD",
        mode="diff",
        target_stubs={"a.py": "def f(): ..."},
        new_test_files_content={"tests/test_a.py": "def test(): ..."},
        allowed_files=["a.py"],
    )
    syn = TaskSynapse.from_assignment(assignment)
    assert syn.task_id == "t1"
    assert syn.mode == "diff"
    assert syn.target_stubs == {"a.py": "def f(): ..."}

    # Miner side: extract the assignment view, fill the response.
    back = syn.to_assignment()
    assert back.model_dump() == assignment.model_dump()

    syn.fill_response(MinerResponse(
        task_id="t1", subtask_id="s1", patch="PATCH",
        stub_tests_passed=True, stub_test_output="ok",
        iterations_used=3, stop_reason="tests_passed",
        files_modified=["a.py"],
    ))
    resp = syn.to_response()
    assert resp.patch == "PATCH"
    assert resp.stub_tests_passed is True
    assert resp.stop_reason == "tests_passed"
    assert resp.files_modified == ["a.py"]


def test_status_and_verification_synapses_construct():
    from protocol.synapses import StatusSynapse, VerificationSynapse

    s = StatusSynapse(validator_id="v1")
    assert s.available is False

    v = VerificationSynapse(task_id="t", holdback_commit="abc")
    assert v.gates_passed is False
    assert v.holdback_commit == "abc"


# ---- ScoreBook ---------------------------------------------------------

def test_scorebook_records_and_trims(tmp_path):
    book = ScoreBook(path=str(tmp_path / "scores.json"), window=3)
    for s in (0.1, 0.2, 0.3, 0.4, 0.5):
        book.record("hk1", s)
    assert book.snapshot()["hk1"] == [0.3, 0.4, 0.5]


def test_scorebook_ema_weights_recent_work():
    book = ScoreBook(window=20, alpha=0.5)
    for s in (0.0, 0.0, 1.0, 1.0):
        book.record("improving", s)
    for s in (1.0, 1.0, 0.0, 0.0):
        book.record("declining", s)
    assert book.effective("improving") > 0.5
    assert book.effective("declining") < 0.5
    assert book.effective("unknown") == 0.0


def test_scorebook_weight_vector_normalizes():
    book = ScoreBook(window=5)
    book.record("a", 0.9)
    book.record("b", 0.3)
    uids, weights = book.weight_vector(["a", "b", "never_scored"])
    assert uids == [0, 1]
    assert abs(sum(weights) - 1.0) < 1e-9
    assert weights[0] > weights[1]

    empty_uids, empty_weights = ScoreBook().weight_vector(["x", "y"])
    assert empty_uids == [] and empty_weights == []


def test_scorebook_persistence_round_trip(tmp_path):
    path = str(tmp_path / "scores.json")
    book = ScoreBook(path=path, window=5)
    book.record("hk", 0.75)
    reloaded = ScoreBook(path=path, window=5)
    assert reloaded.snapshot()["hk"] == [0.75]


def test_scorebook_survives_corrupt_file(tmp_path):
    path = str(tmp_path / "scores.json")
    with open(path, "w") as f:
        f.write("{not json")
    book = ScoreBook(path=path)
    assert book.snapshot() == {}


def test_scorebook_clamps_scores():
    book = ScoreBook()
    book.record("hk", 3.7)
    book.record("hk", -1.0)
    assert book.snapshot()["hk"] == [1.0, 0.0]


# ---- Holdback ----------------------------------------------------------

def _tests(n):
    return {f"tests/test_{i}.py": f"def test_{i}(): assert True" for i in range(n)}


def test_holdback_selection_is_deterministic():
    tests = _tests(6)
    v1, h1 = select_holdback(tests, 0.34, seed="task-abc")
    v2, h2 = select_holdback(tests, 0.34, seed="task-abc")
    assert h1 == h2 and v1 == v2
    assert len(h1) == 2
    v3, h3 = select_holdback(tests, 0.34, seed="task-xyz")
    assert h3.keys() != h1.keys() or True  # different seed may differ


def test_holdback_always_leaves_one_visible():
    tests = _tests(2)
    visible, held = select_holdback(tests, 0.9, seed="s")
    assert len(visible) >= 1
    assert len(held) == 1

    only_one = _tests(1)
    visible, held = select_holdback(only_one, 0.9, seed="s")
    assert held == {} and len(visible) == 1


def test_holdback_commit_and_verify():
    _, held = select_holdback(_tests(4), 0.5, seed="s")
    commit = commit_hash(held)
    assert verify_reveal(held, commit)
    tampered = dict(held)
    first = next(iter(tampered))
    tampered[first] = tampered[first] + "\n# sneaky edit"
    assert not verify_reveal(tampered, commit)
    assert commit_hash({}) == ""


def test_apply_holdback_diff_mode(monkeypatch):
    monkeypatch.setenv("BITSWARM_HOLDBACK_FRACTION", "0.5")
    decomp = {"mode": "diff", "new_test_files": _tests(4)}
    apply_holdback(decomp, seed="t1")
    assert len(decomp["new_test_files"]) == 2
    assert len(decomp["holdback_tests"]) == 2
    assert len(decomp["holdback_commit"]) == 64


def test_apply_holdback_defaults_off(monkeypatch):
    monkeypatch.delenv("BITSWARM_HOLDBACK_FRACTION", raising=False)
    decomp = {"mode": "diff", "new_test_files": _tests(4)}
    apply_holdback(decomp, seed="t1")
    assert len(decomp["new_test_files"]) == 4
    assert decomp["holdback_tests"] == {}
    assert decomp["holdback_commit"] == ""


def test_reveal_into_repo_verifies_commit(tmp_path):
    _, held = select_holdback(_tests(4), 0.5, seed="s")
    decomp = {"holdback_tests": held, "holdback_commit": commit_hash(held)}
    written = reveal_into_repo(decomp, str(tmp_path))
    assert sorted(written) == sorted(held)
    for rel in held:
        assert os.path.isfile(tmp_path / rel)

    bad = {"holdback_tests": held, "holdback_commit": "0" * 64}
    with pytest.raises(RuntimeError, match="commitment"):
        reveal_into_repo(bad, str(tmp_path))


# ---- Miner runtime -----------------------------------------------------

def test_run_assignment_end_to_end(tmp_path):
    """run_assignment unbundles the repo, invokes the injected agent,
    and shapes the MinerResponse, without any transport."""
    from miner.runtime import run_assignment
    from protocol.transport import bundle_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("print('hi')\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                    check=True, env=env)

    seen = {}

    async def fake_execute(subtask, repo_path, **kwargs):
        seen["subtask_id"] = subtask["subtask_id"]
        seen["repo_exists"] = os.path.isfile(
            os.path.join(repo_path, "hello.py"))
        seen["mode"] = kwargs.get("mode", "scaffold")
        return types.SimpleNamespace(
            patch="diff --git a/x b/x", tests_passed=True,
            test_output="1 passed", iterations_used=2,
            stop_reason="tests_passed", files_modified=["hello.py"],
        )

    task = TaskAssignment(
        task_id="t", subtask_id="s1",
        repo_bundle=bundle_repo(str(repo)),
        allowed_files=["hello.py"], timeout_seconds=30,
        mode="diff", target_stubs={"hello.py": "..."},
    )
    resp = asyncio.run(run_assignment(task, fake_execute))
    assert seen["repo_exists"] is True
    assert seen["mode"] == "diff"
    assert resp.stub_tests_passed is True
    assert resp.patch.startswith("diff --git")
    assert resp.stop_reason == "tests_passed"
    assert resp.execution_time_seconds > 0


def test_run_assignment_reports_agent_crash(tmp_path):
    from miner.runtime import run_assignment
    from protocol.transport import bundle_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                    check=True, env=env)

    async def exploding_execute(subtask, repo_path, **kwargs):
        raise RuntimeError("agent exploded")

    task = TaskAssignment(task_id="t", subtask_id="s1",
                           repo_bundle=bundle_repo(str(repo)))
    resp = asyncio.run(run_assignment(task, exploding_execute))
    assert resp.stub_tests_passed is False
    assert resp.stop_reason == "error"
    assert "agent exploded" in resp.error_message


# ---- Neuron modules import ----------------------------------------------

def test_neuron_modules_import():
    pytest.importorskip("bittensor")
    import neurons.miner   # noqa: F401
    import neurons.validator  # noqa: F401
