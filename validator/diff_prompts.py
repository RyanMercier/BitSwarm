"""
Diff-mode coordinator prompts.

The scaffold-mode coordinator (`validator/prompts.py` +
`validator/decomposer.py:build_user_message`) operates on the
assumption that the target codebase is empty (or close to it) and the
coordinator's job is to write new stubs that miners then implement.

Diff mode is the opposite: the target codebase exists and works. The
coordinator's job is to decompose a CHANGE into per-file modification
subtasks. For each subtask the coordinator produces:

  - A list of EXISTING files the subtask is allowed to modify
  - A list of NEW test files that pin the new behavior
  - A behavior_spec describing what changes in the modified files
  - A "target_stub" per modified file: the post-edit public API
    (signatures + types + docstrings) the miner must converge to

The miner is given the original file content plus the target stub
plus the new tests; it produces a modified version of the file that
matches the target stub and satisfies the new tests, without breaking
the existing test suite.

Why target stubs rather than free-form edit specs: the same reason
scaffold mode uses NotImplementedError stubs. An executable signature
is unambiguous; an English description of "what should change" is not.
Two miners working in parallel on dependent files must agree on the
post-edit interface; the target stub is the contract.
"""
from __future__ import annotations


DIFF_COORDINATOR_SYSTEM_PROMPT = """\
You are the BitSwarm Diff-Mode Coordinator. Your job is to decompose a
CHANGE to an existing codebase into per-file modification subtasks
that multiple independent coding agents can implement in parallel
without communication.

You produce EXECUTABLE CONTRACTS, not English descriptions. For each
modified file you produce a target-state stub: the post-edit public
API as real source code (signatures, types, docstrings, but with
placeholder bodies). The miner converges its implementation to this
contract. This eliminates the primary failure mode of parallel
modification: two miners independently changing the same function's
signature in incompatible ways.

## What you receive

1. An existing repository (full file tree + file contents, pre-loaded
   in the user message; you do not have tools in this invocation).
2. A natural-language description of the change to make.
3. The existing test suite, which is the regression gate (anything
   you change must keep it passing).

## What you produce

A JSON decomposition with these top-level fields:

- "mode": always "diff"
- "subtasks": one per cluster of related file modifications
- "target_stubs": for every existing file any subtask modifies, the
  post-edit signature stub
- "new_test_files": net-new test file content (the additive gate)
- "shared_additions": any net-new shared type / config files the
  change requires
- "integration_test_files": net-new integration tests (optional)
- "requirements_additions": any new third-party dependencies

## How to decompose

Step 1: read the existing code. Understand what is there, which
modules are related, where the public boundaries are. Skim the
existing test suite to understand the regression contract.

Step 2: map the change spec onto existing files. For each existing
file that needs to change, decide:
  - Which subtask owns the modification
  - What the post-edit public API of the file looks like (the target
    stub)
  - Whether any net-new shared types are needed across subtasks

Step 3: define subtask boundaries. Each subtask owns a set of
modify_files. NO FILE may appear in more than one subtask's
modify_files. Subtask boundaries should respect existing module
boundaries when possible (one subtask per module is the easiest case).

Step 4: write the post-edit signatures. For each file in any
subtask's modify_files, write the target_stubs[file] entry. This is
the file's new public surface: every public function/class that will
exist after the modification, with correct signatures, type hints,
and docstrings, but with placeholder bodies. Private helpers and
implementation details are NOT in the stub; only the post-edit
public API.

Step 5: write the new tests. For each subtask, write one or more
test files that pin the new behavior. These tests must FAIL on the
unmodified code and PASS only when the miner correctly implements
the change.

Step 6: identify shared additions if needed. If the change introduces
new types or constants that multiple subtasks need, put them in
shared_additions with COMPLETE implementations (not stubs). Miners
import from them but do not modify them.

## Critical constraints

IMPORTANT: The miners cannot communicate. Every ambiguity in your
target stubs becomes an integration failure at merge time.

- A modified file's target_stub must be the EXACT public API the
  rest of the project will see post-edit. If subtask A's modified
  file calls a function from subtask B's modified file, the
  signature in B's target stub must match A's call sites.
- new_test_files paths must be net-new. Do not specify modifications
  to existing test files via new_test_files; if an existing test
  file needs modification, put it in a subtask's modify_files
  instead.
- Every file path in any subtask's modify_files must exist in the
  current repository. Do not invent new files in modify_files; new
  files belong in shared_additions or new_test_files.
- Complexity weights sum to 1.0 across subtasks.

## Anti-patterns

- DO NOT produce target_stubs whose signatures conflict with how the
  rest of the existing codebase calls into them. The whole repo must
  still work post-edit.
- DO NOT write target_stubs that are identical to the original file
  (no-op modifications). If a file does not need to change, it does
  not appear in any subtask's modify_files.
- DO NOT write tests that pass against the unmodified code. The new
  tests are the additive gate; if they pass without the change, they
  are not testing the change.
- DO NOT decompose so finely that each subtask modifies a single
  function. Subtasks should respect logical boundaries (modules,
  layers); a subtask modifying a single line is wasted overhead.

## Output format

Return ONLY a JSON object with this exact structure:

{
  "mode": "diff",
  "subtasks": [
    {
      "subtask_id": "snake_case_id",
      "description": "one-line summary of the change in this subtask",
      "modify_files": ["path/to/existing/file1.py", "path/to/existing/file2.py"],
      "new_test_files": ["tests/test_new_behavior_subtask.py"],
      "behavior_spec": "detailed description of what changes in modify_files: what new behavior, what removed behavior, what unchanged. Reference the relevant target_stubs entries.",
      "dependencies": ["other_subtask_id"],
      "complexity_weight": 0.4
    }
  ],
  "target_stubs": {
    "path/to/existing/file1.py": "# Target post-edit public API\\n\\nclass Foo:\\n    def existing_method(self, x: int) -> str:\\n        \\\"\\\"\\\"Docstring describing post-edit behavior.\\\"\\\"\\\"\\n        raise NotImplementedError\\n\\n    def new_method(self) -> None:\\n        \\\"\\\"\\\"Docstring for new method added by the change.\\\"\\\"\\\"\\n        raise NotImplementedError\\n"
  },
  "new_test_files": {
    "tests/test_new_behavior_subtask.py": "import pytest\\nfrom path.to.existing.file1 import Foo\\n\\ndef test_new_method_does_x():\\n    ..."
  },
  "shared_additions": {},
  "integration_test_files": {},
  "requirements_additions": []
}

Start your response with the opening { brace. No prose before the JSON.
"""


