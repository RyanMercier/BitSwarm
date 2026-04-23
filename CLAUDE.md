# CLAUDE.md - BitSwarm Subnet

Read BITSWARM_SPEC.md before writing any code. It contains the full system architecture, protocol definitions, prompts, tool definitions, and lessons learned from the prototype.

## What You're Building

BitSwarm is a Bittensor subnet where validators decompose feature specs into scaffolded subtasks and distribute them to miners who implement in parallel, return patches, and get scored based on test results. Validators set on-chain weights; miners earn TAO.

The prototype (see BitSwarm_POC/) proved the core loop works on a single machine. This repo builds the real subnet infrastructure.

## Project Structure

```
bitswarm/
├── CLAUDE.md                     # This file
├── BITSWARM_SPEC.md              # Full architecture spec
├── config.py                     # API keys, model selection, timeouts
├── validator/
│   ├── __init__.py
│   ├── server.py                 # Validator HTTP/Axon server
│   ├── decomposer.py             # Coordinator decomposition (ported from POC)
│   ├── scaffolder.py             # Writes scaffolding to repo (ported from POC)
│   ├── validator_checks.py       # Decomposition validation (ported from POC)
│   ├── merge.py                  # Tiered DAG merge pipeline (ported from POC)
│   ├── repair.py                 # Cross-compile + integration repair (ported from POC)
│   ├── scorer.py                 # Per-miner scoring (ported from POC)
│   └── prompts.py                # Coordinator system prompt
├── miner/
│   ├── __init__.py
│   ├── server.py                 # Miner HTTP/Axon server
│   ├── agent.py                  # Miner agent loop (ported from POC)
│   ├── tools.py                  # Tool definitions + validators (ported from POC)
│   ├── recovery.py               # Error recovery + thrashing detection (ported from POC)
│   ├── warm_start.py             # Annotated file tree + pre-loaded context (ported from POC)
│   └── prompts.py                # Miner system prompt
├── protocol/
│   ├── __init__.py
│   ├── schemas.py                # Pydantic models: TaskAssignment, MinerResponse, etc.
│   └── transport.py              # Repo bundling (git bundle) + HTTP helpers
├── docker/
│   ├── Dockerfile.validator
│   ├── Dockerfile.miner
│   └── docker-compose.yml
├── tests/
│   ├── test_protocol.py
│   ├── test_decomposer.py
│   └── test_merge.py
└── requirements.txt
```

## Build Order

### Phase 1: Protocol

Define the shared contract between validator and miner. Build and test this first.

1. `protocol/schemas.py` - Pydantic models for TaskAssignment, MinerResponse, StatusCheck, ScoreReport
2. `protocol/transport.py` - git bundle create/unbundle, HTTP helpers

### Phase 2: Port Core Logic

The POC code works. Copy it, rename imports (coordinator -> validator, merger -> validator). Do not redesign.

3. `config.py`
4. `validator/prompts.py`, `validator/decomposer.py`, `validator/validator_checks.py`, `validator/scaffolder.py`
5. `miner/prompts.py`, `miner/tools.py`, `miner/recovery.py`, `miner/warm_start.py`, `miner/agent.py`
6. `validator/merge.py`, `validator/repair.py`, `validator/scorer.py`

### Phase 3: Networking

Wire up HTTP servers so validator and miner communicate over the network instead of asyncio.gather on local copies.

7. `miner/server.py` - FastAPI: POST /task (run agent, return patch), GET /status
8. `validator/server.py` - Orchestration: accept spec + repo, decompose, distribute to miners via HTTP, merge, score

### Phase 4: Docker Sandboxing

9. `docker/Dockerfile.miner` - Python + git, no network during code execution
10. `docker/Dockerfile.validator` - Python + git, network for API calls
11. `docker/docker-compose.yml` - Local dev: 1 validator + N miners

### Phase 5: Bittensor Integration

12. Replace HTTP with Axon/Dendrite/Synapse
13. Weight setting from scorer output
14. Metagraph queries for miner discovery
15. StatusSynapse heartbeat

## Key Decisions

- **Validator = coordinator + merger + scorer.** Trusted party that orchestrates everything.
- **Miner = agent runtime.** Receives subtask, runs agent, returns patch. Model/provider is miner's choice.
- **Protocol-first.** schemas.py defines the contract. Build servers around it.
- **Port, don't rewrite.** POC code is battle-tested. Copy and adjust imports.

## Lessons from the Prototype

See BITSWARM_SPEC.md Sections 15-18. Critical ones:

- `contextvars.ContextVar` for miner tool state, not module globals (async race condition)
- Remove stale `.git/index.lock` before every git operation
- Diff against scaffolding commit hash, not staged state
- Two-phase coordinator (plan then files) to prevent truncation
- Foundational types go in shared_files as complete implementations
- Tiered merge + repair makes outcomes reliable regardless of decomposition quality
- All prompts must be fully generic (zero task-specific content)

## Do Not

- Do not modify coordinator or miner prompts without reading BITSWARM_SPEC.md rationale
- Do not skip coordinator validation
- Do not allow miners to run unsandboxed in production
- Do not hardcode task-specific content in prompts
- Do not build a web UI yet
