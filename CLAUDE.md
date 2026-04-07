# CLAUDE.md — BitSwarm Prototype

Read BITSWARM_SPEC.md before writing any code. It contains the full system architecture, all prompts, tool definitions, error recovery protocols, and scoring logic. This file tells you what to build for the prototype and in what order.

## What You're Building

A single-machine prototype of BitSwarm that proves one thing: can a coordinator agent decompose a feature spec into scaffolded subtasks precise enough that multiple independent agents, working in parallel with no shared context, produce code that merges and passes integration tests?

No Bittensor integration. No Docker sandboxing. No networking. Just the core loop: decompose, scaffold, assign, implement, merge, test, score.

## Project Structure

```
bitswarm/
├── CLAUDE.md                     # This file
├── BITSWARM_SPEC.md              # Full architecture spec (READ THIS FIRST)
├── config.py                     # API keys, model selection, timeouts
├── orchestrator.py               # Main entry point: runs the full pipeline
├── coordinator/
│   ├── __init__.py
│   ├── decomposer.py             # Calls Claude API with coordinator prompt, returns JSON
│   ├── scaffolder.py             # Writes decomposition JSON as actual files to repo
│   ├── validator_checks.py       # Programmatic validation of decomposition (ast.parse, imports, weights)
│   └── prompts.py                # Coordinator system prompt (from BITSWARM_SPEC.md Section 3.2)
├── miner/
│   ├── __init__.py
│   ├── agent.py                  # Miner agent: tool-use loop calling Claude API
│   ├── tools.py                  # Tool definitions, validators, executors (from BITSWARM_SPEC.md Section 3.3)
│   ├── recovery.py               # Error recovery protocol, retry state, thrashing detection
│   ├── warm_start.py             # Builds annotated file tree and pre-loaded context block
│   └── prompts.py                # Miner system prompt (from BITSWARM_SPEC.md Section 3.3)
├── merger/
│   ├── __init__.py
│   ├── merge.py                  # Applies patches in dependency order via git
│   ├── test_runner.py            # Runs pytest, captures output
│   └── scorer.py                 # Per-miner scoring: stub pass/fail + integration multiplier
├── target_repo/                  # The Flask app we build a feature against
│   ├── app.py
│   ├── models.py
│   ├── requirements.txt
│   └── tests/
│       └── test_app.py
└── requirements.txt
```

## The Target Repository

Create this minimal Flask app. This is the "existing codebase" the feature gets built against.

```python
# target_repo/app.py
from flask import Flask, jsonify
from models import db

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///app.db'
app.config['SECRET_KEY'] = 'dev-secret-key'
db.init_app(app)

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
```

```python
# target_repo/models.py
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
```

```python
# target_repo/tests/test_app.py
import pytest
from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_health(client):
    rv = client.get('/health')
    assert rv.status_code == 200
```

```
# target_repo/requirements.txt
flask
flask-sqlalchemy
pytest
```

## The Test Task

```
Add Google OAuth login. Users should be able to click a Login with Google button,
authenticate via Google's OAuth2 flow, and be redirected back to the app where a
session is created. If the user's email doesn't exist in the database, create a new
User record. Add a /me endpoint that returns the current logged-in user's info or
401 if not authenticated. Add a /logout endpoint that clears the session.
```

## Build Order

Build and test each component in this exact sequence. Do not skip ahead.

### Step 1: Config

```python
# config.py
import os

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
COORDINATOR_MODEL = "claude-sonnet-4-20250514"
MINER_MODEL = "claude-sonnet-4-20250514"
MAX_MINER_ITERATIONS = 5
MAX_COORDINATOR_RETRIES = 3
SUBTASK_TIMEOUT_SECONDS = 300
```

### Step 2: Coordinator Prompts

Copy the coordinator system prompt EXACTLY from BITSWARM_SPEC.md Section 3.2 "Coordinator System Prompt" into `coordinator/prompts.py`. Do not paraphrase or abbreviate it. The prompt engineering is precise and every instruction matters.

### Step 3: Coordinator Decomposer

`coordinator/decomposer.py` — Calls the Claude API with the coordinator system prompt and a user message containing the repo file tree, key file contents, and the feature spec. Returns parsed JSON matching the decomposition schema.

