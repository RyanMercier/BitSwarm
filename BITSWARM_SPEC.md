# BitSwarm: Complete Technical Specification

Combined architecture blueprint and implementation reference for a cooperative coding agent subnet on Bittensor.

This document merges the high-level system architecture with concrete implementation artifacts (prompts, tool definitions, error recovery protocols, context selection algorithms, and architectural pattern analysis) into a single buildable specification.

---

## 1. System Overview

BitSwarm is a Bittensor subnet where miners are autonomous coding agents that collaborate on different parts of a software feature. Unlike Ridges (SN62) where miners compete to produce the best patch for the same problem, BitSwarm miners each build a different component of a larger system. The validator coordinates decomposition, assigns subtasks, merges results, and scores based on whether the integrated build passes.

The digital commodity this subnet produces is **working, multi-component software features** built from natural language specifications.

### Key Design Decision: Scaffolding Commit

The central architectural insight is that the coordinator does not produce natural language descriptions of interfaces. It produces **actual code**: real Python files with real function signatures, real type definitions, real imports, and `raise NotImplementedError` as every function body. This scaffolding is committed to the repository before any miner starts work. Each miner's job is then to replace the `NotImplementedError` in their assigned files with a working implementation.

This eliminates the primary failure mode of multi-agent coordination: ambiguity. When the contract is executable code rather than a description, there is nothing to misinterpret. A function signature of `def get_user(user_id: int) -> User: raise NotImplementedError` leaves no room for one miner to return a dict while another expects a Pydantic model.

### Core Loop

```
Requester submits feature spec + target repo
                |
                v
    Validator: Coordinator Agent
    1. Ingests repo, analyzes codebase structure
    2. Decomposes spec into N subtasks
    3. Generates ACTUAL CODE SCAFFOLDING:
       - Shared files (complete implementations of types, schemas, configs)
       - Stub files (real functions with NotImplementedError bodies)
       - Stub tests (pytest files that fail on stubs, pass on correct impl)
       - Integration tests (pytest files testing cross-component behavior)
    4. Commits scaffolding to the repo
                |
                v
    Validator distributes subtasks to miners via Synapse
    (one subtask per miner, each gets the SCAFFOLDED repo + their assignment)
                |
                v
    Miners: Agent Runtime (sandboxed)
    1. Receives scaffolded repo + subtask assignment
    2. Reads stub files to understand exact function signatures
    3. Reads docstrings and type hints for behavioral specification
    4. Replaces NotImplementedError with working implementations
    5. Runs stub tests locally, iterates until they pass
    6. Returns a git patch scoped to assigned files
                |
                v
    Validator: Merge + Test Pipeline
    1. Validates patches (file scope, clean apply)
    2. Applies all patches in dependency order
    3. Runs stub tests per miner (individual scoring)
    4. Runs integration tests on merged result (system scoring)
    5. Computes per-miner scores
                |
                v
    Validator sets weights on-chain via Yuma Consensus
```

---

## 2. Mapping to Bittensor Primitives

Bittensor subnets communicate through three core primitives.

### Axon (Miner Server)

Each miner runs an Axon server (FastAPI under the hood) that exposes endpoints for receiving subtask assignments and returning code patches. The miner's Axon advertises its IP:PORT on the Bittensor blockchain so validators can discover and query it.

### Dendrite (Validator Client)

Validators use Dendrite clients to send subtask assignments to miners and collect their responses. A single validator may query multiple miners simultaneously for a single task (one subtask per miner).

### Synapse (Data Object)

All data exchange between validators and miners happens through Synapse objects (Pydantic models). BitSwarm defines custom Synapse subclasses for the task assignment and response protocol (see Section 5).

### Weight Setting

At the end of each tempo (360 blocks, ~72 minutes), validators submit weight vectors to the blockchain. Yuma Consensus aggregates weights from all validators to determine emission distribution. BitSwarm validators set weights based on miners' cumulative task performance over a scoring window.

### Subnet Constraints

- Max 256 UIDs per subnet (up to 64 validators, up to 192 miners)
- Tempo: 360 blocks (~72 minutes between weight updates)
- Block time: 12 seconds
- Miners must maintain uptime, lowest-emission miners get deregistered when new miners register

---

## 3. Component Architecture

### 3.1 Task Queue

The task queue is an off-chain data structure maintained by validators. It holds incoming feature requests from requesters.

```
TaskQueue:
  task_id: str (uuid)
  repo_url: str (git clone URL)
  repo_ref: str (branch/commit to build against)
  spec: str (natural language feature specification)
  test_hints: Optional[str] (requester-provided test expectations)
  budget_subtasks: int (max number of subtasks, default 4)
  timeout_seconds: int (per-subtask deadline, default 600)
  status: enum (pending, decomposing, scaffolded, assigned, merging, complete, failed)
  created_at: datetime
  claimed_by: Optional[str] (validator hotkey that claimed this task)
```

For the prototype, the task queue is local to a single validator. In production, validators share task state through a simple off-chain coordination layer (Redis, a shared API, or gossip protocol). Tasks are claimed by validators on a first-come basis. A claimed task that isn't completed within a timeout gets released back to the queue.

### 3.2 Coordinator Agent


The coordinator is the most critical component. It runs on the validator side and produces the scaffolding commit that the entire task builds on.

**Inputs:**
- The full target repository (cloned locally)
- The feature specification (natural language)
- Optional test hints from the requester

**Outputs: A scaffolding commit containing:**

#### Shared Files (complete implementations)
Real Python files committed to the repo containing types, schemas, constants, and configurations used by multiple subtasks. These are NOT stubs. They are complete, working code. Miners import from them but do not modify them.

Examples:
- `auth/schemas.py` containing `class OAuthToken(BaseModel)`, `class OAuthUserInfo(BaseModel)`
- `auth/exceptions.py` containing `class AuthenticationError(Exception)`
- `auth/config.py` containing `GOOGLE_CLIENT_ID`, `GOOGLE_REDIRECT_URI`

#### Stub Files (NotImplementedError bodies)
Real Python files with correct imports, correct function signatures, correct type hints, complete docstrings, and `raise NotImplementedError` as every function body. Each stub file belongs to exactly one subtask. No file path overlap between subtasks.

Example:
```python
# auth/google_client.py - Stub file for subtask "oauth_config"
from auth.schemas import OAuthToken, OAuthUserInfo
from auth.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI


def get_authorization_url(state: str) -> str:
    """Build the Google OAuth2 authorization URL.

    Args:
        state: CSRF protection token to include in the OAuth state parameter.

    Returns:
        Full Google OAuth2 authorization URL with client_id, redirect_uri,
        scope (openid, email, profile), response_type=code, and state.
    """
    raise NotImplementedError


def exchange_code_for_token(authorization_code: str) -> OAuthToken:
    """Exchange an authorization code for an access token.

    Args:
        authorization_code: The code returned by Google after user consent.

    Returns:
        OAuthToken with access_token, token_type, expires_in, and id_token fields.

    Raises:
        AuthenticationError: If the token exchange fails (invalid code,
            network error, or Google returns an error response).
    """
    raise NotImplementedError


def get_user_info(access_token: str) -> OAuthUserInfo:
    """Fetch user profile information from Google's userinfo endpoint.

    Args:
        access_token: Valid OAuth2 access token.

    Returns:
        OAuthUserInfo with email, name, and picture fields.

    Raises:
        AuthenticationError: If the access token is invalid or expired.
    """
    raise NotImplementedError
```

#### Stub Tests (per subtask)
Pytest files that import from the stub files and test the interface contract. These tests FAIL on the scaffolding (because stubs raise NotImplementedError) and PASS when the miner correctly implements the stubs. Each subtask has its own stub test file. Stub tests run independently of other subtasks.

Example:
```python
# tests/test_google_client.py - Stub tests for subtask "oauth_config"
import pytest
from unittest.mock import patch, Mock
from auth.google_client import get_authorization_url, exchange_code_for_token, get_user_info
from auth.schemas import OAuthToken, OAuthUserInfo
from auth.exceptions import AuthenticationError


def test_get_authorization_url_contains_client_id():
    url = get_authorization_url(state="test_state")
    assert "client_id=" in url
    assert "test_state" in url


def test_get_authorization_url_contains_scope():
    url = get_authorization_url(state="abc")
    assert "openid" in url
    assert "email" in url


@patch("auth.google_client.requests.post")
def test_exchange_code_returns_oauth_token(mock_post):
    mock_post.return_value = Mock(
        status_code=200,
        json=lambda: {"access_token": "tok", "token_type": "Bearer",
                       "expires_in": 3600, "id_token": "jwt"}
    )
    token = exchange_code_for_token("valid_code")
    assert isinstance(token, OAuthToken)
    assert token.access_token == "tok"


def test_exchange_code_raises_on_invalid_code():
    with pytest.raises(AuthenticationError):
        exchange_code_for_token("")


@patch("auth.google_client.requests.get")
def test_get_user_info_returns_user(mock_get):
    mock_get.return_value = Mock(
        status_code=200,
        json=lambda: {"email": "a@b.com", "name": "Test", "picture": "http://pic"}
    )
    info = get_user_info("valid_token")
    assert isinstance(info, OAuthUserInfo)
    assert info.email == "a@b.com"
```

#### Integration Tests
Pytest files that test cross-component interactions. These test the boundaries between subtasks: does the OAuth callback route correctly use the google_client module to create a User in the database? These pass only when ALL subtasks are correctly implemented and merged.

#### Subtask Manifest
A JSON file mapping each subtask to its files, tests, and metadata:

```json
{
  "task_id": "abc-123",
  "subtasks": [
    {
      "subtask_id": "oauth_config",
      "description": "Implement Google OAuth client: authorization URL, token exchange, user info fetch",
      "stub_files": ["auth/google_client.py"],
      "stub_test_files": ["tests/test_google_client.py"],
      "allowed_files": ["auth/google_client.py"],
      "read_only_context": ["auth/schemas.py", "auth/config.py", "models.py"],
      "dependencies": [],
      "complexity_weight": 0.25
    }
  ],
  "shared_files": ["auth/schemas.py", "auth/exceptions.py", "auth/config.py", "auth/__init__.py"],
  "integration_test_files": ["tests/test_integration_auth.py"],
  "requirements_additions": ["google-auth", "google-auth-oauthlib", "requests"]
}
```

#### Coordinator Validation

After the coordinator returns the decomposition, the validator validates it programmatically before committing the scaffolding:

1. All stub file paths are unique across subtasks (no overlaps)
2. All stub files parse as valid Python (`ast.parse()`)
3. All shared files parse as valid Python
4. Complexity weights sum to 1.0
5. No circular dependencies in the dependency graph
6. All imports in stub files reference existing repo files, shared files, or standard library
7. Each subtask has at least one stub test file
8. Stub tests actually fail when run against the scaffolding (confirming stubs raise NotImplementedError)

If validation fails, retry the coordinator with the specific errors appended to the prompt.

#### Coordinator Model Selection

The coordinator MUST use the best available frontier model (Opus/Sonnet class or equivalent). Decomposition quality determines the entire system's success rate. This is where you spend money. Miner inference can be cheap because the problem is constrained. Coordinator inference must be high quality because the problem is open-ended.

#### Coordinator System Prompt

The complete system prompt for the coordinator agent. This runs on the validator side using a frontier model.

