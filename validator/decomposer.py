import json
import os
import subprocess

import anthropic

from config import ANTHROPIC_API_KEY, COORDINATOR_MODEL, MAX_COORDINATOR_RETRIES
from validator.prompts import COORDINATOR_SYSTEM_PROMPT


def get_file_tree(repo_path):
    """Get the file tree of the target repo."""
    result = subprocess.run(
        ["find", ".", "-type", "f", "-not", "-path", "./.git/*", "-not", "-path", "./__pycache__/*"],
        capture_output=True, text=True, cwd=repo_path,
    )
    return result.stdout.strip()


MAX_FILE_BYTES = 50_000
SKIP_EXTENSIONS = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                   ".zip", ".tar", ".gz", ".bin", ".exe", ".db", ".sqlite"}
SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", "node_modules", ".mypy_cache"}


def collect_repo_files(repo_path):
    """Walk the repo and return {relative_path: content} for all readable files."""
    files = {}
    for root, dirs, filenames in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            _, ext = os.path.splitext(fname)
            if ext in SKIP_EXTENSIONS:
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, repo_path)
            if os.path.getsize(full) > MAX_FILE_BYTES:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    files[rel] = f.read()
            except OSError:
                pass
    return files


def build_user_message(repo_path, feature_spec, previous_errors=None):
    """
    Phase 1 prompt: asks for the decomposition PLAN only.
    File contents are generated separately in Phase 2 so the model
    can focus on each task without the JSON growing too large.
    """
    file_tree = get_file_tree(repo_path)
    repo_files = collect_repo_files(repo_path)
    file_contents = ""
    for path, content in repo_files.items():
        file_contents += f"\n=== {path} ===\n{content}\n"

    message = f"""IMPORTANT: All repository context is pre-loaded below. You do NOT have tools available in this invocation. Do NOT attempt to use file_read, bash, or any other tools. Skip Step 1 of your instructions (tool exploration). The full repo is already here — proceed directly to decomposition.

## Target Repository

File tree:
{file_tree}
{file_contents}

## Feature Specification

{feature_spec}

## Task ID

bitswarm-prototype-001

## Output Requirements — READ CAREFULLY

Output a single JSON object. Start your response with the opening {{ brace. No prose before the JSON.

PHASE 1 — PLAN ONLY: For this call, output the decomposition structure: subtasks array, shared_files dict, requirements_additions list. You do NOT need to include stub_files, stub_test_files, or integration_test_files content — leave those as empty dicts {{}}.

The file contents will be generated in a separate Phase 2 call. Focus on getting the plan right:
- subtasks: complete array with subtask_id, description, stub_files list, stub_test_files list, dependencies, complexity_weight
- shared_files: dict of path → full content for any truly shared infrastructure (types, interfaces)
- stub_files: {{}}  ← leave empty, Phase 2 fills this
- stub_test_files: {{}}  ← leave empty, Phase 2 fills this
- integration_test_files: {{}}  ← leave empty, Phase 2 fills this
- requirements_additions: list of any new pip packages needed"""

    if previous_errors:
        error_block = "\n".join(f"- {e}" for e in previous_errors)
        message += f"""

## VALIDATION ERRORS FROM PREVIOUS ATTEMPT

Your previous decomposition failed validation with these errors:

{error_block}

Output only the corrected JSON, starting with {{"""

    return message