The user message must include:
- Full file tree of the target repo (output of `find . -type f`)
- Contents of app.py, models.py, tests/test_app.py, requirements.txt
- The feature specification

Parse the response as JSON. The coordinator prompt instructs the model to return a single JSON object. If parsing fails, retry with the parse error appended.

### Step 4: Coordinator Validation

`coordinator/validator_checks.py` — Implements every check from BITSWARM_SPEC.md Section 3.2 "Coordinator Self-Verification Loop":

1. All files in shared_files, stub_files, stub_test_files, integration_test_files parse via `ast.parse()`
2. All imports in stub files resolve to existing repo files, shared files, standard library, or packages in requirements_additions
3. All imports in stub TEST files also resolve (tests import types too)
4. No file path overlaps between subtasks
5. Complexity weights sum to 1.0 (within 0.01 tolerance)
6. No circular dependencies
7. Each subtask has at least one stub test file
8. After writing scaffolding to disk: stub tests actually FAIL when run (confirming stubs raise NotImplementedError)

If any check fails, return the list of specific error strings. The decomposer retries with these errors appended to the prompt. Budget: MAX_COORDINATOR_RETRIES attempts.

### Step 5: Scaffolder

`coordinator/scaffolder.py` — Takes the validated decomposition JSON and writes all files to the repo:
- Creates directories as needed
- Writes shared files (complete implementations)
- Writes stub files (NotImplementedError bodies)
- Writes stub test files
- Writes integration test files
- Updates requirements.txt with requirements_additions
- Runs `git add -A && git commit -m "BitSwarm scaffolding"`

### Step 6: Miner Tools

Copy the tool definitions, validators, and executors EXACTLY from BITSWARM_SPEC.md Section 3.3 "Miner Tool Definitions" into `miner/tools.py`. This includes:
- `file_read` with path traversal check
- `file_write` with scope enforcement, AST validation, and public interface check
- `bash` with blocked command patterns and timeout
- `list_files` (optional 4th tool)
- The `run_tool()` router function
- All validation functions

Set `REPO_ROOT` and `ALLOWED_FILES` at runtime from the subtask assignment.

### Step 7: Miner Error Recovery

Copy the error recovery protocol from BITSWARM_SPEC.md Section 3.3 "Error Recovery Protocol" into `miner/recovery.py`. This includes:
- `RetryState` and `IterationRecord` dataclasses
- `extract_error_signature()` for dedup
- `extract_per_test_errors()` for thrashing detection
- `detect_thrashing()` that triggers hard reset after 3 different errors on same test
- `format_test_feedback()` with error type hints
- `build_retry_context()` with approach-log pattern
- `should_stop()` and `update_state()`

### Step 8: Miner Warm Start

`miner/warm_start.py` — Copy the `build_annotated_file_tree()` function and user message template from BITSWARM_SPEC.md Section 3.3 "Miner Warm-Start Context Block". This function:
- Walks the repo and annotates each file (YOUR ASSIGNMENT, YOUR TESTS, shared, assigned to another engineer)
- Builds a user message with pre-loaded stub file content, test file content, and shared schema content
- Eliminates the miner's first 3-5 file_read calls

### Step 9: Miner Prompts

Copy the miner system prompt EXACTLY from BITSWARM_SPEC.md Section 3.3 "Miner System Prompt" into `miner/prompts.py`.

### Step 10: Miner Agent

`miner/agent.py` — The core agent loop. This is where the miner calls the Claude API with tool use.

```python
async def execute_subtask(subtask, repo_path, all_subtask_files, shared_files):
    """
    Run the miner agent for a single subtask.
    
    1. Build warm-start context (annotated tree + pre-loaded files)
    2. Initialize RetryState
    3. Call Claude API with system prompt + warm-start user message + tools
    4. Process tool calls: validate -> execute -> return result
    5. After each pytest run, update RetryState
    6. If thrashing detected, trigger hard reset (restore original stubs)
    7. When tests pass or iterations exhausted, generate git diff
    8. Return MinerResult
    """
```

