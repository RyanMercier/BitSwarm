import os


def build_annotated_file_tree(
    repo_root,
    subtask_allowed_files,
    subtask_test_files,
    shared_files,
    all_subtask_files,
):
    """
    Build an annotated file tree that shows the miner exactly where
    everything is and what they can/cannot touch.
    """
    lines = ["Project structure:"]
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            d for d in sorted(dirs)
            if not d.startswith(".") and d not in ("__pycache__", "venv", "node_modules")
        ]
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
            if rel_path in subtask_allowed_files:
                lines.append(f"{file_indent}├── {f}  ← YOUR ASSIGNMENT")
            elif rel_path in subtask_test_files:
                lines.append(f"{file_indent}├── {f}  ← YOUR TESTS")
            elif rel_path in shared_files:
                lines.append(f"{file_indent}├── {f}  [shared - do not modify]")
            elif any(rel_path in files_list for files_list in all_subtask_files.values()):
                lines.append(f"{file_indent}├── {f}  [assigned to another engineer]")
            else:
                lines.append(f"{file_indent}├── {f}")
    return "\n".join(lines)


def build_warm_start_message(
    subtask,
    repo_root,
    shared_files_content,
    stub_files_content,
    test_files_content,
    all_subtask_files,
    shared_file_paths,
    all_subtasks=None,
):
    """
    Build the user message for a miner with pre-loaded file contents.
    Eliminates the miner's first 3-5 file_read calls.
    """
    subtask_id = subtask["subtask_id"]
    description = subtask["description"]
    allowed_files = subtask["allowed_files"]
    test_files = subtask["stub_test_files"]

    annotated_tree = build_annotated_file_tree(
        repo_root=repo_root,
        subtask_allowed_files=allowed_files,
        subtask_test_files=test_files,
        shared_files=shared_file_paths,
        all_subtask_files=all_subtask_files,
    )

    # Build pre-loaded file sections
    stub_sections = ""
    for path in allowed_files:
        content = stub_files_content.get(path, "")
        if not content:
            full_path = os.path.join(repo_root, path)
            if os.path.isfile(full_path):
                with open(full_path, "r") as f:
                    content = f.read()
        stub_sections += f"\n=== STUB FILE: {path} ===\n{content}\n"

    test_sections = ""
    for path in test_files:
        content = test_files_content.get(path, "")
        if not content:
            full_path = os.path.join(repo_root, path)
            if os.path.isfile(full_path):
                with open(full_path, "r") as f:
                    content = f.read()
        test_sections += f"\n=== TEST FILE: {path} ===\n{content}\n"

    schema_sections = ""
    for path, content in shared_files_content.items():
        schema_sections += f"\n=== SHARED SCHEMA: {path} ===\n{content}\n"

    # Pre-load dependency stub files (read-only) so miner knows exact interfaces
    dep_sections = ""
    dependencies = subtask.get("dependencies", [])
    if dependencies and all_subtasks:
        dep_stub_paths = set()
        for dep_id in dependencies:
            for st in all_subtasks:
                if st["subtask_id"] == dep_id:
                    for f in st.get("stub_files", []):
                        if f not in allowed_files:
                            dep_stub_paths.add(f)
        for path in sorted(dep_stub_paths):
            content = stub_files_content.get(path, "")
            if not content:
                full_path = os.path.join(repo_root, path)
                if os.path.isfile(full_path):
                    with open(full_path, "r") as fh:
                        content = fh.read()
            if content:
                dep_sections += f"\n=== DEPENDENCY STUB (read-only): {path} ===\n{content}\n"

    test_file_list = " ".join(test_files)

    return f"""Your assignment: {subtask_id}
Description: {description}
Files to implement: {allowed_files}
Tests to pass: {test_files}

{annotated_tree}
{stub_sections}
{test_sections}
{schema_sections}
{dep_sections}
## IMPORTANT: Cross-Subtask Dependencies

Other engineers' modules (marked "[assigned to another engineer]" above) are stubs
in your repo  -  their functions raise NotImplementedError. If your tests import and
use objects from those modules, those tests will fail with NotImplementedError BEFORE
testing your stub.

When this happens, edit your test file to use `unittest.mock.MagicMock()` instead:
  from unittest.mock import MagicMock
  dep = MagicMock()  # replaces any object from another subtask's module
  my_obj = MyClass(dep)  # now tests YOUR stub

Your test files ARE in your allowed_files  -  you may and should edit them.

If DEPENDENCY STUB files are shown above, they define the EXACT function signatures
of modules you depend on. When calling functions from those modules, match the
signatures exactly (parameter names, types, count). Do not guess interfaces.

Start implementing. Run tests with: pytest {test_file_list} -v --tb=short"""