def build_diff_phase1_prompt(repo_path, change_spec, previous_errors=None,
                              language: str | None = None) -> str:
    """Phase 1 diff prompt: identify modify_files + decompose into subtasks.

    Output: a partial decomposition with subtasks + target_stub
    placeholders (Phase 2 fills in the actual stub content). Same
    two-phase split that scaffold mode uses, for the same reason
    (large JSON outputs truncate).
    """
    # Lazy import to avoid circular dependency on the SDK coordinator
    # module which imports from this file.
    from validator.decomposer import (
        collect_repo_files,
        get_file_tree,
    )
    from validator.lang_profiles import profile_for

    profile = profile_for(language=language, repo_path=repo_path)
    file_tree = get_file_tree(repo_path)
    repo_files = collect_repo_files(repo_path)

    # Budget for pre-loaded file content in the Phase 1 prompt. Below
    # this threshold (small repos like the wordle demo target), include
    # every file inline so the coordinator can plan with full context.
    # Above it (real OSS repos: click is ~940KB, would blow past the
    # model's context window), include only the file tree and a small
    # selection of "obviously relevant" files (those mentioned by name
    # or relative path in the change spec). Phase 2 then pre-loads the
    # full content of every file Phase 1 marked for modification, so
    # target stubs always converge against the real source.
    PHASE1_CONTENT_BUDGET_BYTES = 80_000
    total_bytes = sum(len(c.encode("utf-8")) for c in repo_files.values())

    file_contents = ""
    inclusion_note = ""
    if total_bytes <= PHASE1_CONTENT_BUDGET_BYTES:
        for path, content in repo_files.items():
            file_contents += f"\n=== {path} ===\n{content}\n"
        inclusion_note = (
            f"All {len(repo_files)} files pre-loaded "
            f"({total_bytes / 1024:.0f} KB total)."
        )
    else:
        # Pick files whose path is mentioned in the change spec, plus
        # all top-level __init__.py files in any source package
        # (those are the export surface and tend to be small).
        spec_lower = change_spec.lower()
        mentioned = [
            p for p in repo_files
            if p.lower() in spec_lower
            or p.split("/")[-1].lower() in spec_lower
        ]
        inits = [p for p in repo_files
                 if p.endswith("__init__.py") and p.count("/") <= 2]
        candidates = list(dict.fromkeys(mentioned + inits))

        included: list[str] = []
        bytes_used = 0
        for p in candidates:
            content = repo_files.get(p, "")
            if not content:
                continue
            csize = len(content.encode("utf-8"))
            if bytes_used + csize > PHASE1_CONTENT_BUDGET_BYTES:
                continue
            file_contents += f"\n=== {p} ===\n{content}\n"
            included.append(p)
            bytes_used += csize

        inclusion_note = (
            f"Repository is large ({total_bytes / 1024:.0f} KB / "
            f"{len(repo_files)} files). Pre-loaded {len(included)} "
            f"files referenced by name in the change spec or matching "
            f"package __init__.py conventions ({bytes_used / 1024:.0f} KB). "
            f"For files not pre-loaded, plan based on the file tree and "
            f"the spec; Phase 2 pre-loads the full content of every "
            f"file you mark for modification, so your target stubs will "
            f"always see the real source before they are written."
        )

    language_block = (
        "## TARGET LANGUAGE\n\n"
        f"This repository's primary language is **{profile.display_name}**.\n"
        f"All target stubs and new test files must use {profile.display_name}\n"
        f"conventions and the appropriate file extensions ({', '.join(profile.extensions)}).\n\n"
        "---\n\n"
    )

    message = language_block + f"""IMPORTANT: Repository context is pre-loaded below. You do NOT have tools in this invocation. Skim the file tree and any pre-loaded file contents directly; proceed to decomposition.

## Existing Repository

{inclusion_note}

File tree:
{file_tree}
{file_contents}

## Change Specification

{change_spec}

## Output Requirements - READ CAREFULLY

PHASE 1 - PLAN ONLY: Output the modification plan. Include:
- "mode": "diff"
- "subtasks": full array with subtask_id, description, modify_files (list of EXISTING file paths), new_test_files, behavior_spec, dependencies, complexity_weight
- "target_stubs": {{}}  (leave empty, Phase 2 fills in)
- "new_test_files": {{}}  (leave empty, Phase 2 fills in)
- "shared_additions": {{}}  (leave empty unless the change requires net-new shared types; in that case populate now)
- "integration_test_files": {{}}  (leave empty, Phase 2 may fill in)
- "requirements_additions": list of any new third-party deps

Phase 2 will generate target_stubs, new_test_files, and integration_test_files content. Focus on getting the plan right.

EVERY file path in any subtask's modify_files must appear in the file tree above. Do not invent file paths.

Output a single JSON object. Start with the opening {{ brace. No prose before the JSON."""

    if previous_errors:
        error_block = "\n".join(f"- {e}" for e in previous_errors)
        message += f"""

## VALIDATION ERRORS FROM PREVIOUS ATTEMPT

Your previous decomposition failed validation with these errors:

{error_block}

Output only the corrected JSON, starting with {{"""

    return message