```
You are the BitSwarm Coordinator. Your job is to decompose a feature specification into a scaffolded codebase that multiple independent coding agents can implement in parallel without communication.

You produce EXECUTABLE CODE, not descriptions. Every output you generate must parse as valid Python.

## Your Role

You receive:
1. A target repository (cloned locally, files readable via tools)
2. A natural language feature specification
3. Optional test hints from the requester

You produce a JSON decomposition containing:
- Shared files: complete, working Python files (types, schemas, configs, exceptions) that multiple subtasks import from
- Stub files: real Python files with correct signatures, type hints, docstrings, and `raise NotImplementedError` as every function body
- Stub tests: pytest files that FAIL on stubs and PASS on correct implementations
- Integration tests: pytest files testing cross-component behavior
- A subtask manifest mapping each subtask to its files, tests, and metadata

## Critical Constraints

IMPORTANT: The agents implementing your stubs CANNOT communicate with each other. They CANNOT ask you for clarification. They work ONLY from the code you produce. Every ambiguity in your scaffolding becomes a failure.

This means:
- Every function signature must be exact. If a function returns `User`, define `User` in a shared file. Do not leave return types as `dict` when a model is needed.
- Every import in a stub file must resolve. If a stub imports `from auth.schemas import OAuthToken`, then `auth/schemas.py` must exist in shared files with `OAuthToken` fully defined.
- Every import in a stub TEST file must also resolve. Tests import types too. If a test asserts `isinstance(result, OAuthUserInfo)`, then `OAuthUserInfo` must be defined in a shared file with the EXACT field names the test references. If the test checks `info.picture` but you define the field as `avatar_url`, the miner writes correct code that fails for the wrong reason.
- Every constant or configuration value that multiple subtasks need must be in a shared file. Miners cannot create shared types.
- Docstrings must specify behavior precisely enough to write tests against. Include: expected return values for common inputs, exception conditions with exception types, and side effects.
- Type hints must be complete. No `Any` types. No untyped parameters.

IMPORTANT: If your decomposition fails validation (and it will sometimes), you will be retried with the specific errors appended. For example: "Your decomposition failed validation: auth/schemas.py references OAuthUserInfo but this type is not defined in any shared file." Read the error carefully and fix the specific issue. Do not regenerate the entire decomposition from scratch unless the errors are pervasive. Budget: you have up to 3 attempts to produce valid scaffolding.

## How to Decompose

Step 1: Read the repository structure. Understand existing conventions, frameworks, import patterns, and test patterns. Use file_read and bash tools to explore.

Step 2: Identify the natural boundaries in the feature. Good boundaries are:
- Different external services (OAuth client vs database layer vs API routes)
- Different layers (data access vs business logic vs HTTP handlers)
- Different functional areas (authentication vs session management vs user profile)

Step 3: For each boundary, determine the interface contract. What functions does component A call on component B? These interfaces become your stub signatures.

Step 4: Extract every shared type. Any type that appears in more than one subtask's signatures MUST be in a shared file. Err on the side of MORE shared types, not fewer. A miner who needs a type that doesn't exist in shared files will fail.

Step 5: Write stub tests FIRST, then write stubs that match the tests. This ensures your tests actually test the contract. Each stub test should:
- Test happy path behavior (function returns expected type with expected values)
- Test at least one error condition (function raises expected exception)
- Use mocking for external dependencies (network calls, database queries)
- Import from the stub file using the exact module path

Step 6: Write integration tests that test the boundaries between subtasks. These should mock external services but NOT mock the internal components.

## Decomposition Quality Heuristics

- Prefer 3-5 subtasks for a typical feature. 2 is too few (not enough parallelism). 8+ is too many (integration complexity).
- Each subtask should have 2-6 functions to implement. 1 function is too trivial. 10+ functions means the subtask should be split.
- No circular dependencies between subtasks. The dependency graph must be a DAG.
- File paths must NEVER overlap between subtasks. Each file belongs to exactly one subtask.
- Complexity weights must sum to 1.0 and reflect actual implementation difficulty, not line count.

## Anti-Patterns — Do NOT Do These

- Do NOT write vague docstrings like "Process the data and return results." Specify WHAT data, HOW to process, and WHAT the results look like.
- Do NOT create stub functions that are impossible to implement without information not in the docstring or type hints.
- Do NOT put business logic in shared files. Shared files contain types, schemas, configs, constants, and exceptions ONLY.
- Do NOT create subtasks that depend on each other's implementation details. Dependencies should only flow through shared types and the stub interfaces.
- Do NOT create tests that test implementation details (like which HTTP library is used). Test behavior (given this input, expect this output/exception).
- Do NOT add dependencies that aren't already in the repo's requirements unless absolutely necessary for the feature, and list any additions explicitly in the manifest.

## Output Format

Respond with a single JSON object matching this schema:

{
  "task_id": "<from input>",
  "subtasks": [
    {
      "subtask_id": "string (snake_case, descriptive)",
      "description": "string (1-2 sentences, what this subtask implements)",
      "stub_files": ["path/to/stub1.py", "path/to/stub2.py"],
      "stub_test_files": ["tests/test_stub1.py"],
      "allowed_files": ["path/to/stub1.py", "path/to/stub2.py"],
      "read_only_context": ["path/to/existing_file.py", "shared/schemas.py"],
      "dependencies": ["other_subtask_id"],
      "complexity_weight": 0.25
    }
  ],
  "shared_files": {
    "path/to/shared.py": "full file content as string"
  },
  "stub_files": {
    "path/to/stub.py": "full file content as string with NotImplementedError bodies"
  },
  "stub_test_files": {
    "tests/test_stub.py": "full test file content as string"
  },
  "integration_test_files": {
    "tests/test_integration.py": "full integration test content as string"
  },
  "requirements_additions": ["package-name"]
}

Every file content value must be valid Python that passes ast.parse().
Every import must resolve to either: an existing repo file, a shared file you're creating, a stub file you're creating, a package in requirements, or the standard library.
Complexity weights must sum to 1.0.
Stub test files must FAIL when run against stubs (because stubs raise NotImplementedError).
```

#### Coordinator Self-Verification Loop

Before accepting the decomposition, the validator runs programmatic verification and retries the coordinator with specific errors on failure. Budget 2-3 coordinator retries per task.

```python
"""
Coordinator self-verification and retry protocol.
The coordinator's error-feedback-retry loop is just as important
as the miner's. Scaffolding breaks will break EVERY miner downstream.
"""

MAX_COORDINATOR_RETRIES = 3

def verify_and_retry_coordinator(
    coordinator_fn,  # callable that returns decomposition JSON
    repo_root: str,
    feature_spec: str,
    test_hints: str | None,
) -> dict | None:
    errors_from_previous = []

    for attempt in range(1, MAX_COORDINATOR_RETRIES + 1):
        # Call coordinator (with any previous errors appended)
        decomposition = coordinator_fn(
            repo_root=repo_root,
            feature_spec=feature_spec,
            test_hints=test_hints,
            previous_errors=errors_from_previous,
        )

        errors = []

        # Check 1: All files parse as valid Python
        for path, content in {
            **decomposition["shared_files"],
            **decomposition["stub_files"],
            **decomposition["stub_test_files"],
            **decomposition.get("integration_test_files", {}),
        }.items():
            try:
                ast.parse(content)
            except SyntaxError as e:
                errors.append(f"SyntaxError in {path} line {e.lineno}: {e.text}")

        # Check 2: All imports in stubs resolve to existing/shared/stdlib
        for path, content in decomposition["stub_files"].items():
            for module in extract_imports(content):
                if not resolves(module, repo_root, decomposition["shared_files"]):
                    errors.append(
                        f"Unresolved import in {path}: '{module}' — "
                        f"define it in a shared file or fix the import path"
                    )

        # Check 3: All imports in TEST files also resolve
        for path, content in decomposition["stub_test_files"].items():
            for module in extract_imports(content):
                if not resolves(module, repo_root, decomposition["shared_files"]):
                    errors.append(
                        f"Unresolved import in test {path}: '{module}' — "
                        f"tests import types too; ensure they exist in shared files"
                    )

        # Check 4: All types referenced in test assertions exist with correct fields
        for path, content in decomposition["stub_test_files"].items():
            type_issues = check_type_field_consistency(
                content, decomposition["shared_files"]
            )
            errors.extend(type_issues)

        # Check 5: No file path overlaps between subtasks
        all_paths = []
        for st in decomposition["subtasks"]:
            for f in st["stub_files"]:
                if f in all_paths:
                    errors.append(f"File path overlap: {f} assigned to multiple subtasks")
                all_paths.append(f)

        # Check 6: Complexity weights sum to 1.0
        total_weight = sum(st["complexity_weight"] for st in decomposition["subtasks"])
        if abs(total_weight - 1.0) > 0.01:
            errors.append(f"Complexity weights sum to {total_weight}, expected 1.0")

        # Check 7: Write files to disk and run stub tests — verify they FAIL
        # (confirming stubs actually raise NotImplementedError)
        write_scaffolding_to_disk(decomposition, repo_root)
        for st in decomposition["subtasks"]:
            for test_file in st["stub_test_files"]:
                result = run_pytest(test_file, repo_root)
                if result.returncode == 0:
                    errors.append(
                        f"Stub test {test_file} PASSED on scaffolding — "
                        f"tests should FAIL on NotImplementedError stubs. "
                        f"The test is probably a no-op or doesn't call the stub."
                    )

        if not errors:
            return decomposition  # Validation passed

        # Feed errors back for retry
        errors_from_previous = errors
        # Log for metrics
        log_coordinator_retry(attempt, errors)

    # All retries exhausted
    return None  # Release task back to queue
```

#### Context Selection Algorithm

How the coordinator selects which repo files to include as read-only context for each subtask.

```python
"""
BitSwarm Context Selection Algorithm

Determines which repository files to include as read_only_context
for each subtask, so miners have enough information to implement
their stubs without access to the full repository.

Incorporates claw-code's context prioritization patterns:
- Static analysis of imports (what the stub actually references)
- Proximity scoring (files near the stub in the directory tree)
- Convention files (README, setup.cfg, pyproject.toml for project context)
- Size budgeting (total context must fit in a token budget)
"""

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path


# --- Configuration ---

MAX_CONTEXT_FILES_PER_SUBTASK = 15
MAX_CONTEXT_BYTES_PER_SUBTASK = 200_000  # ~50K tokens
CONVENTION_FILES = [
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "README.md",
    ".flake8",
    ".pylintrc",
    "mypy.ini",
]


@dataclass
class ScoredFile:
    path: str
    score: float
    reason: str
    size_bytes: int


def extract_imports(file_content: str) -> list[str]:
    """Extract all import module paths from a Python file."""
    try:
        tree = ast.parse(file_content)
    except SyntaxError:
        return []

    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.append(node.module)
    return modules


def module_to_possible_paths(module: str, repo_root: str) -> list[str]:
    """Convert a dotted module path to possible file paths."""
    parts = module.split(".")
    candidates = []

    # Try as package/__init__.py
    pkg_path = os.path.join(repo_root, *parts, "__init__.py")
    if os.path.isfile(pkg_path):
        candidates.append(os.path.relpath(pkg_path, repo_root))

    # Try as module.py
    mod_path = os.path.join(repo_root, *parts) + ".py"
    if os.path.isfile(mod_path):
        candidates.append(os.path.relpath(mod_path, repo_root))

    # Try parent as package (from foo.bar import baz → foo/bar.py)
    if len(parts) > 1:
        parent_mod = os.path.join(repo_root, *parts[:-1]) + ".py"
        if os.path.isfile(parent_mod):
            candidates.append(os.path.relpath(parent_mod, repo_root))

    return candidates


def compute_directory_distance(path_a: str, path_b: str) -> int:
    """Number of directory hops between two file paths."""
    parts_a = Path(path_a).parts[:-1]  # directories only
    parts_b = Path(path_b).parts[:-1]

    # Find common prefix length
    common = 0
    for a, b in zip(parts_a, parts_b):
        if a == b:
            common += 1
        else:
            break

    return (len(parts_a) - common) + (len(parts_b) - common)


def select_context_for_subtask(
    subtask_stub_files: list[str],
    subtask_stub_content: dict[str, str],  # path -> content
    subtask_test_content: dict[str, str],  # path -> content (test files also import types)
    shared_files: list[str],
    all_repo_files: list[str],
    repo_root: str,
) -> list[str]:
    """
    Select read_only_context files for a single subtask.
    
    Priority tiers (from claw-code's context prioritization):
    
    Tier 1 (ALWAYS include): Shared files imported by stubs AND by tests
    Tier 2 (HIGH priority): Existing repo files imported by stubs or tests
    Tier 3 (MEDIUM priority): Files in same directory as stubs
    Tier 4 (LOW priority): Convention/config files
    Tier 5 (LOWEST priority): Files imported by Tier 2 files (transitive)
    
    Budget: Stay under MAX_CONTEXT_FILES and MAX_CONTEXT_BYTES.
    """
    scored: dict[str, ScoredFile] = {}

    # --- Tier 1: Shared files imported by stubs AND tests (score 100) ---
    # Tests import types too. If a test checks isinstance(result, OAuthUserInfo),
    # the miner needs the shared file that defines OAuthUserInfo in their context.
    all_imports = set()
    for stub_path, content in subtask_stub_content.items():
        all_imports.update(extract_imports(content))
    for test_path, content in subtask_test_content.items():
        all_imports.update(extract_imports(content))

    for module in all_imports:
        for fpath in module_to_possible_paths(module, repo_root):
            if fpath in shared_files:
                size = os.path.getsize(os.path.join(repo_root, fpath))
                scored[fpath] = ScoredFile(
                    path=fpath, score=100, reason="shared_import", size_bytes=size
                )

    # --- Tier 2: Existing repo files imported by stubs or tests (score 80) ---
    for module in all_imports:
        for fpath in module_to_possible_paths(module, repo_root):
            if fpath not in scored and fpath not in subtask_stub_files:
                size = os.path.getsize(os.path.join(repo_root, fpath))
                scored[fpath] = ScoredFile(
                    path=fpath, score=80, reason="direct_import", size_bytes=size
                )

    # --- Tier 3: Sibling files in same directory (score 40-60) ---
    stub_dirs = set()
    for stub_path in subtask_stub_files:
        stub_dirs.add(os.path.dirname(stub_path))

    for repo_file in all_repo_files:
        if repo_file in scored or repo_file in subtask_stub_files:
            continue
        file_dir = os.path.dirname(repo_file)
        if file_dir in stub_dirs:
            size = os.path.getsize(os.path.join(repo_root, repo_file))
            # __init__.py gets higher score (defines package interface)
            score = 60 if repo_file.endswith("__init__.py") else 40
            scored[repo_file] = ScoredFile(
                path=repo_file, score=score, reason="sibling_file", size_bytes=size
            )

    # --- Tier 4: Convention files (score 30) ---
    for conv_file in CONVENTION_FILES:
        conv_path = os.path.join(repo_root, conv_file)
        if os.path.isfile(conv_path) and conv_file not in scored:
            size = os.path.getsize(conv_path)
            scored[conv_file] = ScoredFile(
                path=conv_file, score=30, reason="convention", size_bytes=size
            )

    # --- Tier 5: Transitive imports from Tier 2 (score 20) ---
    tier2_files = [s for s in scored.values() if s.reason == "direct_import"]
    for t2 in tier2_files:
        try:
            t2_abs = os.path.join(repo_root, t2.path)
            with open(t2_abs, "r") as f:
                t2_content = f.read()
            t2_imports = extract_imports(t2_content)
            for module in t2_imports:
                for fpath in module_to_possible_paths(module, repo_root):
                    if fpath not in scored and fpath not in subtask_stub_files:
                        size = os.path.getsize(os.path.join(repo_root, fpath))
                        scored[fpath] = ScoredFile(
                            path=fpath, score=20, reason="transitive_import",
                            size_bytes=size,
                        )
        except (OSError, UnicodeDecodeError):
            continue

    # --- Budget-constrained selection ---
    ranked = sorted(scored.values(), key=lambda s: -s.score)

    selected = []
    total_bytes = 0
    for item in ranked:
        if len(selected) >= MAX_CONTEXT_FILES_PER_SUBTASK:
            break
        if total_bytes + item.size_bytes > MAX_CONTEXT_BYTES_PER_SUBTASK:
            continue  # Skip large files, try smaller ones
        selected.append(item.path)
        total_bytes += item.size_bytes

    return selected
```