def build_file_generation_prompt(decomposition, repo_path, feature_spec):
    """
    Phase 2 prompt: given the validated plan, generate all stub file contents.
    This is a focused call — the model only needs to write Python files.
    """
    subtasks = decomposition.get("subtasks", [])
    shared_files = decomposition.get("shared_files", {})

    # Collect what files are needed
    stub_files_needed = []
    test_files_needed = []
    for st in subtasks:
        for f in st.get("stub_files", []):
            stub_files_needed.append((f, st))
        for f in st.get("stub_test_files", []):
            test_files_needed.append((f, st))

    # Build context: subtask descriptions
    subtask_context = ""
    for st in subtasks:
        subtask_context += f"\n### Subtask: {st['subtask_id']}\n"
        subtask_context += f"Description: {st.get('description', '')}\n"
        subtask_context += f"Stub files: {st.get('stub_files', [])}\n"
        subtask_context += f"Test files: {st.get('stub_test_files', [])}\n"
        subtask_context += f"Dependencies on other subtasks: {st.get('dependencies', [])}\n"

    # Build shared files context
    shared_context = ""
    for path, content in shared_files.items():
        shared_context += f"\n=== SHARED: {path} ===\n{content}\n"

    # List integration test files needed
    integration_files = list(decomposition.get("integration_test_files", {}).keys())
    if not integration_files:
        integration_files = ["tests/test_integration.py"]

    stub_list = "\n".join(f"  - {f} (for subtask '{st['subtask_id']}')" for f, st in stub_files_needed)
    test_list = "\n".join(f"  - {f} (for subtask '{st['subtask_id']}')" for f, st in test_files_needed)
    integ_list = "\n".join(f"  - {f}" for f in integration_files)

    # Include any JSON config/data files so tests use exact field names
    config_file_contents = ""
    for fname in os.listdir(repo_path):
        if fname.endswith(".json") and not fname.startswith("."):
            fpath = os.path.join(repo_path, fname)
            if os.path.isfile(fpath) and os.path.getsize(fpath) < MAX_FILE_BYTES:
                with open(fpath) as f:
                    config_file_contents += f"\n## {fname} (use these exact field names in test data)\n```json\n{f.read()}\n```\n"

    return f"""You are writing Python stub files for a parallel implementation project.

## Project: {feature_spec[:200]}...

{config_file_contents}

## Decomposition Plan

{subtask_context}

## Shared Files (already implemented — import from these)

{shared_context if shared_context else "(none)"}

## Files You Must Write

STUB FILES (each function/method body must raise NotImplementedError):
{stub_list}

TEST FILES (tests must FAIL on stubs because stubs raise NotImplementedError):
{test_list}

INTEGRATION TEST FILES (test cross-subtask interaction after all stubs implemented):
{integ_list}

## Rules

Stub files:
- Include all class definitions and function signatures with type hints
- Every function body: raise NotImplementedError(f"{{self.__class__.__name__}}.method_name not implemented")
- Include docstrings explaining what each function must do
- CRITICAL: Always use FULL package paths for imports. If files live inside a package
  directory (e.g. 'mypackage/'), use the full dotted path:
  CORRECT:  from mypackage.module import MyClass
  WRONG:    from module import MyClass     (bare name, will fail at runtime)
  WRONG:    import module                  (bare name, will fail at runtime)
  WRONG:    from .module import MyClass    (relative import, avoid)
- Import from shared files and standard library only (no cross-subtask imports)
- Ensure that objects which pass data between subtasks carry all required fields.
  If subtask A produces an object that subtask B consumes, the shared type definition
  must include every field both subtasks need.

Test files:
- CRITICAL: Tests MUST FAIL when run against stubs. This verifies stubs are real.
- Import from the corresponding stub module (the subtask being tested)
- Each test calls a stub function and asserts something about the RETURN VALUE
- DO NOT use pytest.raises(NotImplementedError) — that makes the test PASS on stubs (wrong)
- DO NOT write tests that only import or check class existence — those PASS without calling anything
- CORRECT pattern: "result = vec.dot(other); assert result == 6.0"
  The stub raises NotImplementedError → pytest reports FAILED → validation passes
- WRONG pattern: "with pytest.raises(NotImplementedError): vec.dot(other)"
  This catches the error → test PASSES on stub → validation rejects it
- Each subtask must have at least 3 meaningful tests that call real functions and check results
- CRITICAL: If your tests need objects from OTHER subtasks as dependencies (e.g. a test
  that needs an object from another miner's module), use `unittest.mock.MagicMock()` instead
  of importing the real class. Other subtasks are stubs in isolated miner repos — importing them
  causes tests to fail before even testing YOUR stub. Example:
    from unittest.mock import MagicMock
    dependency_obj = MagicMock()  # don't import from another subtask's module
    my_obj = MyClass(dependency_obj)  # test YOUR stub
  This ensures tests fail due to YOUR stub raising NotImplementedError, not a dependency.

Integration test files:
- Test that implementations from different subtasks work together
- Import from all relevant modules
- Use @pytest.mark.xfail(raises=NotImplementedError, strict=False) on each test
  so integration tests are allowed to fail during the scaffold phase
- CRITICAL: always include explicit imports for EVERY module you use, including
  `import numpy as np` if you use np.anything, `from PIL import Image` if you
  use Image, etc. Never use a name without importing it first.
- CRITICAL: if the repo contains JSON config/data files, all inline test data MUST use
  the exact same field names as those files. Copy field names exactly — do not rename them.

## Output Format

Return ONLY a JSON object with this exact structure:
{{
  "stub_files": {{
    "path/to/file.py": "complete file content as string",
    ...
  }},
  "stub_test_files": {{
    "tests/test_xxx.py": "complete test file content as string",
    ...
  }},
  "integration_test_files": {{
    "tests/test_integration.py": "complete integration test file content as string"
  }}
}}

Write EVERY file listed above. Do not skip any. Start your response with the opening {{ brace."""


def stream_json(client, model, system, messages, label, debug_path=None, max_retries=3):
    """Stream a response and return the accumulated text. Retries on network errors."""
    import time

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            text = "{"
            with client.messages.stream(
                model=model,
                max_tokens=32000,
                system=system,
                messages=messages,
            ) as stream:
                for chunk in stream.text_stream:
                    text += chunk
                    if len(text) % 3000 < len(chunk):
                        print(".", end="", flush=True)
            print(f" ({len(text):,} chars)")

            if debug_path:
                os.makedirs(os.path.dirname(debug_path), exist_ok=True)
                with open(debug_path, "w") as f:
                    f.write(text)

            return text

        except Exception as exc:
            last_exc = exc
            err_str = str(exc)
            # Retry on transient network errors
            if any(kw in err_str for kw in ("RemoteProtocolError", "incomplete chunked",
                                             "ConnectionError", "timeout", "Timeout",
                                             "overloaded", "529", "500")):
                if attempt < max_retries:
                    wait = 5 * attempt
                    print(f"\n  [stream_json] Transient error (attempt {attempt}/{max_retries}): {exc}")
                    print(f"  Retrying in {wait}s...")
                    time.sleep(wait)
                    continue
            raise

    raise last_exc


