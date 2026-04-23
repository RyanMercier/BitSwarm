"""Test protocol schema serialization."""
import pytest
from protocol.schemas import TaskAssignment, MinerResponse


def test_task_assignment_roundtrip():
    ta = TaskAssignment(
        task_id="test-1",
        subtask_id="auth",
        subtask_description="Implement auth",
        allowed_files=["auth.py"],
        stub_test_files=["tests/test_auth.py"],
    )
    data = ta.model_dump()
    ta2 = TaskAssignment(**data)
    assert ta2.task_id == "test-1"
    assert ta2.allowed_files == ["auth.py"]


def test_miner_response_roundtrip():
    mr = MinerResponse(
        task_id="test-1",
        subtask_id="auth",
        patch="diff --git a/auth.py ...",
        stub_tests_passed=True,
        iterations_used=2,
    )
    data = mr.model_dump()
    mr2 = MinerResponse(**data)
    assert mr2.stub_tests_passed is True
    assert mr2.iterations_used == 2
