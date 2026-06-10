import json
import os
import subprocess

import anthropic

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_BASE_URL,
    COORDINATOR_BACKEND,
    COORDINATOR_MODEL,
    MAX_COORDINATOR_RETRIES,
)
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


def build_user_message(repo_path, feature_spec, previous_errors=None,
                        language: str | None = None):
    """
    Phase 1 prompt: asks for the decomposition PLAN only.
    File contents are generated separately in Phase 2 so the model
    can focus on each task without the JSON growing too large.

    ``language`` selects the target language profile. When set, an
    explicit "language override" block is prepended so the model uses
    the correct file extensions and directory layout in the plan
    (otherwise the Python-heavy system prompt biases every plan to
    ``.py`` paths regardless of the actual target).
    """
    file_tree = get_file_tree(repo_path)
    repo_files = collect_repo_files(repo_path)
    file_contents = ""
    for path, content in repo_files.items():
        file_contents += f"\n=== {path} ===\n{content}\n"

    # Resolve the language profile (honors COORDINATOR_LANGUAGE env var
    # if ``language`` is None). Inject a strong override that the system
    # prompt's Python-heavy examples cannot drown out.
    from validator.lang_profiles import profile_for
    profile = profile_for(language=language, repo_path=repo_path)
    language_block = (
        "## CRITICAL LANGUAGE OVERRIDE -- READ FIRST\n\n"
        f"The target language for this project is **{profile.display_name}**.\n\n"
        "The system prompt and decomposition examples reference PYTHON\n"
        "conventions (.py file extensions, NotImplementedError, pytest,\n"
        "pip packages, package/__init__.py layout). IGNORE those examples\n"
        f"and use the corresponding {profile.display_name} idioms:\n\n"
        f"- Source file extensions: {', '.join(profile.extensions)}\n"
        f"- Default integration test filename: {profile.integration_test_filename}\n"
        f"- Miners verify with: {profile.test_command_hint}\n"
        "- For language-appropriate project layout, follow the conventions\n"
        f"  of {profile.display_name} (e.g. ``src/main/java/<pkg>/<Type>.java``\n"
        "  for Java, ``src/<module>.rs`` for Rust, ``wordle/words.ts`` for\n"
        "  TypeScript, ``wordle/words.cpp`` + ``wordle/words.hpp`` for C++,\n"
        f"  etc.). Use {profile.display_name}'s native module / namespace /\n"
        "  package directory shape.\n"
        '- ``requirements_additions`` should list dependencies in the\n'
        f"  format appropriate for {profile.display_name} (npm package\n"
        '  names for TypeScript, Maven coordinates for Java, Cargo crate\n'
        '  names for Rust, NuGet packages for C#, etc.), NOT pip packages,\n'
        f"  unless the target IS Python.\n\n"
        "EVERY ``stub_files`` and ``stub_test_files`` path in your plan\n"
        f"MUST use a {profile.display_name}-appropriate extension and\n"
        f"directory layout. The Phase 2 step will write the actual file\n"
        "contents using these exact paths -- if the paths are wrong, the\n"
        "downstream harvester drops everything and the run fails.\n\n"
        "---\n\n"
    )

    message = language_block + f"""IMPORTANT: All repository context is pre-loaded below. You do NOT have tools available in this invocation. Do NOT attempt to use file_read, bash, or any other tools. Skip Step 1 of your instructions (tool exploration). The full repo is already here -- proceed directly to decomposition.

## Target Repository

File tree:
{file_tree}
{file_contents}

## Feature Specification

{feature_spec}

## Task ID

bitswarm-prototype-001

## Output Requirements  -  READ CAREFULLY

Output a single JSON object. Start your response with the opening {{ brace. No prose before the JSON.

PHASE 1  -  PLAN ONLY: For this call, output the decomposition structure: subtasks array, shared_files dict, requirements_additions list. You do NOT need to include stub_files, stub_test_files, or integration_test_files content  -  leave those as empty dicts {{}}.

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


def build_integration_test_prompt(decomposition, repo_path, feature_spec,
                                    language: str | None = None) -> str:
    """Phase 1.5 prompt: write the integration tests FIRST.

    Test-first decomposition: the integration tests become the
    contract that Phase 2 stub generation must satisfy. Writing them
    here (with shared types in scope, before any stubs exist) keeps
    the test author from drifting away from the eventual stub
    signatures, since the same model can't see the stubs yet -- it has
    to commit to a single set of signatures and have Phase 2 follow.
    """
    from validator.lang_profiles import profile_for
    profile = profile_for(language=language, repo_path=repo_path)
    subtasks = decomposition.get("subtasks", []) or []
    shared_files = decomposition.get("shared_files", {}) or {}

    subtask_block = ""
    for st in subtasks:
        subtask_block += (
            f"\n### Subtask: {st['subtask_id']}\n"
            f"Description: {st.get('description', '')}\n"
            f"Stub files: {st.get('stub_files', [])}\n"
            f"Dependencies on other subtasks: {st.get('dependencies', [])}\n"
        )

    shared_block = ""
    for path, content in shared_files.items():
        shared_block += f"\n=== SHARED: {path} ===\n{content}\n"
    if not shared_block:
        shared_block = "(none)"

    integ_files = list(decomposition.get("integration_test_files", {}).keys())
    if not integ_files:
        integ_files = [profile.integration_test_filename]
    integ_list = "\n".join(f"  - {p}" for p in integ_files)

    return f"""{profile.phase2_intro.replace('stub files', 'INTEGRATION TESTS')}

