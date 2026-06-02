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