def build_diff_phase2_prompt(decomposition, repo_path, change_spec,
                              language: str | None = None) -> str:
    """Phase 2 diff prompt: generate target_stubs + new_test_files content.

    The model has already produced the subtask plan in Phase 1. Now it
    fills in:
      - target_stubs[file] for every file in any subtask's modify_files
      - new_test_files[path] for every path in any subtask's new_test_files
      - integration_test_files (optional)

    Output is harvested by the subprocess coordinator's
    file-writing wrapper (the same mechanism the scaffold-mode Phase 2
    uses for stub generation).
    """
    from validator.decomposer import collect_repo_files
    from validator.lang_profiles import profile_for

    profile = profile_for(language=language, repo_path=repo_path)
    subtasks = decomposition.get("subtasks", []) or []

    # Pre-load the CURRENT content of every file that any subtask will
    # modify, so the model can write target stubs that match the
    # post-edit shape of the existing code (not a guess at what was
    # there).
    modify_files = []
    for st in subtasks:
        for f in st.get("modify_files", []) or []:
            if f not in modify_files:
                modify_files.append(f)
    repo_files = collect_repo_files(repo_path)
    existing_block = ""
    for f in modify_files:
        content = repo_files.get(f, "")
        if content:
            existing_block += f"\n=== EXISTING (to be modified): {f} ===\n{content}\n"
        else:
            existing_block += f"\n=== EXISTING (to be modified): {f} ===\n(file not pre-loaded; rely on Phase 1 context)\n"

    subtask_block = ""
    for st in subtasks:
        subtask_block += (
            f"\n### Subtask: {st.get('subtask_id', '?')}\n"
            f"Description: {st.get('description', '')}\n"
            f"Behavior spec: {st.get('behavior_spec', '')}\n"
            f"Modify files: {st.get('modify_files', [])}\n"
            f"New test files: {st.get('new_test_files', [])}\n"
            f"Dependencies on other subtasks: {st.get('dependencies', [])}\n"
        )

    shared_additions = decomposition.get("shared_additions", {}) or {}
    shared_block = ""
    for path, content in shared_additions.items():
        shared_block += f"\n=== SHARED ADDITION: {path} ===\n{content}\n"
    if not shared_block:
        shared_block = "(none)"

    target_stub_paths = sorted(modify_files)
    target_stub_list = "\n".join(f"  - {p}" for p in target_stub_paths)

    new_test_paths = []
    for st in subtasks:
        for p in st.get("new_test_files", []) or []:
            if p not in new_test_paths:
                new_test_paths.append(p)
    new_test_list = "\n".join(f"  - {p}" for p in new_test_paths)

    return f"""{profile.phase2_intro.replace('stub files', 'TARGET STUBS and NEW TEST FILES')}

Target language: {profile.display_name}.

## Change spec

{change_spec[:2000]}

## Subtask plan (from Phase 1)
{subtask_block}

## Shared additions (already-defined types you can use)
{shared_block}

## Existing files that will be modified (post-edit stubs must converge to these structures)
{existing_block}

## Files you must write NOW

TARGET STUBS (one per file in any subtask's modify_files; show the
post-edit public API as real source code with placeholder bodies):
{target_stub_list}

NEW TEST FILES (one per path in any subtask's new_test_files; tests
must FAIL on the current unmodified code and PASS once miners
implement the change):
{new_test_list}

## Rules

Target stubs:
- Show the EXACT public API the file will have after the modification.
- Every public function/class signature, with type annotations and
  docstrings describing post-edit behavior.
- Function bodies use the language's "not implemented" idiom (the
  same idiom scaffold mode uses).
- Private helpers and implementation details are NOT in the stub;
  only the public surface.
- The stub is a SPEC for the miner, not the final code. The miner
  reads the existing file plus this stub and produces an
  implementation matching the stub's signatures.

New test files:
{profile.test_rules}

PLUS, specifically for diff mode:
- Each new test must FAIL on the CURRENT unmodified code (the test
  exercises behavior that does not yet exist). If a test would pass
  on the current code, it does not validate the change.
- Tests must PASS on a correct implementation of the target stubs.
- Import from the existing modules (the ones in modify_files);
  reference the post-edit interfaces from the target stubs above.

Integration tests (if any):
{profile.integration_rules}

## Output format

Write each file directly to disk using the Write tool. Paths are
relative to your current working directory:

  - target_stubs go at the file's repo-relative path with a
    `.target_stub` suffix appended (e.g. `auth/jwt.py.target_stub`).
    This keeps them separate from the original on disk so the
    harvester can collect them without touching the original.
  - new_test_files go at their declared paths verbatim.
  - integration_test_files go at their declared paths verbatim.
  - shared_additions (if any new ones beyond Phase 1) go at their
    declared paths verbatim.

After all required files are written, stop. Do not print contents
to stdout.
"""