Target language: {profile.display_name}.

## Project spec
{feature_spec[:1500]}

## Decomposition plan (subtasks that WILL be implemented)
{subtask_block}

## Shared files (already-defined types you can use)
{shared_block}

## Files you must write NOW
{integ_list}

## Why now (test-first decomposition)

You are writing these integration tests BEFORE any subtask stubs
exist. The tests are the contract. Phase 2 will generate stubs to
match the public API your tests reference. So whatever signatures
you write into the tests (constructor argument lists, method names,
return types) become the SOURCE OF TRUTH for the rest of the project.

Be deliberate. Use ONE consistent signature shape per type across
ALL test files. Don't construct ``Game(words, "hello")`` in one
function and ``Game("hello")`` in another -- pick one, document it,
stick to it.

## Rules

Integration tests:
{profile.integration_rules}

Tests must:
- Construct real objects with concrete arguments.
- Call public methods and assert on return values.
- Fail when run against stubs that throw "not implemented" (the
  expected pre-mining state). Do NOT wrap calls in try/catch.
- Cover at least the behaviours called out in the spec's
  "Integration test contract" section (if present).

## Output Format

Write each integration test file directly to disk using the Write
tool, at the path indicated above. After all integration test files
are written, stop. Do not print the contents to stdout.

Paths are relative to your current working directory.
"""


def build_file_generation_prompt(decomposition, repo_path, feature_spec,
                                  language: str | None = None):
    """
    Phase 2 prompt: given the validated plan, generate all stub file contents.

    ``language`` controls which ``LanguageProfile`` drives the
    intro / stub-body idiom / import conventions / test framework.
    When omitted, resolves via ``COORDINATOR_LANGUAGE`` env var or
    auto-detection from ``repo_path`` markers (Cargo.toml -> rust,
    package.json -> typescript, etc.), defaulting to Python.
    """
    from validator.lang_profiles import profile_for
    profile = profile_for(language=language, repo_path=repo_path)
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
        integration_files = [profile.integration_test_filename]

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

    # Test-first: if Phase 1.5 already produced integration tests,
    # show them inline as the contract Phase 2 must satisfy.
    pre_existing_integration = decomposition.get("integration_test_files", {}) or {}
    if pre_existing_integration:
        contract_block = (
            "\n## Integration tests (ALREADY WRITTEN -- this is the "
            "contract your stubs MUST satisfy)\n\n"
            "These tests were written BEFORE you wrote any stubs. They\n"
            "are the source of truth for every public type / function /\n"
            "method signature. Your stubs must declare exactly the\n"
            "interfaces these tests reference -- same names, same arg\n"
            "counts, same types.\n"
        )
        for path, content in pre_existing_integration.items():
            contract_block += f"\n### {path}\n```\n{content}\n```\n"
    else:
        contract_block = ""

    return f"""{profile.phase2_intro}