---

### 3.3 Miner Agent Runtime


Each miner runs an autonomous coding agent inside a sandboxed environment. The miner's agent architecture, model provider, and tooling are entirely up to the miner. The subnet only constrains the output.

**What the miner receives (via Synapse):**
- The scaffolded repository (with scaffolding commit already applied)
- Their subtask assignment from the manifest (which stub files to implement)
- The stub tests their implementation must pass
- A timeout deadline

**What the miner does:**
The miner's job is constrained: replace `raise NotImplementedError` in their assigned stub files with working implementations. They read the function signatures, type hints, and docstrings to understand what each function should do. They read the shared files and context files to understand the codebase conventions. They write the implementation. They run the stub tests. If tests fail, they iterate. When tests pass, they generate a git diff and return it.

This is fundamentally different from Claude Code's prompting model, where agents receive open-ended instructions and have full codebase access. BitSwarm miners receive a constrained assignment with explicit boundaries. The prompt is closer to "make these 6 specific pytest tests pass by implementing these 4 specific functions" than "add OAuth to this app."

**Miner returns (via Synapse response):**
- `patch`: unified diff (git diff format) scoped to allowed files
- `stub_tests_passed`: bool
- `stub_test_output`: stdout/stderr from pytest
- `files_modified`: list of file paths changed
- `execution_time_seconds`: float

**Miner's Internal Loop:**

```
1. Read stub file(s) assigned to this subtask
2. Read docstrings and type hints to understand expected behavior
3. Read shared files (schemas, configs) to understand data types
4. Read context files (existing app code) to understand conventions
5. Write implementation replacing NotImplementedError
6. Run stub tests: pytest tests/test_<subtask>.py
7. If tests fail, read error output, fix implementation, go to 6
8. If tests pass, generate git diff of allowed files, return
```

The miner pays for their own inference costs. Miners using cheaper models (open source via Chutes, local inference) have lower costs but potentially lower quality. Miners using expensive models (commercial APIs) produce better code but have higher margins to cover. The market finds equilibrium.

**Sandboxing:**

The miner runtime MUST be sandboxed. Miners run untrusted code (their agents produce arbitrary code).

- Docker container with no network access during code execution (miner fetches model inference externally, but generated code runs sandboxed)
- Read-only mount of the scaffolded repo
- Writable workspace for the miner's changes
- Time limit enforced by the container runtime
- Resource limits (CPU, memory) to prevent DoS

#### Miner System Prompt

The complete system prompt for the miner agent. Designed for one-shot, no-communication execution inside a sandboxed environment.

```
You are a BitSwarm Miner Agent. Your job is to implement Python functions that currently raise NotImplementedError, making all assigned tests pass.

## What You Have

You are working inside a repository that has already been scaffolded. The scaffolding includes:
- Stub files assigned to you: real Python files with correct function signatures, type hints, docstrings, and `raise NotImplementedError` as every function body
- Shared files: complete implementations of types, schemas, configs, and exceptions that your code imports from
- Stub tests: pytest files that will PASS when you correctly implement the stubs

Your assignment message includes:
- An annotated project file tree showing the full repository structure, which files are yours, which are shared (read-only), and which are assigned to other engineers
- The contents of your stub files, test files, and shared schema files pre-loaded so you can start immediately
- Which files you may write to (your allowed_files) and which tests to pass

## What You Must Do

1. Review the stub files, test files, and shared schemas already provided in your assignment
2. Understand every function signature, type hint, and docstring in your stub files
3. Understand what each test expects (the tests are the ground truth contract)
4. Replace every `raise NotImplementedError` with a working implementation
5. Run the stub tests: `pytest <test_file> -v --tb=short`
6. If tests fail, read the error output, fix your implementation, and re-run
7. When all tests pass, stop

## Strict Rules

CRITICAL: You may ONLY modify files listed in your allowed_files. Do not create new files. Do not modify test files. Do not modify shared files. Any change outside allowed_files will be rejected and you will score zero.

CRITICAL: Preserve every function signature exactly. Do not change function names, parameter names, parameter types, or return types. Do not add parameters. Do not change the public interface.

CRITICAL: Do not add new dependencies. Use only packages already in the repository's requirements. If you need functionality from an uninstalled package, implement it using available packages.

CRITICAL: Do not create new public functions, classes, or module-level variables in your stub files beyond what already exists. You may create private helper functions (prefixed with _) inside the file if needed.

IMPORTANT: Read the docstring carefully. It specifies the expected behavior. Your implementation must match the docstring, not your assumptions about what the function "should" do.

IMPORTANT: Read the test file before implementing. The tests are the ground truth contract. If the docstring and tests disagree, match the tests.

IMPORTANT: Use the same coding style as the existing repository. Match import patterns, naming conventions, error handling patterns, and formatting.

## Implementation Strategy

For each stub file:
1. Read the entire file to understand all functions and their relationships
2. Read the corresponding test file to understand expected behavior and edge cases
3. Read shared files that are imported to understand types and constants
4. Implement functions in dependency order (if function A calls function B in the same file, implement B first)
5. After implementing all functions in a file, run the tests
6. Fix any failures based on the test output

## When Tests Fail

Read the FULL test output. Pay attention to:
- The assertion that failed (tells you what the expected vs actual value was)
- The traceback (tells you which line in your implementation caused the error)
- The test name (tells you which behavior is wrong)

Common failure patterns:
- TypeError: You're returning the wrong type. Check the return type hint and the test assertion.
- AssertionError: Your output doesn't match expected. Re-read the docstring for the exact specification.
- ImportError: You're importing something that doesn't exist. Only import from shared files and standard library.
- AttributeError: You're accessing a field that doesn't exist on a type. Re-read the schema definition in shared files.
- Mock-related failures: The test mocks an external call. Your implementation must use the exact module path that the test patches.

After a failure:
- Do NOT rewrite everything from scratch. Read the specific error, identify the specific line, fix the specific issue.
- Do NOT change test files to make them pass. The tests are the contract.
- Do NOT add try/except blocks to swallow errors that tests expect to be raised.

## Tools Available

You have these tools:
- file_read: Read any file in the repository
- file_write: Write to files in your allowed_files list ONLY
- bash: Run shell commands (pytest, python, grep, find, etc.)
- list_files: List directory contents without a full bash invocation

Read before you write. Always read a file with file_read before modifying it with file_write.
Run tests with: bash("pytest <test_file_path> -v --tb=short")
Use bash for search: bash("grep -rn 'pattern' path/")
Use list_files to explore directories you haven't seen.

Note: Your stub files, test files, and shared schemas are pre-loaded in your assignment message. You do NOT need to file_read those — start implementing immediately. Use file_read only for additional context files you want to inspect. Use the project file tree in your assignment to understand import paths — do not guess.

## Output Efficiency

Go straight to the point. Your stub and test content is already in your assignment — start implementing.
Do not narrate your plan. Do not explain your reasoning at length. Act.
Keep going until all tests pass or you have exhausted your iteration budget.
Do not stop after writing code. You must run the tests. Claiming you're done without test output is not acceptable.
```

#### Miner Warm-Start Context Block

Instead of making the miner read every file via tools (costing tool-call tokens), pre-load the most critical files directly into the user message. Also include an annotated project file tree so the miner knows where it sits in the codebase.

```python
def build_annotated_file_tree(
    repo_root: str,
    subtask_allowed_files: list[str],
    subtask_test_files: list[str],
    shared_files: list[str],
    all_subtask_files: dict[str, list[str]],  # subtask_id -> files (for "other engineer" labels)
) -> str:
    """
    Build an annotated file tree that shows the miner exactly where
    everything is and what they can/cannot touch.
    """
    # Walk the repo and annotate each file
    lines = ["Project structure:"]
    for root, dirs, files in os.walk(repo_root):
        # skip noise
        dirs[:] = [d for d in sorted(dirs)
                    if not d.startswith(".") and d not in ("__pycache__", "venv", "node_modules")]
        depth = root.replace(repo_root, "").count(os.sep)
        indent = "│   " * depth
        rel_root = os.path.relpath(root, repo_root)
        if rel_root != ".":
            lines.append(f"{indent}├── {os.path.basename(root)}/")
        for f in sorted(files):
            if f.startswith("."):
                continue
            rel_path = os.path.relpath(os.path.join(root, f), repo_root)
            file_indent = "│   " * (depth + 1)
            # Annotate
            if rel_path in subtask_allowed_files:
                lines.append(f"{file_indent}├── {f}  ← YOUR ASSIGNMENT")
            elif rel_path in subtask_test_files:
                lines.append(f"{file_indent}├── {f}  ← YOUR TESTS")
            elif rel_path in shared_files:
                lines.append(f"{file_indent}├── {f}  [shared - do not modify]")
            elif any(rel_path in files for files in all_subtask_files.values()):
                lines.append(f"{file_indent}├── {f}  [assigned to another engineer]")
            else:
                lines.append(f"{file_indent}├── {f}")
    return "\n".join(lines)


user_message = f"""
Your assignment: {subtask_id}
Description: {subtask_description}
Files to implement: {allowed_files}
Tests to pass: {test_files}

{annotated_file_tree}

=== STUB FILE: {stub_path} ===
{stub_content}

=== TEST FILE: {test_path} ===
{test_content}

=== SHARED SCHEMAS: {schema_path} ===
{schema_content}

Start implementing. Run tests with: pytest {test_path} -v --tb=short
"""
```

#### Miner Tool Definitions

Exact tool schemas for the miner agent runtime, modeled after claw-code's tool registration pattern but scoped for BitSwarm's constraints.