Use the Anthropic Python SDK with tool use. The loop:
1. Send messages to Claude with `tools=TOOL_DEFINITIONS`
2. If response has `tool_use` blocks, execute each tool via `run_tool()`
3. Append tool results as `tool_result` messages
4. If a tool call was `bash` with `pytest` in the command, update RetryState
5. Check stopping conditions after each test run
6. If response has no tool_use (just text), check if agent is done or stuck
7. On exit: generate `git diff` of allowed files only, return patch

For hard reset: when `state.hard_reset_triggered` becomes True, restore the original stub file contents (from the scaffolding) by writing the original stub back via file_write, and inject the hard reset message from `build_retry_context()`.

### Step 11: Merger

`merger/merge.py` — Implements the merge pipeline from BITSWARM_SPEC.md Section 3.4:

```python
async def merge_and_test(task, miner_results, base_repo_path):
    """
    1. Create a fresh copy of the scaffolded repo
    2. For each miner result (in dependency order):
       a. Validate patch touches only allowed files
       b. Apply patch with git apply
       c. If conflict, mark miner score = 0
    3. Run each miner's stub tests individually
    4. Run integration tests on merged result
    5. Compute scores using the scoring formula from Section 3.4
    6. Return MergeResult
    """
```

`merger/scorer.py` — Scoring logic from BITSWARM_SPEC.md Section 3.4:
- Stub tests passed + integration passed = full complexity_weight
- Stub tests passed + integration failed = complexity_weight * 0.5
- Stub tests failed = 0.0
- Patch conflict or scope violation = 0.0

### Step 12: Orchestrator

`orchestrator.py` — Ties everything together. This is the main script.

```python
async def run(spec: str, repo_path: str, num_miners: int = 4):
    # 1. Copy target repo to temp working directory
    # 2. git init the working copy
    
    # 3. Run coordinator decomposition with self-verification loop
    #    (retry up to MAX_COORDINATOR_RETRIES with specific errors)
    
    # 4. Write scaffolding to repo, git commit
    
    # 5. Print decomposition summary
    
    # 6. For each subtask, create a separate copy of the scaffolded repo
    
    # 7. Run all miners in parallel (asyncio.gather)
    #    Each miner gets its own repo copy and runs independently
    
    # 8. Print per-miner stub test results
    
    # 9. Run merger on a fresh copy of the scaffolded repo
    
    # 10. Print integration test results and scores
    
    # 11. If integration passed, print SUCCESS
    #     If not, print which components failed and why
```

Run with:
```bash
export ANTHROPIC_API_KEY=sk-...
pip install anthropic flask flask-sqlalchemy pytest gitpython
python orchestrator.py
```

The spec and task are hardcoded for the prototype. No CLI args needed yet.

## What Success Looks Like

1. Coordinator produces valid scaffolding on first or second attempt
2. Stub tests fail on scaffolding (confirming stubs are real)
3. All 4 miners pass their stub tests (ideally within 2-3 iterations each)
4. All patches apply cleanly (zero merge conflicts)
5. Integration tests pass on the merged codebase
6. You can `cd` into the merged repo and actually run the Flask app

## What to Watch For

If Step 3 fails repeatedly: the coordinator prompt needs tuning. Look at the validation errors. Common issues: imports that don't resolve, types referenced in tests that aren't in shared files, stub tests that pass on stubs (meaning they're no-ops).

If Step 3 passes but miners fail: either the stubs are under-specified (docstrings too vague) or the warm-start context is missing files the miner needs. Check what the miner's first few tool calls are. If it's reading files that should have been pre-loaded, add them to warm-start.

If miners pass stubs but integration fails: the coordinator's integration tests aren't testing the right boundaries, or the shared schemas are incomplete (a type is missing a field that one component writes and another reads).

If patches conflict: the coordinator assigned overlapping file paths to different subtasks. This is a coordinator validation bug.

## Do Not

- Do not add Bittensor, Docker, or networking code. This is a single-machine prototype.
- Do not build a web UI or API server. The orchestrator runs once and prints results.
- Do not implement the seam verifier, quality metrics, or partial credit scoring. Those are post-prototype.
- Do not hardcode the decomposition. The coordinator must generate it from the spec.
- Do not skip the coordinator validation step. Broken scaffolding breaks everything downstream.
- Do not modify the miner or coordinator prompts without reading the full rationale in BITSWARM_SPEC.md. Every instruction is there for a reason.