Target language: {profile.display_name}. Miners verify their work with:
    {profile.test_command_hint}

## Project: {feature_spec[:200]}...

{config_file_contents}

## Decomposition Plan

{subtask_context}

## Shared Files (already implemented -- import from these)

{shared_context if shared_context else "(none)"}
{contract_block}
## Files You Must Write

STUB FILES (every function/method body raises the language's "not
implemented" idiom -- see Rules below):
{stub_list}

TEST FILES (tests must FAIL when run against the stubs):
{test_list}

INTEGRATION TEST FILES (test cross-subtask interaction once stubs
are implemented):
{integ_list}

## Rules

Stub files:
{profile.stub_rules}

Test files:
{profile.test_rules}

Integration test files:
{profile.integration_rules}

## Output Format

Return ONLY a JSON object with this exact structure (paths use the
extensions appropriate to {profile.display_name}):
{{
  "stub_files": {{
    "<path-to-stub>": "complete file content as string",
    ...
  }},
  "stub_test_files": {{
    "<path-to-test>": "complete test file content as string",
    ...
  }},
  "integration_test_files": {{
    "{profile.integration_test_filename}": "complete integration test file content as string"
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

    # Direct parse (ideal case  -  model returned only JSON)
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


def call_coordinator(repo_path, feature_spec, previous_errors=None,
                      debug_dir=None, language: str | None = None,
                      mode: str = "scaffold"):
    """
    Two-phase coordinator call:
      Phase 1 - get decomposition plan (subtask structure, no file contents)
      Phase 2 - generate all stub file contents given the plan

    This split is necessary because the model reliably produces the plan
    but consistently truncates file contents when both are in one response.

    ``language`` selects the target language profile. Passed through to
    both Phase 1 (so the planned ``stub_files`` paths get the right
    extensions) and Phase 2 (so the file generator uses the matching
    language idioms). When ``None``, resolves via the
    ``COORDINATOR_LANGUAGE`` env var / repo auto-detect, defaulting to
    Python.

    ``mode`` is ``"scaffold"`` (default; scaffold new code from an
    empty/minimal repo) or ``"diff"`` (modify an existing codebase). Diff
    mode uses a separate set of prompts (``validator/diff_prompts.py``)
    and produces decompositions with the diff-mode shape (modify_files,
    target_stubs, new_test_files).
    """
    client = anthropic.Anthropic(
        api_key=ANTHROPIC_API_KEY,
        base_url=ANTHROPIC_BASE_URL,  # None = SDK default
    )

    from validator.lang_profiles import profile_for
    profile = profile_for(language=language, repo_path=repo_path)

    if mode == "diff":
        return _call_coordinator_diff(
            client, profile, repo_path, feature_spec,
            previous_errors, debug_dir,
        )

    # Scaffold mode (existing behavior).
    print("  [Phase 1] Decomposition plan...", end="", flush=True)
    plan_message = build_user_message(
        repo_path, feature_spec, previous_errors, language=profile.name,
    )
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

    # Phase 2: generate file contents
    print("  [Phase 2] Generating stub files...", end="", flush=True)
    file_prompt = build_file_generation_prompt(
        decomposition, repo_path, feature_spec, language=profile.name,
    )
    files_debug = os.path.join(debug_dir, "phase2_files.txt") if debug_dir else None

    files_text = stream_json(
        client, COORDINATOR_MODEL,
        system=(
            f"You are a {profile.display_name} code generator. "
            "Output only valid JSON. No prose. Start with {."
        ),
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


def _call_coordinator_diff(client, profile, repo_path, change_spec,
                            previous_errors, debug_dir):
    """SDK-backed diff-mode coordinator. Same two-phase split as scaffold
    mode but uses the diff-mode prompts and produces the diff-mode
    decomposition shape."""
    from validator.diff_prompts import (
        DIFF_COORDINATOR_SYSTEM_PROMPT,
        build_diff_phase1_prompt,
        build_diff_phase2_prompt,
    )

    print("  [Phase 1, diff] Modification plan...", end="", flush=True)
    plan_message = build_diff_phase1_prompt(
        repo_path, change_spec, previous_errors, language=profile.name,
    )
    plan_debug = os.path.join(debug_dir, "phase1_plan.txt") if debug_dir else None

    plan_text = stream_json(
        client, COORDINATOR_MODEL,
        system=DIFF_COORDINATOR_SYSTEM_PROMPT,
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
        raise ValueError(f"Phase 1 (diff) JSON parse error: {e}")

    if decomposition.get("mode") != "diff":
        # The coordinator should set mode=diff; if it didn't, force it.
        decomposition["mode"] = "diff"

    subtasks = decomposition.get("subtasks", []) or []
    if not subtasks:
        raise ValueError("Phase 1 (diff) returned no subtasks")
    print(f" {len(subtasks)} modification subtask(s) planned", flush=True)

    print("  [Phase 2, diff] Generating target stubs + new tests...", end="", flush=True)
    file_prompt = build_diff_phase2_prompt(
        decomposition, repo_path, change_spec, language=profile.name,
    )
    files_debug = os.path.join(debug_dir, "phase2_files.txt") if debug_dir else None

    files_text = stream_json(
        client, COORDINATOR_MODEL,
        system=(
            f"You are a {profile.display_name} code generator producing "
            "target-state stubs for an existing codebase. Output only valid "
            "JSON. No prose. Start with {."
        ),
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
        raise ValueError(f"Phase 2 (diff) JSON parse error: {e}")

    decomposition["target_stubs"] = file_contents.get("target_stubs", {}) or {}
    decomposition["new_test_files"] = file_contents.get("new_test_files", {}) or {}
    decomposition["integration_test_files"] = (
        file_contents.get("integration_test_files", {}) or {}
    )
    if file_contents.get("shared_additions"):
        # Merge Phase 2 shared additions into whatever Phase 1 already had.
        existing = decomposition.get("shared_additions", {}) or {}
        existing.update(file_contents["shared_additions"])
        decomposition["shared_additions"] = existing

    return decomposition


def _select_call_coordinator():
    """Resolve ``COORDINATOR_BACKEND`` to a ``call_coordinator`` callable.

    Lazy import so the SDK path doesn't drag in subprocess code and
    vice versa.
    """
    if COORDINATOR_BACKEND == "claude_code":
        from validator.decomposer_cc import call_coordinator as _impl
        print("[Coordinator] backend=claude_code (subprocess, no API spend)")
        return _impl
    if COORDINATOR_BACKEND in ("", "sdk", "anthropic"):
        return call_coordinator
    raise RuntimeError(
        f"Unknown COORDINATOR_BACKEND={COORDINATOR_BACKEND!r}. "
        f"Set to 'sdk' (default) or 'claude_code'."
    )


def decompose(repo_path, feature_spec, validate_fn=None, debug_dir=None,
               mode: str = "scaffold"):
    """
    Run the coordinator decomposition with self-verification loop.

    validate_fn(decomposition, repo_path) -> list[str]  (empty = valid)
        When None and mode='diff', defaults to
        validator.diff_validator.validate_diff_decomposition.
        When None and mode='scaffold', no automatic validation runs
        (caller's responsibility).
    debug_dir: if set, saves raw API responses there for inspection
    mode: 'scaffold' (default) or 'diff'
    """
    previous_errors = []
    call_coord = _select_call_coordinator()

    # Diff mode defaults to using the diff-mode structural validator.
    if mode == "diff" and validate_fn is None:
        from validator.diff_validator import validate_diff_decomposition
        validate_fn = validate_diff_decomposition

    # Cache lookup: hash (spec, repo, language, model, backend, mode)
    # and try to short-circuit. Mode is part of the key so a scaffold
    # decomposition and a diff decomposition of the same repo+spec
    # cache separately.
    from validator import cache as _cache
    from validator.lang_profiles import profile_for as _profile_for
    _profile = _profile_for(repo_path=repo_path)
    _backend = COORDINATOR_BACKEND
    _cache_key = _cache.compute_key(
        feature_spec=feature_spec,
        repo_path=repo_path,
        language=_profile.name,
        model=COORDINATOR_MODEL,
        backend=f"{_backend}:{mode}",
    )
    _cached = _cache.load(_cache_key)
    if _cached is not None:
        if validate_fn is None:
            print(f"[Coordinator] cache hit ({_cache_key[:12]}) -- reusing")
            return _cached
        cached_errors = validate_fn(_cached, repo_path)
        if not cached_errors:
            print(f"[Coordinator] cache hit ({_cache_key[:12]}) -- reusing")
            return _cached
        print(f"[Coordinator] cache hit ({_cache_key[:12]}) but stale "
              f"({len(cached_errors)} validation errors), regenerating")

    for attempt in range(1, MAX_COORDINATOR_RETRIES + 1):
        print(f"\n[Coordinator] Attempt {attempt}/{MAX_COORDINATOR_RETRIES}")

        attempt_debug_dir = None
        if debug_dir:
            attempt_debug_dir = os.path.join(debug_dir, f"coordinator_attempt_{attempt}")
            os.makedirs(attempt_debug_dir, exist_ok=True)

        try:
            decomposition = call_coord(
                repo_path, feature_spec,
                previous_errors=previous_errors if previous_errors else None,
                debug_dir=attempt_debug_dir,
                language=_profile.name,
                mode=mode,
            )
        except (json.JSONDecodeError, ValueError) as e:
            print(f"\n[Coordinator] Error: {e}")
            previous_errors = [f"Your response was not valid JSON. Error: {e}"]
            continue

        # Self-critique pass (cheap relative to mining). Catches
        # cross-file interface drift that Phase 1.5 can't reach for
        # non-Python languages.
        #
        # Advisory only: critique findings are LOGGED and stashed on
        # the decomposition dict for downstream visibility, but they
        # do NOT trigger a retry on their own. A full re-decomposition
        # costs 1-3 minutes of claude work and usually loses good
        # stubs; that's the wrong trade for paper-cut issues like
        # "pom.xml missing mockito-core". When ``validate_fn`` flags
        # real errors and we're retrying anyway, the critique items
        # are appended to the retry feedback so the next attempt sees
        # them too.
        from validator.critique import critique as _critique
        critique_issues = _critique(decomposition)
        if critique_issues:
            print(f"[Coordinator] critique flagged {len(critique_issues)} issue(s) "
                  "(advisory; not triggering a retry):")
            for issue in critique_issues:
                print(f"  ! {issue}")
            decomposition["_critique_notes"] = list(critique_issues)

        if validate_fn is None:
            saved = _cache.save(_cache_key, decomposition)
            if saved:
                print(f"[Coordinator] cached at {saved}")
            return decomposition

        errors = validate_fn(decomposition, repo_path)
        if not errors:
            print(f"[Coordinator] Validation passed on attempt {attempt}")
            saved = _cache.save(_cache_key, decomposition)
            if saved:
                print(f"[Coordinator] cached at {saved}")
            return decomposition

        # validate_fn surfaced real errors. Fold the critique items
        # into the retry feedback so the next attempt sees them too;
        # critique alone never gets us here.
        errors = list(errors) + critique_issues
        print(f"[Coordinator] Validation failed with {len(errors)} errors:")
        for err in errors:
            print(f"  - {err}")
        previous_errors = errors

    print("[Coordinator] All retries exhausted")
    return None
