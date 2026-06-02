"""
Test validator.server.dispatch_to_miners.

Stubs out the actual HTTP send so we exercise:
- Round-robin assignment of subtasks across miner URLs
- Mapping of MinerResponse fields onto RemoteMinerResult
- Per-subtask failure isolation (a miner error doesn't kill the batch)
"""
import asyncio

import pytest

from protocol.schemas import MinerResponse
from validator import server as validator_server


@pytest.fixture
def fake_decomposition():
    return {
        "subtasks": [
            {"subtask_id": "a", "stub_files": ["a.py"],
             "stub_test_files": ["tests/test_a.py"], "complexity_weight": 0.25,
             "description": "do a", "dependencies": []},
            {"subtask_id": "b", "stub_files": ["b.py"],
             "stub_test_files": ["tests/test_b.py"], "complexity_weight": 0.25,
             "description": "do b", "dependencies": []},
            {"subtask_id": "c", "stub_files": ["c.py"],
             "stub_test_files": ["tests/test_c.py"], "complexity_weight": 0.25,
             "description": "do c", "dependencies": []},
        ],
        "shared_files": {"types.py": "X = 1\n"},
        "stub_files": {"a.py": "...", "b.py": "...", "c.py": "..."},
        "stub_test_files": {
            "tests/test_a.py": "...",
            "tests/test_b.py": "...",
            "tests/test_c.py": "...",
        },
    }


def test_dispatch_round_robins_and_maps_responses(monkeypatch, fake_decomposition):
    # Skip the real git-bundle call  -  repo doesn't exist in tests
    monkeypatch.setattr(validator_server, "bundle_repo", lambda path: "bundle-b64")

    calls: list[tuple[str, str]] = []  # (miner_url, subtask_id)

    async def fake_send(client, miner_url, assignment):
        calls.append((miner_url, assignment.subtask_id))
        return MinerResponse(
            task_id=assignment.task_id,
            subtask_id=assignment.subtask_id,
            patch=f"diff --git a/{assignment.subtask_id}.py ...",
            stub_tests_passed=True,
            stub_test_output="PASSED",
            iterations_used=3,
            stop_reason="tests_passed",
            files_modified=[f"{assignment.subtask_id}.py"],
        )

    monkeypatch.setattr(validator_server, "_send_to_miner", fake_send)

    miner_urls = ["http://miner-1:8081", "http://miner-2:8081"]
    results = asyncio.run(validator_server.dispatch_to_miners(
        decomposition=fake_decomposition,
        scaffolded_repo="/nonexistent",
        miner_urls=miner_urls,
        subtask_timeout=10,
        task_id="t1",
    ))

    # Three subtasks, two miners: round-robin gives a->m1, b->m2, c->m1
    by_subtask = {sid: url for url, sid in calls}
    assert by_subtask == {
        "a": "http://miner-1:8081",
        "b": "http://miner-2:8081",
        "c": "http://miner-1:8081",
    }

    assert set(results.keys()) == {"a", "b", "c"}
    for sid, r in results.items():
        assert r.subtask_id == sid
        assert r.patch.startswith(f"diff --git a/{sid}.py")
        assert r.tests_passed is True
        assert r.iterations_used == 3
        assert r.stop_reason == "tests_passed"
        assert r.merge_conflict is False
        assert r.files_modified == [f"{sid}.py"]


def test_dispatch_isolates_miner_errors(monkeypatch, fake_decomposition):
    monkeypatch.setattr(validator_server, "bundle_repo", lambda path: "bundle-b64")

    async def fake_send(client, miner_url, assignment):
        if assignment.subtask_id == "b":
            raise RuntimeError("miner exploded")
        return MinerResponse(
            task_id=assignment.task_id,
            subtask_id=assignment.subtask_id,
            patch=f"diff --git a/{assignment.subtask_id}.py ...",
            stub_tests_passed=True,
        )

    monkeypatch.setattr(validator_server, "_send_to_miner", fake_send)

    results = asyncio.run(validator_server.dispatch_to_miners(
        decomposition=fake_decomposition,
        scaffolded_repo="/nonexistent",
        miner_urls=["http://miner-1:8081"],
        subtask_timeout=10,
    ))

    # Other subtasks succeed despite b failing
    assert results["a"].tests_passed is True
    assert results["a"].patch.startswith("diff --git a/a.py")
    assert results["c"].tests_passed is True

    # Failed subtask gets a zero-result shim with stop_reason=http_error
    assert results["b"].patch == ""
    assert results["b"].tests_passed is False
    assert results["b"].stop_reason == "http_error"
    assert "miner exploded" in results["b"].test_output


def test_dispatch_requires_at_least_one_miner(fake_decomposition):
    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(validator_server.dispatch_to_miners(
            decomposition=fake_decomposition,
            scaffolded_repo="/nonexistent",
            miner_urls=[],
        ))


def test_dispatch_populates_allowed_files(monkeypatch, fake_decomposition):
    """Subtask records should pick up allowed_files = stub_files + stub_test_files."""
    monkeypatch.setattr(validator_server, "bundle_repo", lambda path: "bundle-b64")

    seen_assignments: list = []

    async def fake_send(client, miner_url, assignment):
        seen_assignments.append(assignment)
        return MinerResponse(
            task_id=assignment.task_id,
            subtask_id=assignment.subtask_id,
            patch="x",
            stub_tests_passed=True,
        )

    monkeypatch.setattr(validator_server, "_send_to_miner", fake_send)

    asyncio.run(validator_server.dispatch_to_miners(
        decomposition=fake_decomposition,
        scaffolded_repo="/nonexistent",
        miner_urls=["http://miner-1:8081"],
    ))

    by_sid = {a.subtask_id: a for a in seen_assignments}
    assert by_sid["a"].allowed_files == ["a.py", "tests/test_a.py"]
    assert by_sid["b"].allowed_files == ["b.py", "tests/test_b.py"]
    # Each miner is given the full picture (all_subtask_files) for context
    assert set(by_sid["a"].all_subtask_files.keys()) == {"a", "b", "c"}