```python
"""
BitSwarm Miner Tool Definitions

Tools are registered as a list of dicts matching the Anthropic tool-use schema.
Each tool has an execution function and a pre-execution validator.
"""

from typing import Any
import json
import subprocess
import os

# --- Configuration ---

REPO_ROOT: str = ""  # Set at runtime
ALLOWED_FILES: list[str] = []  # Set from subtask assignment
STUB_TEST_FILES: list[str] = []  # Set from subtask assignment
BASH_TIMEOUT_SECONDS: int = 60
MAX_FILE_READ_BYTES: int = 512_000  # 500KB per read
MAX_FILE_WRITE_BYTES: int = 1_048_576  # 1MB per write


# --- Tool Schemas (Anthropic format) ---

TOOL_DEFINITIONS = [
    {
        "name": "file_read",
        "description": (
            "Read the contents of a file in the repository. "
            "Use this to read stub files, shared files, test files, "
            "and existing repo files for context. "
            "Returns the file content as a string. "
            "Fails if the file does not exist or exceeds size limits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from repository root to the file to read. "
                        "Example: 'auth/google_client.py' or 'tests/test_google_client.py'"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "Optional byte offset to start reading from. Default 0. "
                        "Use for reading large files in chunks."
                    ),
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Optional maximum bytes to read. Default reads entire file up to size limit."
                    ),
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_write",
        "description": (
            "Write content to a file. ONLY works for files in your allowed_files list. "
            "Writes the COMPLETE file content — this is a full replace, not a patch. "
            "The file must already exist (you are replacing NotImplementedError stubs). "
            "Fails if the path is not in allowed_files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from repository root. "
                        "Must be in your allowed_files list."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The complete file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "bash",
        "description": (
            "Execute a shell command and return stdout and stderr. "
            "Use for: running pytest, searching files with grep/find, "
            "checking Python syntax, inspecting the repository. "
            "Commands run in the repository root directory. "
            "Network access is disabled. "
            "Commands time out after 60 seconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to execute. "
                        "For tests: 'pytest tests/test_file.py -v --tb=short' "
                        "For search: 'grep -rn pattern path/' "
                        "For structure: 'find . -name \"*.py\" -not -path \"./venv/*\"'"
                    ),
                },
            },
            "required": ["command"],
        },
    },
    # --- Optional 4th tool (add if miners waste bash calls on 'ls') ---
    {
        "name": "list_files",
        "description": (
            "List files and directories at a given path. Lightweight alternative "
            "to bash('ls'). Returns a flat listing with file sizes. "
            "Use to understand project structure or check what exists in a directory "
            "without a full bash invocation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Relative path from repository root. "
                        "Default '.' for repository root. "
                        "Example: 'auth/' or 'tests/'"
                    ),
                    "default": ".",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum directory depth to list. Default 2.",
                    "default": 2,
                },
            },
            "required": [],
        },
    },
]


# --- Pre-execution Validators ---

def validate_file_read(params: dict[str, Any]) -> tuple[bool, str]:
    """Validate file_read before execution. Returns (ok, error_message)."""
    path = params.get("path", "")
    abs_path = os.path.normpath(os.path.join(REPO_ROOT, path))

    # Path traversal check
    if not abs_path.startswith(os.path.normpath(REPO_ROOT)):
        return False, f"Path traversal denied: {path}"

    if not os.path.isfile(abs_path):
        return False, f"File not found: {path}"

    size = os.path.getsize(abs_path)
    if size > MAX_FILE_READ_BYTES:
        return False, (
            f"File too large: {size} bytes (limit {MAX_FILE_READ_BYTES}). "
            f"Use offset and limit params to read in chunks."
        )

    return True, ""


def validate_file_write(params: dict[str, Any]) -> tuple[bool, str]:
    """Validate file_write before execution. Enforces allowed_files scope."""
    path = params.get("path", "")
    content = params.get("content", "")
    abs_path = os.path.normpath(os.path.join(REPO_ROOT, path))

    # Path traversal check
    if not abs_path.startswith(os.path.normpath(REPO_ROOT)):
        return False, f"Path traversal denied: {path}"

    # Scope check — the primary security boundary
    normalized_allowed = [
        os.path.normpath(os.path.join(REPO_ROOT, f)) for f in ALLOWED_FILES
    ]
    if abs_path not in normalized_allowed:
        return False, (
            f"SCOPE VIOLATION: {path} is not in your allowed_files. "
            f"You may only modify: {ALLOWED_FILES}. "
            f"This attempt has been logged. Modify only your assigned files."
        )

    # Size check
    if len(content.encode("utf-8")) > MAX_FILE_WRITE_BYTES:
        return False, f"Content too large: limit is {MAX_FILE_WRITE_BYTES} bytes."

    # Syntax check — the file must be valid Python
    try:
        import ast
        ast.parse(content)
    except SyntaxError as e:
        return False, (
            f"SYNTAX ERROR in your code: {e}. "
            f"Fix the syntax before writing. Line {e.lineno}: {e.text}"
        )

    # Public interface check — miner must not add new public symbols
    # Parse the original stub and compare public names
    try:
        original_path = abs_path  # file already exists (it's a stub)
        if os.path.isfile(original_path):
            with open(original_path, "r") as f:
                original_content = f.read()
            original_public = _extract_public_names(original_content)
            new_public = _extract_public_names(content)
            added = new_public - original_public
            if added:
                return False, (
                    f"INTERFACE VIOLATION: You added new public symbols that "
                    f"were not in the original stub: {added}. "
                    f"You may only implement existing functions/classes. "
                    f"Private helpers (prefixed with _) are allowed."
                )
    except Exception:
        pass  # If comparison fails, allow the write (syntax already validated)

    return True, ""


def _extract_public_names(source: str) -> set[str]:
    """Extract public (non-underscore-prefixed) top-level names from Python source."""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
    return names


BASH_BLOCKED_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "curl ",
    "wget ",
    "pip install",
    "apt ",
    "sudo ",
    "chmod ",
    "chown ",
    "mkfs",
    "dd if=",
    "> /dev/",
    "shutdown",
    "reboot",
    "kill -9",
    "pkill",
    "nc ",        # netcat
    "ncat ",
    "ssh ",
    "scp ",
]


def validate_bash(params: dict[str, Any]) -> tuple[bool, str]:
    """Validate bash command before execution."""
    command = params.get("command", "")

    for pattern in BASH_BLOCKED_PATTERNS:
        if pattern in command.lower():
            return False, f"Blocked command pattern: {pattern}"

    return True, ""


def validate_list_files(params: dict[str, Any]) -> tuple[bool, str]:
    """Validate list_files before execution."""
    path = params.get("path", ".")
    abs_path = os.path.normpath(os.path.join(REPO_ROOT, path))

    if not abs_path.startswith(os.path.normpath(REPO_ROOT)):
        return False, f"Path traversal denied: {path}"

    if not os.path.isdir(abs_path):
        return False, f"Directory not found: {path}"

    return True, ""


# --- Tool Execution Functions ---

def execute_file_read(params: dict[str, Any]) -> str:
    """Execute file_read and return content."""
    path = params["path"]
    abs_path = os.path.normpath(os.path.join(REPO_ROOT, path))
    offset = params.get("offset", 0)
    limit = params.get("limit")

    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
        if offset:
            f.seek(offset)
        content = f.read(limit) if limit else f.read()

    return content


def execute_file_write(params: dict[str, Any]) -> str:
    """Execute file_write and return confirmation."""
    path = params["path"]
    abs_path = os.path.normpath(os.path.join(REPO_ROOT, path))
    content = params["content"]

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)

    line_count = content.count("\n") + 1
    return f"Written {len(content)} bytes ({line_count} lines) to {path}"


def execute_bash(params: dict[str, Any]) -> str:
    """Execute bash command inside sandbox and return output."""
    command = params["command"]

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT_SECONDS,
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output += f"\n[exit code: {result.returncode}]"
        return output.strip()

    except subprocess.TimeoutExpired:
        return f"[TIMEOUT: command exceeded {BASH_TIMEOUT_SECONDS}s limit]"
    except Exception as e:
        return f"[ERROR: {e}]"


def execute_list_files(params: dict[str, Any]) -> str:
    """Execute list_files and return directory listing."""
    path = params.get("path", ".")
    max_depth = params.get("max_depth", 2)
    abs_path = os.path.normpath(os.path.join(REPO_ROOT, path))

    lines = []
    for root, dirs, files in os.walk(abs_path):
        depth = root.replace(abs_path, "").count(os.sep)
        if depth >= max_depth:
            dirs.clear()
            continue
        # Skip hidden dirs and common noise
        dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and d != "__pycache__" and d != "node_modules" and d != "venv"]
        indent = "  " * depth
        rel_root = os.path.relpath(root, REPO_ROOT)
        lines.append(f"{indent}{rel_root}/")
        for f in sorted(files):
            if f.startswith("."):
                continue
            fpath = os.path.join(root, f)
            size = os.path.getsize(fpath)
            lines.append(f"{indent}  {f}  ({size} bytes)")

    return "\n".join(lines) if lines else "(empty directory)"


# --- Tool Router ---

TOOL_REGISTRY = {
    "file_read": {
        "validate": validate_file_read,
        "execute": execute_file_read,
    },
    "file_write": {
        "validate": validate_file_write,
        "execute": execute_file_write,
    },
    "bash": {
        "validate": validate_bash,
        "execute": execute_bash,
    },
    "list_files": {
        "validate": validate_list_files,
        "execute": execute_list_files,
    },
}


def run_tool(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """
    Main tool execution entry point.
    Returns {"success": bool, "output": str}
    
    Pipeline: validate -> execute -> format result
    Modeled after claw-code's validate -> execute -> format pattern
    and Claude Code's checkPermissions -> call -> build result pipeline.
    """
    if name not in TOOL_REGISTRY:
        return {"success": False, "output": f"Unknown tool: {name}"}

    tool = TOOL_REGISTRY[name]

    # Pre-execution validation (claw-code pattern: check before execute)
    ok, error = tool["validate"](params)
    if not ok:
        return {"success": False, "output": error}

    # Execute
    try:
        output = tool["execute"](params)
        return {"success": True, "output": output}
    except Exception as e:
        return {"success": False, "output": f"Tool execution error: {e}"}
```

#### Error Recovery Protocol

The exact error-feedback-retry loop for miners, incorporating retry patterns from claw-code and OmX.

