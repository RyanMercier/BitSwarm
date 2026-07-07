"""
Bittensor Synapse definitions for BitSwarm.

Three synapses, mirroring the HTTP protocol in protocol/schemas.py:

  TaskSynapse          validator -> miner   subtask assignment in,
                                            miner response out
  StatusSynapse        validator -> miner   availability ping
  VerificationSynapse  validator -> validator  passive re-verification
                                            of a merged result

A bittensor Synapse is a pydantic model that both sides mutate: the
caller (dendrite) fills the request fields, the responder's axon
forward function fills the response fields, and the same object rides
back. Request and response fields therefore live side by side on each
class, with the response fields defaulted so a fresh request is valid.

The module imports without bittensor installed (the base class falls
back to plain pydantic) so the ordinary test suite and the HTTP-only
deployment never need the chain stack. ``HAS_BITTENSOR`` tells
callers which world they are in; the neuron entry points refuse to
start without it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

try:
    import bittensor as bt
    _Base = bt.Synapse
    HAS_BITTENSOR = True
except Exception:  # pragma: no cover - exercised only without the package
    _Base = BaseModel
    HAS_BITTENSOR = False

from protocol.schemas import MinerResponse, TaskAssignment


class TaskSynapse(_Base):
    """One subtask assignment and its result.

    Request fields are set by the validator before dendrite send;
    response fields are filled by the miner's forward handler.
    ``repo_bundle`` is a base64 git bundle and can be large; axon and
    dendrite body-size limits must be raised accordingly (see
    docs/TESTNET.md).
    """

    # --- request (validator fills) ---
    protocol_version: int = 1
    task_id: str = ""
    subtask_id: str = ""
    repo_bundle: str = ""
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
    mode: str = "scaffold"
    target_stubs: dict[str, str] = Field(default_factory=dict)
    new_test_files_content: dict[str, str] = Field(default_factory=dict)
    shared_additions_content: dict[str, str] = Field(default_factory=dict)

    # --- response (miner fills) ---
    patch: str = ""
    stub_tests_passed: bool = False
    stub_test_output: str = ""
    files_modified: list[str] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    error_message: str = ""
    iterations_used: int = 0
    stop_reason: str = ""

    @classmethod
    def from_assignment(cls, assignment: TaskAssignment) -> "TaskSynapse":
        """Build the request synapse from the HTTP-protocol model."""
        return cls(**assignment.model_dump())

    def to_assignment(self) -> TaskAssignment:
        """Extract the assignment view (miner side)."""
        fields = TaskAssignment.model_fields.keys()
        return TaskAssignment(**{k: getattr(self, k) for k in fields})

    def fill_response(self, response: MinerResponse) -> "TaskSynapse":
        """Copy a MinerResponse's fields onto this synapse (miner side)."""
        for k in ("patch", "stub_tests_passed", "stub_test_output",
                   "files_modified", "execution_time_seconds",
                   "error_message", "iterations_used", "stop_reason"):
            setattr(self, k, getattr(response, k))
        return self

    def to_response(self) -> MinerResponse:
        """Extract the response view (validator side, after the call)."""
        return MinerResponse(
            task_id=self.task_id,
            subtask_id=self.subtask_id,
            patch=self.patch,
            stub_tests_passed=self.stub_tests_passed,
            stub_test_output=self.stub_test_output,
            files_modified=list(self.files_modified or []),
            execution_time_seconds=self.execution_time_seconds,
            error_message=self.error_message,
            iterations_used=self.iterations_used,
            stop_reason=self.stop_reason,
        )


class StatusSynapse(_Base):
    """Lightweight availability ping. Cheap enough to send to the
    whole metagraph before dispatching a task."""

    # request
    validator_id: str = ""
    # response
    available: bool = False
    current_task_id: str = ""
    miner_id: str = ""
    backend: str = ""


class VerificationSynapse(_Base):
    """Validator-to-validator passive verification.

    The active validator publishes the merged repo bundle plus the
    gate spec; a passive validator re-runs the gates in its own
    hermetic environment and reports what it saw. Divergence between
    validators surfaces as weight disagreement, which Yuma Consensus
    discounts.
    """

    # request
    task_id: str = ""
    merge_bundle: str = ""                 # base64 git bundle of the merged repo
    mode: str = "scaffold"
    gate_test_files: list[str] = Field(default_factory=list)
    holdback_tests: dict[str, str] = Field(default_factory=dict)
    holdback_commit: str = ""              # sha256 committed before mining
    # response
    gates_passed: bool = False
    gate_results: dict[str, bool] = Field(default_factory=dict)
    holdback_verified: bool = False
    verifier_id: str = ""
    detail: str = ""
