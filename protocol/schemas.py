"""
BitSwarm Protocol Schemas

Pydantic models for all data exchanged between validator and miner.
"""
from pydantic import BaseModel, Field


class TaskAssignment(BaseModel):
    """Validator -> Miner: subtask assignment.

    ``mode`` selects the workload shape. "scaffold" (default) is the
    build-from-spec flow: the miner replaces not-implemented stub
    bodies. "diff" is the modify-existing-code flow: the miner edits
    the files in its subtask's modify_files toward the post-edit
    contracts in ``target_stubs``, and the diff-mode fields below
    carry the context. All new fields default to empty so existing
    scaffold-mode peers interoperate unchanged.
    """
    task_id: str = ""
    subtask_id: str = ""
    repo_bundle: str = ""           # base64 encoded scaffolded repo (git bundle)
    subtask_description: str = ""
    allowed_files: list[str] = Field(default_factory=list)
    stub_test_files: list[str] = Field(default_factory=list)
    timeout_seconds: int = 600
    subtask_manifest: dict = Field(default_factory=dict)
    shared_files: dict[str, str] = Field(default_factory=dict)
    all_subtask_files: dict[str, list[str]] = Field(default_factory=dict)
    stub_files_content: dict[str, str] = Field(default_factory=dict)
    test_files_content: dict[str, str] = Field(default_factory=dict)
    all_subtasks: list[dict] = Field(default_factory=list)
    # Diff-mode fields (ignored in scaffold mode):
    mode: str = "scaffold"
    target_stubs: dict[str, str] = Field(default_factory=dict)
    new_test_files_content: dict[str, str] = Field(default_factory=dict)
    shared_additions_content: dict[str, str] = Field(default_factory=dict)


class MinerResponse(BaseModel):
    """Miner -> Validator: subtask result."""
    task_id: str = ""
    subtask_id: str = ""
    patch: str = ""
    stub_tests_passed: bool = False
    stub_test_output: str = ""
    files_modified: list[str] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    error_message: str = ""
    iterations_used: int = 0
    stop_reason: str = ""


class StatusCheck(BaseModel):
    """Validator -> Miner: availability ping."""
    validator_id: str = ""


class StatusResponse(BaseModel):
    """Miner -> Validator: availability response."""
    available: bool = False
    current_task_id: str = ""
    miner_id: str = ""


class ScoreReport(BaseModel):
    """Per-miner scores from a completed task."""
    task_id: str = ""
    scores: dict[str, float] = Field(default_factory=dict)
    integration_passed: bool = False
    integration_ratio: float = 0.0
    total_score: float = 0.0