```python
"""
BitSwarm Miner Error Recovery Protocol

Defines the retry loop structure, what context carries forward between
retries, what gets reset, and how test failure output is formatted.
"""

from dataclasses import dataclass, field
from enum import Enum


# --- Configuration ---

MAX_ITERATIONS = 5  # Max implement-test-fix cycles
MAX_CONSECUTIVE_SAME_ERROR = 3  # Stop if same error repeats 3x
MAX_DIFFERENT_ERRORS_SAME_TEST = 3  # Hard reset if same test fails 3x with different errors
TEST_OUTPUT_MAX_CHARS = 4000  # Truncate test output fed back to agent
IMPLEMENTATION_APPROACH = "incremental_fix"  # Default; switches to "hard_reset" when thrashing


class StopReason(Enum):
    TESTS_PASSED = "tests_passed"
    MAX_ITERATIONS = "max_iterations_exhausted"
    REPEATED_ERROR = "same_error_repeated_3x"
    TIMEOUT = "execution_timeout"
    SCOPE_VIOLATION = "scope_violation"


class RecoveryAction(Enum):
    INCREMENTAL_FIX = "incremental_fix"
    HARD_RESET = "hard_reset"  # Wipe implementation, restart from stub


@dataclass
class IterationRecord:
    """Record of a single implement-test cycle."""
    iteration: int
    files_written: list[str]
    test_command: str
    tests_passed: bool
    test_output: str  # Full output
    error_summary: str  # Extracted error (for dedup)
    fix_description: str  # What the agent said it was fixing


@dataclass
class RetryState:
    """Persistent state across retry iterations."""
    iteration_count: int = 0
    history: list[IterationRecord] = field(default_factory=list)
    consecutive_same_error: int = 0
    last_error_signature: str = ""
    stop_reason: StopReason | None = None
    # Thrashing detection: same test failing with different errors each time
    per_test_error_variants: dict[str, set[str]] = field(default_factory=dict)
    hard_reset_triggered: bool = False


# --- Error Extraction ---

def extract_error_signature(test_output: str) -> str:
    """
    Extract a normalized error signature for dedup.
    Two errors are "the same" if they fail on the same test
    with the same exception type.
    """
    # Parse pytest output for FAILED lines
    failed_lines = []
    for line in test_output.split("\n"):
        if "FAILED" in line or "ERROR" in line:
            # Normalize: strip timing, file paths to basenames
            normalized = line.strip().split(" - ")[0]  # Remove messages after dash
            failed_lines.append(normalized)

    return "|".join(sorted(failed_lines))


def extract_per_test_errors(test_output: str) -> dict[str, str]:
    """
    Extract a mapping of test_name -> error_type for thrashing detection.
    If the same test fails 3x with a DIFFERENT error each time, the miner
    is patching patches — it should hard reset and start fresh.
    """
    per_test = {}
    current_test = None
    for line in test_output.split("\n"):
        # pytest -v format: "tests/test_foo.py::test_bar FAILED"
        if "FAILED" in line and "::" in line:
            current_test = line.split("::")[1].split(" ")[0].strip()
        # Capture the error type from traceback lines
        if current_test and ("Error:" in line or "Exception:" in line):
            error_type = line.strip().split(":")[0].strip()
            per_test[current_test] = error_type
            current_test = None
    return per_test


def detect_thrashing(state: RetryState, new_per_test: dict[str, str]) -> RecoveryAction:
    """
    Detect if the miner is thrashing: same test failing with different errors
    each iteration means the fix attempts are making things worse, not better.
    
    If any single test has accumulated 3+ distinct error types across iterations,
    trigger a hard reset: wipe the implementation back to the original stub and
    tell the agent to start fresh with a different approach.
    """
    for test_name, error_type in new_per_test.items():
        if test_name not in state.per_test_error_variants:
            state.per_test_error_variants[test_name] = set()
        state.per_test_error_variants[test_name].add(error_type)

        if len(state.per_test_error_variants[test_name]) >= MAX_DIFFERENT_ERRORS_SAME_TEST:
            return RecoveryAction.HARD_RESET

    return RecoveryAction.INCREMENTAL_FIX


def format_test_feedback(test_output: str, iteration: int) -> str:
    """
    Format test output for injection into the agent's next turn.
    
    Design decisions (from claw-code and OmX patterns):
    - Include the FULL error traceback, not just the assertion
    - Truncate stdout/captured output, not the error info
    - Prepend iteration context so the agent knows where it is
    - Include a "what to focus on" hint based on error type
    """
    # Truncate if needed, preserving the error section
    if len(test_output) > TEST_OUTPUT_MAX_CHARS:
        # Keep the last N chars (errors are at the end in pytest output)
        test_output = (
            "[...truncated...]\n" + test_output[-TEST_OUTPUT_MAX_CHARS:]
        )

    # Detect error type for guidance hint
    hint = ""
    if "TypeError" in test_output:
        hint = (
            "HINT: TypeError usually means wrong return type or wrong argument type. "
            "Check the function signature and return type hint."
        )
    elif "AssertionError" in test_output or "AssertionError" in test_output:
        hint = (
            "HINT: AssertionError means your output doesn't match expected. "
            "Re-read the docstring and test assertion carefully."
        )
    elif "ImportError" in test_output or "ModuleNotFoundError" in test_output:
        hint = (
            "HINT: Import error. Only import from shared files, existing repo files, "
            "and packages in requirements. Do not install new packages."
        )
    elif "AttributeError" in test_output:
        hint = (
            "HINT: AttributeError usually means you're accessing a field that "
            "doesn't exist on a type. Re-read the schema in shared files."
        )
    elif "NotImplementedError" in test_output:
        hint = (
            "HINT: NotImplementedError means you haven't replaced a stub yet. "
            "Check that you wrote ALL functions in the file, not just some."
        )
    elif "mock" in test_output.lower() or "patch" in test_output.lower():
        hint = (
            "HINT: Mock/patch related error. Your implementation must use the exact "
            "module path that the test's @patch decorator targets. Read the test's "
            "patch path and make sure your code imports from that same path."
        )

    return (
        f"--- TEST RESULTS (Iteration {iteration}) ---\n"
        f"{test_output}\n"
        f"--- END TEST RESULTS ---\n"
        f"{hint}\n"
        f"Fix the failing tests. Do not rewrite from scratch — identify the specific "
        f"error, fix the specific issue, and re-run."
    )


# --- The Retry Loop ---

def build_retry_context(state: RetryState) -> str:
    """
    Build context string to inject into the agent's prompt on retry.
    
    What carries forward (from OmX's approach-log pattern):
    - Which tests failed and why (the error, not the full output)
    - What was already tried (prevents repeating failed approaches)
    - Current iteration count
    
    What gets reset:
    - Default: nothing. The agent keeps its full conversation history.
    - Exception: on HARD RESET, the agent is told to wipe its implementation
      back to the original stub and start fresh. This triggers when the same
      test fails 3x with a different error each time (thrashing).
    
    This follows the "incremental fix" pattern from claw-code by default,
    switching to "hard context reset" (OmX's Ralph pattern) only when the
    agent is demonstrably thrashing — patching patches instead of converging.
    """
    if not state.history:
        return ""

    lines = [
        f"You are on iteration {state.iteration_count} of {MAX_ITERATIONS}.",
        "Previous attempts:",
    ]

    for record in state.history:
        status = "PASSED" if record.tests_passed else "FAILED"
        lines.append(
            f"  Iteration {record.iteration}: {status} — {record.error_summary or 'N/A'}"
        )

    if state.hard_reset_triggered:
        lines.append(
            f"\nHARD RESET: The same test has failed {MAX_DIFFERENT_ERRORS_SAME_TEST} times "
            f"with a DIFFERENT error each time. Your incremental fixes are making things "
            f"worse, not better. STOP patching. Re-read the original stub file and test "
            f"file from scratch. Write a completely new implementation. Do not reuse any "
            f"of your previous code."
        )
    elif state.consecutive_same_error >= 2:
        lines.append(
            f"\nWARNING: The same error has occurred {state.consecutive_same_error} times. "
            f"Your previous fix approach is not working. Try a fundamentally different "
            f"approach. Re-read the docstring and test file from scratch."
        )

    return "\n".join(lines)


def should_stop(state: RetryState) -> tuple[bool, StopReason | None]:
    """Determine if the retry loop should stop."""
    if state.history and state.history[-1].tests_passed:
        return True, StopReason.TESTS_PASSED

    if state.iteration_count >= MAX_ITERATIONS:
        return True, StopReason.MAX_ITERATIONS

    if state.consecutive_same_error >= MAX_CONSECUTIVE_SAME_ERROR:
        return True, StopReason.REPEATED_ERROR

    return False, None


def update_state(state: RetryState, record: IterationRecord) -> RetryState:
    """Update retry state after an iteration."""
    state.iteration_count += 1
    state.history.append(record)

    # Same-error detection (stop if identical error 3x)
    error_sig = extract_error_signature(record.test_output)
    if error_sig == state.last_error_signature and error_sig != "":
        state.consecutive_same_error += 1
    else:
        state.consecutive_same_error = 1
        state.last_error_signature = error_sig

    # Thrashing detection (hard reset if same test, different errors 3x)
    if not record.tests_passed:
        per_test = extract_per_test_errors(record.test_output)
        action = detect_thrashing(state, per_test)
        if action == RecoveryAction.HARD_RESET and not state.hard_reset_triggered:
            state.hard_reset_triggered = True
            # On hard reset: restore original stub files from scaffolding
            # The miner loop should re-write the original stub content
            # via file_write before the agent's next turn

    stop, reason = should_stop(state)
    if stop:
        state.stop_reason = reason

    return state


# --- Pseudocode for the full miner loop ---

"""
MINER EXECUTION LOOP (pseudocode):

state = RetryState()
messages = [system_prompt, user_assignment_message]

while True:
    # 1. Call the LLM with current messages + tools
    response = call_llm(messages, tools=TOOL_DEFINITIONS)
    
    # 2. Process tool calls in the response
    for tool_call in response.tool_calls:
        result = run_tool(tool_call.name, tool_call.params)
        messages.append(tool_result_message(tool_call.id, result))
        
        # Track if this was a test run
        if tool_call.name == "bash" and "pytest" in tool_call.params["command"]:
            tests_passed = "[exit code: 0]" in result["output"]
            
            record = IterationRecord(
                iteration=state.iteration_count + 1,
                files_written=[...],  # from recent file_write calls
                test_command=tool_call.params["command"],
                tests_passed=tests_passed,
                test_output=result["output"],
                error_summary=extract_error_signature(result["output"]),
                fix_description="",  # filled from agent's text response
            )
            state = update_state(state, record)
            
            if state.stop_reason:
                break
            
            if not tests_passed:
                # Inject formatted feedback for the agent
                feedback = format_test_feedback(result["output"], state.iteration_count)
                # This becomes part of the tool result the agent sees
    
    # 3. Check stopping conditions
    if state.stop_reason:
        break
    
    # 4. If agent produced no tool calls, it's done (or stuck)
    if not response.tool_calls:
        break

# 5. Generate patch from allowed files
patch = generate_git_diff(ALLOWED_FILES)
return MinerResult(
    patch=patch,
    tests_passed=state.stop_reason == StopReason.TESTS_PASSED,
    test_output=state.history[-1].test_output if state.history else "",
    iterations_used=state.iteration_count,
    stop_reason=state.stop_reason,
)
"""
```

---

### 3.4 Validator: Merge and Scoring Pipeline

When all miners for a task have submitted (or the deadline has passed), the validator runs the merge pipeline. The merger is intentionally simple. If the coordinator did its job, merging is mechanical.

**Phase 1: Patch Validation**

For each miner's patch, verify that only allowed files were modified. Reject any patch that touches out-of-scope files. Score: 0.

**Phase 2: Dependency-Ordered Apply**

Apply patches in the dependency order specified by the coordinator. Run `git apply --check` before each patch. If a patch can't apply cleanly, the miner scores 0. A clean decomposition produces zero conflicts because file paths never overlap. Conflicts indicate either a coordinator bug or a miner modifying files outside their scope.

**Phase 3: Stub Test Scoring (per miner)**

After all patches are applied, run each miner's stub tests individually:

```bash
pytest tests/test_<subtask_id>.py -v
```

This is the PRIMARY scoring signal. If the miner's stub tests pass, they fulfilled their contract. If they fail, they didn't. This is deterministic and objective. The miner has full control over whether their stub tests pass, because stub tests only depend on their code and the shared files.

**Phase 4: Integration Test Scoring (system-level)**

Run the integration tests on the fully merged codebase:

```bash
pytest tests/test_integration_*.py -v
```

This tests whether the components compose correctly. Integration test results affect scoring but do NOT override stub test results.

**Phase 5: Score Computation**

The scoring model is simple and fair. Miners are judged primarily on what they can control (their stub tests), not on what they can't control (other miners' quality).

```python
def compute_scores(task, miner_results, stub_results, integration_passed):
    scores = {}

    for subtask in task.subtasks:
        sid = subtask.subtask_id
        result = miner_results.get(sid)

        # No submission or empty patch
        if result is None or not result.patch:
            scores[sid] = 0.0
            continue

        # Patch touched unauthorized files
        if result.unauthorized_files:
            scores[sid] = 0.0
            continue

        # Patch didn't apply cleanly
        if result.merge_conflict:
            scores[sid] = 0.0
            continue

        # Stub tests failed
        if not stub_results[sid]:
            scores[sid] = 0.0
            continue

        # Stub tests passed
        if integration_passed:
            # Full success: stub passed + integration passed = full credit
            scores[sid] = subtask.complexity_weight
        else:
            # Stub passed but integration failed
            # Miner did their job, composition failed
            # Partial credit: 50% of complexity weight
            scores[sid] = subtask.complexity_weight * 0.5

    return scores
```

**Why this scoring model works:**

1. Miners focus on passing their stub tests, which is the thing they can control.
2. Miners are not fully penalized when another miner on their task produces bad code.
3. The partial credit on integration failure (50%) still creates incentive for miners to write composable code, because full credit requires integration to pass.
4. The scoring is deterministic: any validator re-running the same tests on the same patches will get the same scores.
5. No leave-one-out attribution needed, which eliminates the cascading failure edge cases where removing patch A makes patch B fail in a different way.

**Why the merger doesn't fix things:**

The merger does NOT attempt to resolve conflicts, fix broken code, or adjust implementations. If a patch doesn't apply cleanly, score zero. If stub tests fail, score zero. The merger applies, tests, and scores.

This is intentional. If the merger could fix things, miners would have incentive to submit sloppy code knowing it'll get cleaned up. By making the merger simple, all quality pressure flows to the right places: the coordinator (for contract quality) and the miners (for implementation quality).

### 3.5 Validator: Weight Setting

Validators maintain a running score per miner UID based on their performance across recent tasks. At the end of each tempo, the validator converts these scores into a weight vector and submits it on-chain.

```python
SCORING_WINDOW = 20  # number of recent tasks to consider

def compute_weights(task_history, all_uids):
    scores = {}

    for uid in all_uids:
        recent = [t for t in task_history if uid in t.scores][-SCORING_WINDOW:]
        if not recent:
            scores[uid] = 0.0
            continue

        total_earned = sum(t.scores[uid] for t in recent)
        total_possible = sum(t.subtask_weights[uid] for t in recent)
        scores[uid] = total_earned / total_possible if total_possible > 0 else 0.0

    # Normalize to [0, 1]
    max_score = max(scores.values()) if scores else 1.0
    weights = {uid: s / max_score for uid, s in scores.items()}

    return weights
```

Validators who don't orchestrate tasks can still set weights by independently verifying other validators' completed tasks: request the merged repo and test harness via VerificationSynapse, re-run the tests, corroborate the scores. This creates active validators (orchestrate tasks) and passive validators (verify and corroborate).

---

## 4. Concurrency Model

### Multiple Validators, Multiple Tasks

Each validator independently claims tasks from the queue and orchestrates them. With V active validators, up to V tasks can be in flight simultaneously.

### Miner Assignment

When a validator decomposes a task into N subtasks, it needs N available miners. The validator selects miners based on:
- Recent performance (prefer miners with higher historical scores)
- Availability (miners currently working on another subtask are busy)
- Diversity (avoid assigning all subtasks to miners from the same coldkey)

A miner can only work on one subtask at a time. With 192 miner slots and an average of 4 subtasks per task, the theoretical maximum is ~48 concurrent tasks.

### Miner Availability Protocol

Miners signal availability through a lightweight heartbeat on their Axon. Before assigning a subtask, the validator pings the miner with a StatusSynapse. If busy, the validator picks a different miner.

### Timeout and Reassignment

Each subtask has a deadline (default 600 seconds). If a miner doesn't respond by the deadline:

1. The validator marks the miner as timed out (score impact)
2. The subtask is reassigned to a different available miner
3. All other miners' completed patches are cached, only the missing piece is rebuilt
4. The replacement miner receives the same scaffolded repo and assignment

---

## 5. Synapse Protocol Definitions

### TaskAssignmentSynapse

Sent from validator to miner to assign a subtask. The repo_bundle contains the repository WITH the scaffolding commit already applied, so the miner receives a repo that already has all stub files, shared files, and test files in place.