def build_diff_warm_start_message(
    subtask,
    repo_root,
    target_stubs,
    new_test_files_content,
    shared_additions_content,
    all_subtasks=None,
):
    """Warm-start message for a diff-mode miner.

    The miner sees:
      - The CHANGE description and behavior_spec for its subtask
      - The CURRENT (unmodified) content of every file in its
        modify_files; this is the starting point it edits
      - The TARGET STUB for every file in its modify_files; this is
        the post-edit contract its implementation must converge to
      - The NEW TEST FILES that pin the new behavior
      - Any SHARED ADDITIONS (read-only) other subtasks may rely on
      - For each dependency subtask, the TARGET STUB of that
        dependency's modify_files (so this miner knows the post-edit
        interfaces it will be calling into)
    """
    subtask_id = subtask["subtask_id"]
    description = subtask.get("description", "")
    behavior_spec = subtask.get("behavior_spec", "")
    modify_files = subtask.get("modify_files", []) or []
    new_test_files = subtask.get("new_test_files", []) or []
    dependencies = subtask.get("dependencies", []) or []

    target_stubs = target_stubs or {}
    new_test_files_content = new_test_files_content or {}
    shared_additions_content = shared_additions_content or {}

    # CURRENT content of files to modify
    current_sections = ""
    for path in modify_files:
        full_path = os.path.join(repo_root, path)
        content = ""
        if os.path.isfile(full_path):
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        current_sections += f"\n=== CURRENT (unmodified): {path} ===\n{content}\n"

    # TARGET STUB for each modify file
    target_sections = ""
    for path in modify_files:
        stub = target_stubs.get(path, "")
        if stub:
            target_sections += f"\n=== TARGET STUB: {path} ===\n{stub}\n"
        else:
            target_sections += (
                f"\n=== TARGET STUB: {path} ===\n"
                f"(no target stub provided; preserve the current public API "
                f"and add only what the behavior spec requires)\n"
            )

    # NEW TEST FILES already written to disk by the diff scaffolder; we
    # include them inline so the miner does not need a file_read round-trip
    test_sections = ""
    for path in new_test_files:
        content = new_test_files_content.get(path, "")
        if not content:
            full_path = os.path.join(repo_root, path)
            if os.path.isfile(full_path):
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
        test_sections += f"\n=== NEW TEST FILE: {path} ===\n{content}\n"

    # SHARED ADDITIONS the miner can import from
    shared_sections = ""
    for path, content in shared_additions_content.items():
        shared_sections += f"\n=== SHARED ADDITION (read-only): {path} ===\n{content}\n"

    # Dependency target stubs so this miner knows the post-edit
    # interfaces of files other subtasks are modifying.
    dep_sections = ""
    if dependencies and all_subtasks:
        for dep_id in dependencies:
            for st in all_subtasks:
                if st.get("subtask_id") != dep_id:
                    continue
                for f in st.get("modify_files", []) or []:
                    stub = target_stubs.get(f, "")
                    if stub:
                        dep_sections += (
                            f"\n=== DEPENDENCY TARGET STUB (subtask "
                            f"{dep_id!r}, file {f}) ===\n{stub}\n"
                        )

    test_file_list = " ".join(new_test_files)

    return f"""Your assignment (DIFF MODE): {subtask_id}

Description: {description}

Behavior spec:
{behavior_spec}

Files you must modify: {modify_files}
New tests that must pass after your changes: {new_test_files}

## How diff mode works

1. The files in "Files you must modify" already exist in the repo with
   real working code. Your job is to MODIFY them to match the TARGET
   STUB shown below.
2. The TARGET STUB shows the post-edit public API for each file.
   Function and method signatures in the target stub are the contract.
3. The behavior spec describes WHAT changes (new functionality, removed
   functionality, modified semantics).
4. The NEW TEST FILES already live in your repo. Run them; they fail on
   the unmodified code and must pass after your changes.
5. The existing project test suite (whatever was already in the repo)
   MUST CONTINUE TO PASS. Do not break existing tests.

You may use file_read to read any file. You may use file_write on the
files in your allowed_files list (which is modify_files + new_test_files).
file_write OVERWRITES the file with the content you provide; provide the
full new file content, not a diff.
{current_sections}
{target_sections}
{test_sections}
{shared_sections}
{dep_sections}
## Strategy

1. Read each CURRENT file above. Understand what is there.
2. Read each TARGET STUB. Understand what the post-edit shape should be.
3. Read the NEW TEST FILES. They are the source of truth for the new
   behavior.
4. Modify each file to match its target stub AND make the new tests pass.
5. Run the new tests: pytest {test_file_list} -v --tb=short
6. Iterate until they pass. Then stop.

Start implementing."""
