"""
BitSwarm protocol package: the shared contract between validators and
miners (pydantic schemas, synapses, repo transport).

PROTOCOL_VERSION is bumped whenever the TaskAssignment / MinerResponse
contract changes incompatibly. Miners refuse assignments from a
different version with stop_reason="protocol_mismatch" instead of
failing somewhere deep in the run, so a mixed fleet upgrades without
mystery zero-scores.
"""

PROTOCOL_VERSION = 1