```python
import bittensor as bt
from pydantic import Field

class TaskAssignmentSynapse(bt.Synapse):
    """Validator -> Miner: Assign a subtask."""

    # Request fields (sent by validator)
    task_id: str = ""
    subtask_id: str = ""
    repo_bundle: str = ""           # base64 encoded scaffolded repo (git bundle)
    subtask_description: str = ""   # natural language description
    allowed_files: list[str] = Field(default_factory=list)  # files miner may modify
    stub_test_files: list[str] = Field(default_factory=list)  # test files to pass
    timeout_seconds: int = 600

    # Response fields (filled by miner)
    patch: str = ""                 # unified diff of changes to allowed_files
    stub_tests_passed: bool = False
    stub_test_output: str = ""      # pytest stdout/stderr
    files_modified: list[str] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    error_message: str = ""
```

Note: the miner no longer needs separate `interface_contracts`, `shared_schemas`, or `read_only_context` fields. All of that information is IN the scaffolded repo. The stub files contain the contracts (function signatures + docstrings). The shared files contain the schemas. The existing repo files provide the context. The Synapse is simpler because the scaffolding commit carries the information.

### StatusSynapse

Lightweight availability check.

```python
class StatusSynapse(bt.Synapse):
    """Validator -> Miner: Check availability."""
    available: bool = False
    current_task_id: str = ""
```

### VerificationSynapse

Used by passive validators to verify active validators' results.

```python
class VerificationSynapse(bt.Synapse):
    """Validator -> Validator: Request completed task for verification."""
    task_id: str = ""

    # Response
    merged_repo_bundle: str = ""    # base64 encoded merged result
    test_harness: str = ""          # integration test file content
    miner_scores: dict[str, float] = Field(default_factory=dict)
    decomposition_manifest: str = ""  # JSON of the subtask manifest
```

---

## 6. Scoring and Incentive Mechanism

### Primary Signal: Stub Test Pass/Fail

Each miner is scored primarily on whether their stub tests pass. This is deterministic, objective, and within the miner's control. Stub tests only depend on the miner's code and the shared files, not on any other miner's work.

### Secondary Signal: Integration Test Pass/Fail

Integration tests determine whether passing miners get full credit (1.0x complexity weight) or partial credit (0.5x complexity weight). This creates a bonus for writing code that composes well, not just code that satisfies its own tests.

### Tertiary Signal: Code Quality (Optional)

A frontier model can score code quality for tie-breaking when multiple miners have equivalent test records. This is disabled by default and only relevant once the network has enough miners that tie-breaking matters.

### Scoring Window

Miners are scored over a rolling window of 20 tasks. A single failed task doesn't destroy a miner's weight if they have a strong track record.

### Anti-Gaming Measures

**Empty submissions**: Miners who submit empty patches or trivially simple code that doesn't break the build still score zero because their stub tests will fail. The stubs enforce that each function actually does what the docstring specifies.

**Validator collusion**: Passive validators independently verify completed tasks. If an active validator inflates scores, other validators' verification results will disagree and Yuma Consensus will down-weight the colluding validator.

**Free-riding on easy subtasks**: Complexity weights mitigate this, but the coordinator must honestly assess difficulty. This is a tuning problem for the coordinator prompt.

**Miner-miner collusion**: Miners can't see each other's work during execution. External coordination is possible but the sandboxing and file-scope restrictions limit the benefit.

---

## 7. Task Lifecycle

### Phase 1: Submission
A requester submits: repo URL + ref, feature spec, optional test hints, optional subtask budget.

### Phase 2: Claim and Scaffolding
1. Validator claims the task
2. Clones the repo at the specified ref
3. Runs the coordinator agent to produce the decomposition
4. Validates the decomposition programmatically (see Section 3.2)
5. Commits the scaffolding (shared files, stub files, test files) to the repo
6. Verifies that stub tests fail on the scaffolding (confirming stubs are real)
7. If validation fails, retries coordinator with error context or releases the task

### Phase 3: Assignment
1. Queries the metagraph for active miner UIDs
2. Pings miners with StatusSynapse to check availability
3. Selects N miners (one per subtask) based on availability and historical performance
4. Sends TaskAssignmentSynapse to each miner with the scaffolded repo bundle
5. Starts deadline timers

### Phase 4: Execution
1. Miner unpacks the scaffolded repo
2. Reads their assigned stub files (function signatures, docstrings, type hints)
3. Reads shared files and context files to understand the codebase
4. Implements the stubs (replaces NotImplementedError with real code)
5. Runs stub tests, iterates on failures (up to max retry count)
6. Generates git diff of allowed files, returns via Synapse

### Phase 5: Merge and Score
1. Collects all responses (marks timed-out miners)
2. Validates patches (file scope, clean apply)
3. Applies patches in dependency order
4. Runs stub tests per miner (primary scoring)
5. Runs integration tests (secondary scoring)
6. Computes per-miner scores (Section 3.4)
7. Optionally reassigns failed subtasks to new miners

### Phase 6: Weight Update
At tempo end, aggregates scores across the scoring window and submits weights on-chain.

### Phase 7: Verification
Passive validators request completed task data, re-run tests, corroborate scores.

### Iteration
Tasks are stateless. Each completed task produces a merged PR. The requester merges it into main. The next task starts from the updated main branch. No session state to manage, no accumulated context debt. The coordinator reads the repo as it currently exists and decomposes the new task around what's already there.

The coordinator also acts as a router: if a task is a simple bug fix in one file, it assigns it to a single miner with no decomposition. Not every task needs parallelization. The coordinator decides.

---

## 8. Architectural Patterns (Adopted and Rejected)

Patterns evaluated from Claude Code, claw-code, and Oh-My-Codex (OmX) harness architectures.

6. Architectural Recommendations

### Patterns to ADOPT

#### 6.1 Adopt: Urgency Markers in System Prompts (from Claude Code)

Claude Code uses `CRITICAL:`, `IMPORTANT:`, `Note:` hierarchy to signal instruction priority. The miner prompt above already uses this. The coordinator prompt should too.

**Implementation**: Standardize across all BitSwarm prompts. `CRITICAL` = violation causes score zero. `IMPORTANT` = strong guidance. `Note` = helpful context.

#### 6.2 Adopt: Negative Example Teaching (from Claude Code + OmX)

Both systems pair "do this" with explicit "don't do this" plus specific failure modes. The OmX critic role is especially good at this — "Vague rejections: 'The plan needs more detail.' Instead: 'Task 3 references auth.ts but doesn't specify which function...'"

**Implementation**: The coordinator prompt's "Anti-Patterns" section uses this. Add more failure mode examples as you observe them in production. Maintain a living document of observed miner failure modes and add the top ones to the miner prompt.

#### 6.3 Adopt: Tool Preference Hierarchy (from Claude Code)

Claude Code explicitly says "Use Read instead of cat, head, tail." This prevents the model from using bash for things a dedicated tool handles better.

**Implementation**: Already in the miner prompt. Reinforce in tool descriptions: file_read description explicitly mentions it's preferred over `cat`.

#### 6.4 Adopt: Pre-Read Requirement (from Claude Code)

Claude Code's FileEditTool errors if you haven't read the file first: "This tool will error if you attempt an edit without reading." This enforced-ordering pattern prevents blind writes.

**Implementation**: The `file_write` validator should track which files have been read via `file_read` in the current session. Warn (not error) if writing without reading. The miner prompt already instructs "Read before you write."

#### 6.5 Adopt: The Verification Loop Principle (from OmX)

OmX's most effective pattern: "No evidence = not complete." Every role requires tool-backed evidence of completion.

**Implementation**: For miners, this is natural — stub tests are the evidence. But add to the miner prompt: "Do not stop after writing code. You must run the tests. Claiming you're done without test output is not acceptable."

#### 6.6 Adopt: The Scope Guard Pattern (from OmX)

"Prefer the smallest viable diff. Do not broaden scope unless correctness requires it."

**Implementation**: Critical for miners. The miner prompt already enforces file scope. Additionally add: "If you think the stub signature is wrong or the docstring is incomplete, implement your best interpretation. Do not try to fix the scaffolding."

#### 6.7 Adopt: Structured Error Feedback with Hints (from claw-code + OmX)

Claw-code's turn-tagged prompts (`[turn N]`) and OmX's approach-log pattern both help the agent understand where it is in a retry loop and what was already tried.

**Implementation**: The error recovery protocol above implements this. Test failure output is formatted with iteration numbers, error type hints, and a history of previous attempts.

#### 6.8 Adopt: AST Validation of Outputs (from claw-code)

Claw-code validates structured output with retry. The coordinator should validate ALL generated code through `ast.parse()` before committing.

**Implementation**: Already in the architecture (Section 3.2 Coordinator Validation). Add: if ast.parse fails on any file, retry the coordinator with the specific parse error. Also add to `file_write` validation (already implemented above).

#### 6.9 Adopt: Computed System Prompts (from claw-code + Claude Code)

Both systems build prompts dynamically from runtime state. The miner prompt should include the actual file list, actual test file names, and actual shared file contents — not generic instructions.

**Implementation**: The user message sent to the miner (not the system prompt) should be dynamically assembled using the warm-start pattern (Idea 2). It includes:
- An annotated project file tree showing the full repo structure with markers for "YOUR ASSIGNMENT", "YOUR TESTS", "[shared - do not modify]", and "[assigned to another engineer]"
- Pre-loaded stub file contents, test file contents, and shared schema contents
- The specific subtask metadata (allowed files, test commands)

This eliminates the miner's first 3-5 file_read tool calls and prevents import path guessing. See Idea 2 for the full implementation.

#### 6.10 Adopt: The Same-Error-3x Rule (from OmX)

OmX stops after 3 identical failed approaches. This prevents burning tokens on a loop that's not converging.

**Implementation**: Already in the error recovery protocol. `MAX_CONSECUTIVE_SAME_ERROR = 3`. When triggered, the miner stops and returns its best partial result.

#### 6.11 Adopt: Adversarial Verification Agent (from Claude Code, adapted)

Claude Code's Verification Agent (Section 4.9) is deliberately adversarial — it tries to break things at the seams. It documents its own failure modes and requires tool-backed proof for every claim. The coordinator writes integration tests, but may write shallow ones. An adversarial agent that probes the component boundaries catches what the tests missed.

**Implementation**: Advisory only — does NOT affect miner scores (that would break deterministic scoring / Yuma Consensus). Runs post-merge, produces a quality report, and feeds patterns back into the coordinator prompt for the next task. This creates a learning loop: shallow test coverage on task N gets fixed in the coordinator's prompt for task N+1. See Idea 9 for the full implementation including the verifier prompt, seam identification logic, and coordinator feedback injection.

**When to build**: After the prototype is stable with 10+ completed tasks. Not a launch blocker.

---

### Patterns to NOT Adopt

#### 6.12 Reject: Shared Memory Between Agents (from claw-code + OmX)

Both systems propose shared memory stores, blackboard patterns, and event buses for inter-agent communication. OmX has mailboxes, discovery boards, and shared scratchpads.

**Why not for BitSwarm**: Miners are adversarial and isolated by design. Shared memory between miners would enable collusion, free-riding, and gaming. The scaffolding commit IS the shared memory — it's frozen before miners start and provides all the shared context they need.

#### 6.13 Reject: Bidirectional Communication During Execution (from claw-code)

Claw-code's `SendMessageTool` enables multi-turn subagent conversations. OmX's leader can send mailbox messages to workers mid-execution.

**Why not for BitSwarm**: The validator cannot adjust tasks mid-execution. This is intentional — it makes scoring deterministic and prevents a validator from helping favored miners. The miner must be self-sufficient from the assignment alone.

#### 6.14 Reject: Work Stealing (from Claude Code improvements)

Claude Code proposes that agents finishing early pick up work from overloaded siblings.

**Why not for BitSwarm**: Miners don't know about each other's work. A miner that finishes early simply returns early. In production, a miner could accept a new subtask from a different task — but never from the same task (to prevent information leakage).

#### 6.15 Reject: Dynamic Tool Discovery (from Claude Code's ToolSearch)

Claude Code defers loading tools and uses a ToolSearch mechanism for on-demand discovery.

**Why not for BitSwarm**: Miners have exactly 3-4 tools (file_read, file_write, bash, and optionally list_files). There's no decision overhead. Including all tools in the initial prompt is cheaper than adding a meta-tool. Tool simplicity is a feature, not a limitation.

#### 6.16 Reject: Advisory-Only Enforcement (from OmX)

OmX acknowledges "the model may skip the gate." It relies on prompt instructions for scope enforcement.

**Why not for BitSwarm**: Scope enforcement must be mechanical, not advisory. The `file_write` validator REJECTS writes to files outside `allowed_files`. The merger REJECTS patches touching out-of-scope files. No reliance on the model obeying instructions.

#### 6.17 Reject: Continuous Integration During Execution (from OmX improvements)

OmX proposes cherry-picking completed worker commits into the main branch while other workers are still running.

**Why not for BitSwarm**: Miners work on non-overlapping files from a frozen scaffolding. The merge step happens AFTER all miners complete. Continuous integration during execution would require miners to pull updates, which breaks isolation.

#### 6.18 Reject: The Deslop/Cleanup Pass (from OmX)

OmX runs a mandatory code cleanup pass after implementation.