def parse_json_response(text):
    """Extract and parse JSON from the model response, tolerating surrounding prose."""
    text = text.strip()

    # Direct parse (ideal case — model returned only JSON)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # JSON inside ```json ... ``` fences
    if "```json" in text:
        try:
            start = text.index("```json") + 7
            end = text.index("```", start)
            return json.loads(text[start:end].strip())
        except (ValueError, json.JSONDecodeError):
            pass

    # JSON inside plain ``` ... ``` fences
    if "```" in text:
        try:
            start = text.index("```") + 3
            end = text.index("```", start)
            candidate = text[start:end].strip()
            return json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            pass

    # Last resort: find the outermost { ... } block
    try:
        start = text.index("{")
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        pass

    raise ValueError("No JSON object found in response")


def call_coordinator(repo_path, feature_spec, previous_errors=None, debug_dir=None):
    """
    Two-phase coordinator call:
      Phase 1 — get decomposition plan (subtask structure, no file contents)
      Phase 2 — generate all stub file contents given the plan

    This split is necessary because the model reliably produces the plan
    but consistently truncates file contents when both are in one response.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Phase 1: decomposition plan ─────────────────────────────────────────
    print("  [Phase 1] Decomposition plan...", end="", flush=True)
    plan_message = build_user_message(repo_path, feature_spec, previous_errors)
    plan_debug = os.path.join(debug_dir, "phase1_plan.txt") if debug_dir else None

    plan_text = stream_json(
        client, COORDINATOR_MODEL,
        system=COORDINATOR_SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": plan_message},
            {"role": "assistant", "content": "{"},
        ],
        label="plan",
        debug_path=plan_debug,
    )

    try:
        decomposition = parse_json_response(plan_text)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Phase 1 JSON parse error: {e}")

    subtasks = decomposition.get("subtasks", [])
    if not subtasks:
        raise ValueError("Phase 1 returned no subtasks")

    # ── Phase 2: generate file contents ─────────────────────────────────────
    print("  [Phase 2] Generating stub files...", end="", flush=True)
    file_prompt = build_file_generation_prompt(decomposition, repo_path, feature_spec)
    files_debug = os.path.join(debug_dir, "phase2_files.txt") if debug_dir else None

    files_text = stream_json(
        client, COORDINATOR_MODEL,
        system="You are a Python code generator. Output only valid JSON. No prose. Start with {.",
        messages=[
            {"role": "user", "content": file_prompt},
            {"role": "assistant", "content": "{"},
        ],
        label="files",
        debug_path=files_debug,
    )

    try:
        file_contents = parse_json_response(files_text)
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"Phase 2 JSON parse error: {e}")

    # ── Merge Phase 1 plan + Phase 2 file contents ───────────────────────────
    decomposition["stub_files"] = file_contents.get("stub_files", {})
    decomposition["stub_test_files"] = file_contents.get("stub_test_files", {})
    decomposition["integration_test_files"] = file_contents.get("integration_test_files", {})

    return decomposition


def decompose(repo_path, feature_spec, validate_fn=None, debug_dir=None):
    """
    Run the coordinator decomposition with self-verification loop.

    validate_fn(decomposition, repo_path) -> list[str]  (empty = valid)
    debug_dir: if set, saves raw API responses there for inspection
    """
    previous_errors = []

    for attempt in range(1, MAX_COORDINATOR_RETRIES + 1):
        print(f"\n[Coordinator] Attempt {attempt}/{MAX_COORDINATOR_RETRIES}")

        attempt_debug_dir = None
        if debug_dir:
            attempt_debug_dir = os.path.join(debug_dir, f"coordinator_attempt_{attempt}")
            os.makedirs(attempt_debug_dir, exist_ok=True)

        try:
            decomposition = call_coordinator(
                repo_path, feature_spec,
                previous_errors=previous_errors if previous_errors else None,
                debug_dir=attempt_debug_dir,
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"\n[Coordinator] Error: {e}")
            previous_errors = [f"Your response was not valid JSON. Error: {e}"]
            continue

        if validate_fn is None:
            return decomposition

        errors = validate_fn(decomposition, repo_path)
        if not errors:
            print(f"[Coordinator] Validation passed on attempt {attempt}")
            return decomposition

        print(f"[Coordinator] Validation failed with {len(errors)} errors:")
        for err in errors:
            print(f"  - {err}")
        previous_errors = errors

    print("[Coordinator] All retries exhausted")
    return None