**Why not for BitSwarm**: If a miner's code passes the stub tests, it passes. Code quality scoring is optional and disabled by default. Running a cleanup pass on miner output would introduce non-determinism into scoring. If quality matters, add it as a tertiary scoring signal, not as a post-processing step.

---

---

## 9. Additional Optimizations

### Additional Ideas and Recommendations

**Build Priority Order** (ship in this sequence):

1. **Coordinator Self-Verification Loop** (Idea 1) — if scaffolding is broken, nothing works
2. **Scaffolding Quality Metrics** (Idea 6) — you need to know your decomposition success rate immediately because that tells you whether the whole system is viable. If your coordinator produces broken scaffolding 70% of the time, the warm-start optimization doesn't matter yet.
3. **Miner Warm-Start Context Block** (Idea 2) — biggest easy win for miner performance once scaffolding is reliable
4. **Fallback Partial Credit** (Idea 5) — changes incentives to improve network participation

---

#### Idea 1: Coordinator Self-Verification Loop

Before accepting the decomposition, the validator runs programmatic verification and retries the coordinator with specific errors on failure. Budget 2-3 coordinator retries per task.

```python
"""
Coordinator self-verification and retry protocol.
The coordinator's error-feedback-retry loop is just as important
as the miner's. Scaffolding breaks will break EVERY miner downstream.
"""

MAX_COORDINATOR_RETRIES = 3

def verify_and_retry_coordinator(
    coordinator_fn,  # callable that returns decomposition JSON
    repo_root: str,
    feature_spec: str,
    test_hints: str | None,
) -> dict | None:
    errors_from_previous = []

    for attempt in range(1, MAX_COORDINATOR_RETRIES + 1):
        # Call coordinator (with any previous errors appended)
        decomposition = coordinator_fn(
            repo_root=repo_root,
            feature_spec=feature_spec,
            test_hints=test_hints,
            previous_errors=errors_from_previous,
        )

        errors = []

        # Check 1: All files parse as valid Python
        for path, content in {
            **decomposition["shared_files"],
            **decomposition["stub_files"],
            **decomposition["stub_test_files"],
            **decomposition.get("integration_test_files", {}),
        }.items():
            try:
                ast.parse(content)
            except SyntaxError as e:
                errors.append(f"SyntaxError in {path} line {e.lineno}: {e.text}")

        # Check 2: All imports in stubs resolve to existing/shared/stdlib
        for path, content in decomposition["stub_files"].items():
            for module in extract_imports(content):
                if not resolves(module, repo_root, decomposition["shared_files"]):
                    errors.append(
                        f"Unresolved import in {path}: '{module}' — "
                        f"define it in a shared file or fix the import path"
                    )

        # Check 3: All imports in TEST files also resolve
        for path, content in decomposition["stub_test_files"].items():
            for module in extract_imports(content):
                if not resolves(module, repo_root, decomposition["shared_files"]):
                    errors.append(
                        f"Unresolved import in test {path}: '{module}' — "
                        f"tests import types too; ensure they exist in shared files"
                    )

        # Check 4: All types referenced in test assertions exist with correct fields
        for path, content in decomposition["stub_test_files"].items():
            type_issues = check_type_field_consistency(
                content, decomposition["shared_files"]
            )
            errors.extend(type_issues)

        # Check 5: No file path overlaps between subtasks
        all_paths = []
        for st in decomposition["subtasks"]:
            for f in st["stub_files"]:
                if f in all_paths:
                    errors.append(f"File path overlap: {f} assigned to multiple subtasks")
                all_paths.append(f)

        # Check 6: Complexity weights sum to 1.0
        total_weight = sum(st["complexity_weight"] for st in decomposition["subtasks"])
        if abs(total_weight - 1.0) > 0.01:
            errors.append(f"Complexity weights sum to {total_weight}, expected 1.0")

        # Check 7: Write files to disk and run stub tests — verify they FAIL
        # (confirming stubs actually raise NotImplementedError)
        write_scaffolding_to_disk(decomposition, repo_root)
        for st in decomposition["subtasks"]:
            for test_file in st["stub_test_files"]:
                result = run_pytest(test_file, repo_root)
                if result.returncode == 0:
                    errors.append(
                        f"Stub test {test_file} PASSED on scaffolding — "
                        f"tests should FAIL on NotImplementedError stubs. "
                        f"The test is probably a no-op or doesn't call the stub."
                    )

        if not errors:
            return decomposition  # Validation passed

        # Feed errors back for retry
        errors_from_previous = errors
        # Log for metrics
        log_coordinator_retry(attempt, errors)

    # All retries exhausted
    return None  # Release task back to queue
```

This is the "verification loop" pattern from OmX applied to the coordinator. "No evidence = not complete" applies to scaffolding too. The critical additions over naive validation: (a) test file imports are checked, not just stub imports — tests reference types too, and a mismatch between test assertions and shared file field names is the single most common coordinator bug; (b) stub tests are actually run against scaffolding to confirm they fail; (c) each retry gets the SPECIFIC errors, not a generic "try again."

#### Idea 2: Miner Warm-Start Context Block

Instead of making the miner read every file via tools (costing tool-call tokens), pre-load the most critical files directly into the user message. Also include an annotated project file tree so the miner knows where it sits in the codebase and can write correct imports without guessing.

```python
def build_annotated_file_tree(
    repo_root: str,
    subtask_allowed_files: list[str],
    subtask_test_files: list[str],
    shared_files: list[str],
    all_subtask_files: dict[str, list[str]],  # subtask_id -> files (for "other engineer" labels)
) -> str:
    """
    Build an annotated file tree that shows the miner exactly where
    everything is and what they can/cannot touch.
    """
    # Walk the repo and annotate each file
    lines = ["Project structure:"]
    for root, dirs, files in os.walk(repo_root):
        # skip noise
        dirs[:] = [d for d in sorted(dirs)
                    if not d.startswith(".") and d not in ("__pycache__", "venv", "node_modules")]
        depth = root.replace(repo_root, "").count(os.sep)
        indent = "│   " * depth
        rel_root = os.path.relpath(root, repo_root)
        if rel_root != ".":
            lines.append(f"{indent}├── {os.path.basename(root)}/")
        for f in sorted(files):
            if f.startswith("."):
                continue
            rel_path = os.path.relpath(os.path.join(root, f), repo_root)
            file_indent = "│   " * (depth + 1)
            # Annotate
            if rel_path in subtask_allowed_files:
                lines.append(f"{file_indent}├── {f}  ← YOUR ASSIGNMENT")
            elif rel_path in subtask_test_files:
                lines.append(f"{file_indent}├── {f}  ← YOUR TESTS")
            elif rel_path in shared_files:
                lines.append(f"{file_indent}├── {f}  [shared - do not modify]")
            elif any(rel_path in files for files in all_subtask_files.values()):
                lines.append(f"{file_indent}├── {f}  [assigned to another engineer]")
            else:
                lines.append(f"{file_indent}├── {f}")
    return "\n".join(lines)


user_message = f"""
Your assignment: {subtask_id}
Description: {subtask_description}
Files to implement: {allowed_files}
Tests to pass: {test_files}

{annotated_file_tree}

=== STUB FILE: {stub_path} ===
{stub_content}

=== TEST FILE: {test_path} ===
{test_content}

=== SHARED SCHEMAS: {schema_path} ===
{schema_content}

Start implementing. Run tests with: pytest {test_path} -v --tb=short
"""
```

The annotated file tree prevents a subtle failure mode: the miner invents an import path that doesn't exist. If it can see the tree, it knows `from models import User` is correct because `models.py` is right there in the listing. Without the tree it might write `from app.models import User` because that's common in training data but doesn't match this repo's structure.

The "assigned to another engineer" label reinforces that the miner shouldn't touch those files without revealing what the other miner is doing.

This eliminates 3-5 file_read tool calls at the start of every miner session. The miner can go straight to implementing. It saves ~30 seconds and ~5K tokens per subtask.

#### Idea 3: Difficulty-Calibrated Iteration Budgets

Not all subtasks need 5 iterations. The coordinator already assigns complexity_weight. Use it:

```python
if subtask.complexity_weight <= 0.15:
    max_iterations = 3  # Simple subtask, should pass quickly
elif subtask.complexity_weight <= 0.35:
    max_iterations = 5  # Standard
else:
    max_iterations = 7  # Complex subtask, give more room
```

#### Idea 4: Test-First Implementation Prompt Variant

An alternative miner prompt strategy: instead of "read stubs then implement," tell the miner "read the TESTS first, then implement whatever makes them pass." This is TDD-style and may produce more test-aligned implementations:

```
Read the test file FIRST. Understand exactly what each test expects.
Then read the stub file for the function signatures.
Then implement each function to make its tests pass.
```

Worth A/B testing against the standard "read stubs first" approach.

#### Idea 5: Fallback Partial Credit

When a miner's tests partially pass (e.g., 4/6 tests), the current scoring model gives zero. Consider partial credit:

```python
stub_score = tests_passed / total_tests  # 0.0 to 1.0
if stub_score >= 1.0:
    score = subtask.complexity_weight  # Full credit
elif stub_score >= 0.5:
    score = subtask.complexity_weight * 0.3  # Partial
else:
    score = 0.0  # Below threshold = zero
```

This changes incentives: a miner that implements 4/6 functions correctly gets some credit instead of nothing. This may improve network participation for harder subtasks.

#### Idea 6: Scaffolding Quality Metrics

Track coordinator quality over time:

```python
@dataclass
class ScaffoldingMetrics:
    ast_parse_pass_rate: float      # % of files that parse first try
    stub_test_fail_rate: float      # % of stub tests that correctly fail on stubs
    import_resolution_rate: float   # % of imports that resolve
    miner_success_rate: float       # % of miners that pass stub tests
    integration_pass_rate: float    # % of tasks where integration tests pass
    avg_miner_iterations: float     # How many retries miners need on average
```

If `miner_success_rate` drops below 60%, the coordinator prompt needs tuning. If `avg_miner_iterations` is consistently 1-2, the subtasks might be too easy (over-specified docstrings). If it's consistently 4-5, they might be too hard (under-specified).

#### Idea 7: Coordinator Model Routing

From OmX's tiered reasoning pattern: not every task needs the most expensive model.

```python
if task_complexity == "single_file_bugfix":
    # No decomposition needed. Assign to one miner directly.
    coordinator_model = None  # Skip coordinator entirely
elif task_complexity == "small_feature":  # 2-3 subtasks
    coordinator_model = "sonnet"
elif task_complexity == "medium_feature":  # 4-6 subtasks
    coordinator_model = "opus"
elif task_complexity == "large_feature":  # 6+ subtasks
    coordinator_model = "opus"
    # Consider breaking into multiple tasks instead
```

The architecture doc already mentions this ("the coordinator also acts as a router: if a task is a simple bug fix in one file, it assigns it to a single miner with no decomposition").

#### Idea 8: Stub Test Coverage Analysis

After the coordinator generates tests, run a coverage analysis:

```bash
pytest tests/test_stub.py --cov=path/to/stub --cov-report=term-missing
```

This won't show meaningful coverage (stubs raise NotImplementedError), but it WILL show which functions are exercised by tests. If a stub function has zero test coverage, the coordinator needs to add tests for it.

---

## 10. Post-Merge Quality: Adversarial Seam Verifier

Idea 9: Adversarial Seam Verifier (Post-Merge, Advisory Only)

The coordinator writes integration tests, but the coordinator might write shallow ones. After all patches merge and scoring completes, run an adversarial verification agent that tries to *break* the merged result at the component boundaries — the exact seams between subtasks.

This is adapted from Claude Code's Verification Agent pattern (Section 4.9 of the Claude analysis). That agent is deliberately adversarial, documents its own failure modes ("avoidance — checking easy things, ignoring hard ones" and "seduction by 80% — stopping when mostly works"), and requires tool-backed evidence for every claim.

**Why advisory-only, not a scoring signal:** If the verifier's judgment affects miner scores, it must be deterministic — two validators running the same verifier must agree. An LLM-based verifier is non-deterministic. Different validators would produce different scores, breaking Yuma Consensus. So: the verifier does NOT affect scores. It produces a quality report and feeds findings back into the coordinator.

```python
"""
Adversarial Seam Verifier

Runs AFTER scoring is complete. Does not affect miner scores.
Two outputs:
1. Quality report for the requester (what's fragile at the seams)
2. Findings fed back into coordinator prompt for the NEXT task

This creates a learning loop: bad integration test coverage on task N
gets fixed in the coordinator's prompt for task N+1.
"""

SEAM_VERIFIER_PROMPT = """
You are a verification specialist. Your job is to BREAK the merged codebase
at the boundaries between independently-implemented components.

You have received a merged codebase where different engineers independently
implemented different modules from the same interface specification. The
integration tests have already passed. Your job is to find what the
integration tests MISSED.

## Your Failure Modes (be aware of these)

1. Avoidance: Checking easy things (does it import correctly?) while
   ignoring hard things (what happens when the OAuth token is expired AND
   the session store is full AND the request has no cookies?)

2. Seduction by 80%: Finding that "most paths work" and stopping. You must
   test the adversarial paths — the ones a developer would forget.

## What to Probe

Focus on the SEAMS — the exact points where one component calls another:

- Type mismatches that pass at the happy path but fail at edges
  (None vs empty string, 0 vs False, empty list vs None)
- Error propagation: Component A raises ExceptionX, does Component B
  handle it or crash?
- Concurrency assumptions: Component A assumes sequential access,
  Component B calls it from async context
- State assumptions: Component A assumes the database is initialized,
  Component B doesn't guarantee initialization order
- Missing validation: Component A trusts Component B's output,
  Component B returns malformed data on edge cases

## Rules

- You MUST use tools to verify every claim. No speculation.
- You MUST run actual code or tests, not just read files.
- For each finding, produce: the exact failure scenario, the file
  and line where it breaks, and a concrete test that would catch it.
- "No evidence = not complete." If you can't produce a failing test
  for a claimed weakness, it's not a finding.

## Output Format

For each finding:
  SEAM: {component_a} -> {component_b}
  SCENARIO: {what triggers the failure}
  FILE: {path}:{line}
  SEVERITY: critical / moderate / minor
  TEST: {pytest code that reproduces the issue}

If no seam weaknesses found after thorough probing, output:
  RESULT: CLEAN — all seams verified with adversarial probes.
  PROBES_RUN: {list of scenarios tested}
"""


@dataclass
class SeamFinding:
    seam: str          # "google_client -> session_manager"
    scenario: str      # "Expired token with no refresh token"
    file: str          # "auth/google_client.py:42"
    severity: str      # "critical" | "moderate" | "minor"
    test_code: str     # Pytest code that reproduces it
    

@dataclass
class SeamVerifierReport:
    task_id: str
    findings: list[SeamFinding]
    probes_run: list[str]        # Scenarios attempted
    clean: bool                  # True if no findings
    

def run_seam_verifier(
    merged_repo_root: str,
    subtask_manifest: dict,
    model: str = "sonnet",  # Doesn't need frontier — adversarial probing is bounded
) -> SeamVerifierReport:
    """
    Run after scoring is complete. Does not block or affect scores.
    
    Steps:
    1. Identify seams from the subtask manifest (which subtasks share types,
       which import from each other's modules)
    2. Feed the merged code + seam map to the verifier agent
    3. Let the agent probe with read + bash tools (no writes)
    4. Collect structured findings
    5. Return report
    """
    # Identify seams from manifest
    seams = identify_seams(subtask_manifest)
    
    # Build verifier context
    context = build_seam_context(merged_repo_root, seams)
    
    # Run adversarial agent (read-only tools: file_read, bash for running tests)
    findings = run_verifier_agent(
        prompt=SEAM_VERIFIER_PROMPT,
        context=context,
        model=model,
        tools=["file_read", "bash"],  # Read-only. Cannot modify the merged result.
        max_iterations=10,
    )
    
    return SeamVerifierReport(
        task_id=subtask_manifest["task_id"],
        findings=findings,
        probes_run=[...],
        clean=len(findings) == 0,
    )


def identify_seams(manifest: dict) -> list[dict]:
    """
    Extract the component boundaries from the subtask manifest.
    A seam exists wherever:
    - Subtask A's stub file imports from a shared file that subtask B also imports from
    - Subtask A's read_only_context includes a file that subtask B writes to
    - Integration tests reference functions from multiple subtasks
    """
    seams = []
    subtasks = manifest["subtasks"]
    
    for i, st_a in enumerate(subtasks):
        for st_b in subtasks[i+1:]:
            # Check for shared type dependencies
            shared_overlap = (
                set(st_a.get("read_only_context", []))
                & set(st_b.get("read_only_context", []))
            )
            if shared_overlap:
                seams.append({
                    "from": st_a["subtask_id"],
                    "to": st_b["subtask_id"],
                    "shared_types": list(shared_overlap),
                    "description": (
                        f"{st_a['subtask_id']} and {st_b['subtask_id']} "
                        f"share dependencies on: {', '.join(shared_overlap)}"
                    ),
                })
    return seams


def feed_back_to_coordinator(
    report: SeamVerifierReport,
    coordinator_prompt_history: list[str],
) -> str:
    """
    Inject verifier findings into the coordinator's prompt for the NEXT task.
    
    This creates the learning loop:
    - Task N: coordinator writes shallow integration tests
    - Seam verifier finds edge cases the tests missed
    - Task N+1: coordinator prompt includes "In previous tasks, these
      integration failure patterns were found. Write tests that cover them."
    
    Only inject PATTERNS, not specific findings. The coordinator is writing
    tests for a NEW feature, not fixing old ones.
    """
    if report.clean:
        return ""
    
    # Extract patterns from findings
    patterns = set()
    for finding in report.findings:
        if finding.severity in ("critical", "moderate"):
            # Generalize: "expired token" -> "error propagation across component boundaries"
            patterns.add(generalize_finding(finding))
    
    if not patterns:
        return ""
    
    return (
        "IMPORTANT: In previous tasks, the following integration failure patterns "
        "were found AFTER integration tests passed, meaning the tests were too shallow. "
        "When writing integration tests for this task, explicitly test these patterns:\n"
        + "\n".join(f"- {p}" for p in sorted(patterns))
    )
```

**When to build this:** After the prototype is stable and you have 10+ completed tasks to analyze. The seam verifier's value compounds — each task's findings improve the coordinator's test generation for future tasks. It's a quality flywheel, not a launch blocker.

---

---

## 11. Repository Structure

```
bitswarm/
├── neurons/
│   ├── validator.py              # Main validator loop
│   └── miner.py                  # Main miner loop
├── bitswarm/
│   ├── __init__.py
│   ├── protocol.py               # Synapse definitions
│   ├── coordinator/
│   │   ├── __init__.py
│   │   ├── decomposer.py         # Calls frontier model, returns structured decomposition
│   │   ├── scaffolder.py         # Writes decomposition output as actual files to repo
│   │   ├── validator_checks.py   # Programmatic validation of decomposition
│   │   ├── prompts.py            # Coordinator system/user prompts
│   │   └── schemas.py            # Pydantic models for decomposition JSON
│   ├── miner/
│   │   ├── __init__.py
│   │   ├── runtime.py            # Sandboxed execution environment
│   │   ├── agent.py              # Reference agent: reads stubs, implements, runs tests
│   │   ├── prompts.py            # Miner agent system/user prompts
│   │   └── patch.py              # Git diff generation and scope validation
│   ├── validator/
│   │   ├── __init__.py
│   │   ├── task_queue.py         # Task queue management
│   │   ├── assignment.py         # Miner selection and subtask distribution
│   │   ├── merger.py             # Patch apply, stub test, integration test pipeline
│   │   ├── scorer.py             # Per-miner score computation
│   │   ├── verifier.py           # Cross-validator verification
│   │   └── weights.py            # Weight computation and on-chain submission
│   ├── sandbox/
│   │   ├── __init__.py
│   │   ├── docker.py             # Container lifecycle management
│   │   ├── git_ops.py            # Clone, branch, merge, diff, apply
│   │   └── test_runner.py        # Execute pytest inside sandbox
│   └── utils/
│       ├── __init__.py
│       ├── config.py             # Subnet hyperparameters
│       └── logging.py
├── tests/
│   ├── test_decomposer.py
│   ├── test_scaffolder.py
│   ├── test_merger.py
│   ├── test_scorer.py
│   └── test_protocol.py
├── scripts/
│   ├── submit_task.py            # CLI for submitting tasks
│   └── monitor.py                # Dashboard for task status
├── requirements.txt
├── Dockerfile.miner
├── Dockerfile.validator
├── docker-compose.yml
└── README.md
```

---

## 12. Prototype Plan

The prototype proves the core thesis on a single machine with no Bittensor integration.

### Scope
- One validator process (coordinator + merger)
- N miner processes (separate Python processes communicating via local HTTP)
- Single target repo: a minimal Flask app
- Single task: "Add Google OAuth login with session management, /me endpoint, /logout endpoint"
- Coordinator decomposes into ~4 subtasks
- Each miner uses Claude API via Anthropic SDK
- Validator merges patches and runs pytest

### Success Criteria
1. Coordinator produces valid scaffolding (all validation checks pass)
2. Stub tests fail on scaffolding (confirming stubs are real)
3. At least 3 of 4 miners pass their stub tests within 5 iterations
4. All patches apply cleanly (no git conflicts)
5. Integration tests pass on the merged codebase
6. The Flask app actually runs and the auth flow works (manual verification)

### Prototype to Production Path
1. Replace local HTTP with Bittensor Axon/Dendrite/Synapse
2. Add Docker sandboxing for miner execution
3. Implement weight-setting logic
4. Register on testnet, run with real miners
5. Iterate on coordinator prompts based on failure modes
6. Launch on mainnet

---

## 13. Open Research Questions

1. **Scaffolding reliability**: How often does the coordinator produce scaffolding where ALL stub files parse correctly, ALL imports resolve, and ALL stub tests actually fail (confirming they're real tests, not no-ops)? What are the common failure modes?

2. **Optimal subtask granularity**: Is 4 subtasks the right number for a typical feature? What's the success rate at 2 vs 4 vs 8 subtasks?

3. **Stub precision vs. over-specification**: If the stub docstring is too detailed, the miner is just translating English to Python. If it's too vague, the implementation won't satisfy integration tests. The prototype needs to find the right level of docstring specificity.

4. **Shared file completeness**: The coordinator must anticipate every type, schema, and constant that multiple subtasks will need, and provide complete implementations in shared files before miners start. If a miner needs a type that wasn't in the shared files, they can't create it without violating file scope. How often does this happen and how do we handle it?

5. **Test quality**: The coordinator generates both stub tests and integration tests. If the tests are too shallow (only test happy path), bad implementations pass. If they're too thorough (test every edge case), they over-constrain the implementation. What test coverage level produces the best miner success rate?

6. **Language generalization**: The scaffolding approach works well for Python with type hints. How well does it translate to JavaScript/TypeScript, Go, Rust, or dynamically typed Python without annotations?

7. **Requester spec quality**: Vague specs produce vague scaffolding. Should the coordinator have a "clarification" step where it asks the requester to refine the spec before decomposing? Or should it just do its best and let the success rate reflect the spec quality?

8. **Economic equilibrium**: If a subtask takes 5 minutes of Sonnet-class inference (~$0.50), the miner needs to earn more than $0.50 in emissions per subtask. What's the minimum task volume to sustain a healthy miner pool?

---

## 14. Differences from Claude Code / OpenClaw Subagent Architecture

| Dimension | Claude Code / OpenClaw | BitSwarm |
|---|---|---|
| Trust model | Shared trust, agents see each other's work | Adversarial, miners isolated from each other |
| Contract format | Natural language delegation | Executable code scaffolding (real stubs) |
| Coordination | Loose, orchestrator fixes mistakes live | Strict, scaffolding is frozen before execution |
| Context sharing | Shared context windows and memory | No shared context, only shared schema files |
| Error recovery | Orchestrator catches and corrects | Errors surface at test time, miner gets zero |
| File scope | Any agent can edit any file | Explicit per-miner file whitelist |
| Incentive | None, agents are cost centers | Economic, zero pay for bad output |
| Model choice | Fixed by operator | Each miner chooses independently |
| Cost structure | Operator pays all inference | Each miner pays their own inference |
| Quality pressure | Operator's attention | Market pressure (emissions proportional to quality) |
| Decomposition | Informal, adjustable on the fly | Formal scaffolding commit, frozen before miners start |
| Verification | Operator reviews output | Deterministic test suite, any validator can verify |

The key architectural insight: centralized multi-agent systems optimize for flexibility and graceful recovery because agents share trust. BitSwarm optimizes for contract precision and deterministic verification because agents don't share trust. The scaffolding commit is what makes this work: it converts informal coordination into formal, executable, testable contracts.

---

## 15. Configuration and Hyperparameters

| Parameter | Default | Description |
|---|---|---|
| `max_subtasks_per_task` | 6 | Maximum decomposition breadth |
| `subtask_timeout_seconds` | 600 | Per-subtask execution deadline |
| `task_timeout_seconds` | 1800 | Total task deadline including retries |
| `max_retries_per_subtask` | 1 | Reassignment attempts for failed subtasks |
| `max_miner_iterations` | 5 | Agent retry loops per miner before giving up |
| `scoring_window_tasks` | 20 | Rolling window for weight calculation |
| `partial_credit_ratio` | 0.5 | Credit multiplier when stubs pass but integration fails |
| `coordinator_model` | sonnet | Model for decomposition (use best available) |
| `quality_scoring_enabled` | false | Enable frontier model code quality scoring |
| `min_stub_tests_per_subtask` | 2 | Minimum stub tests per subtask |
| `patch_max_size_bytes` | 1048576 | Maximum patch size (1MB) |
| `sandbox_memory_limit` | 2g | Docker memory limit per miner sandbox |
| `sandbox_cpu_limit` | 2.0 | Docker CPU limit per miner sandbox |
| `sandbox_network` | none | Network access during code execution |
