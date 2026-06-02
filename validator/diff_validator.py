"""
Diff-mode decomposition validator.

Structural checks against the JSON the coordinator returns in diff
mode. Catches the failure modes that would otherwise only surface
during mining (e.g. a subtask claiming to modify a file that doesn't
exist), so the coordinator can retry with feedback.

Distinct from `validator/validator_checks.py` (the scaffold-mode
validator) because the contract shapes are different. Diff-mode
subtasks reference EXISTING files; scaffold-mode subtasks reference
files-to-be-created.
"""
from __future__ import annotations

import os


def validate_diff_decomposition(decomposition: dict, repo_path: str) -> list[str]:
    """Return a list of error strings; empty list means the decomposition
    is valid for diff-mode mining.

    Checks:
      1. mode field is "diff"
      2. subtasks is a non-empty list
      3. Every subtask has the required fields
      4. complexity_weight values sum to 1.0 (within tolerance)
      5. modify_files paths exist in the repo
      6. modify_files paths do not overlap across subtasks
      7. Every modify_file has a corresponding target_stubs entry
      8. new_test_files paths do not collide with existing repo files
      9. new_test_files paths in subtasks have content in top-level new_test_files
      10. subtask dependencies refer to other valid subtask_ids (no cycles)
    """
    errors: list[str] = []

    mode = decomposition.get("mode")
    if mode != "diff":
        errors.append(
            f"Expected mode='diff', got {mode!r}. The diff-mode validator only "
            "handles diff-mode decompositions."
        )

    subtasks = decomposition.get("subtasks") or []
    if not subtasks:
        errors.append("Decomposition has no subtasks; cannot mine.")
        return errors  # early bail: rest of the checks assume subtasks

    # 3. Required fields per subtask
    required_fields = ("subtask_id", "description", "modify_files",
                        "new_test_files", "behavior_spec", "complexity_weight")
    for i, st in enumerate(subtasks):
        for field in required_fields:
            if field not in st:
                errors.append(f"subtasks[{i}] missing required field {field!r}")

    # If any subtask is missing required fields, the structural checks
    # below would crash; bail to let the coordinator fix structure first.
    if errors:
        return errors

    # 4. Weight sum
    total_weight = sum(float(st.get("complexity_weight", 0)) for st in subtasks)
    if not (0.99 <= total_weight <= 1.01):
        errors.append(
            f"complexity_weight values sum to {total_weight:.3f}, expected 1.0."
        )

    # 5. modify_files paths exist
    for st in subtasks:
        sid = st.get("subtask_id", "?")
        for f in st.get("modify_files", []) or []:
            full = os.path.normpath(os.path.join(repo_path, f))
            if not os.path.isfile(full):
                errors.append(
                    f"subtask {sid!r} modify_files references {f!r}, which "
                    f"does not exist in the target repository."
                )

    # 6. modify_files no overlap across subtasks
    seen: dict[str, str] = {}
    for st in subtasks:
        sid = st.get("subtask_id", "?")
        for f in st.get("modify_files", []) or []:
            if f in seen:
                errors.append(
                    f"modify_file {f!r} appears in both subtask {seen[f]!r} "
                    f"and {sid!r}. Each file must belong to exactly one subtask."
                )
            else:
                seen[f] = sid

    # 7. Every modify_file has a target_stubs entry
    target_stubs = decomposition.get("target_stubs", {}) or {}
    all_modify_files: set[str] = set()
    for st in subtasks:
        all_modify_files.update(st.get("modify_files", []) or [])
    for f in all_modify_files:
        if f not in target_stubs:
            errors.append(
                f"modify_file {f!r} has no entry in target_stubs. The "
                "coordinator must provide a post-edit signature stub for every "
                "modified file."
            )

    # 8. new_test_files paths do not collide with existing files
    for st in subtasks:
        sid = st.get("subtask_id", "?")
        for f in st.get("new_test_files", []) or []:
            full = os.path.normpath(os.path.join(repo_path, f))
            if os.path.isfile(full):
                errors.append(
                    f"subtask {sid!r} new_test_files references {f!r}, but "
                    "that path already exists in the repo. new_test_files must "
                    "be net-new paths. To modify an existing test file, add it "
                    "to modify_files instead."
                )

    # 9. new_test_files have content in the top-level dict
    new_test_files = decomposition.get("new_test_files", {}) or {}
    for st in subtasks:
        sid = st.get("subtask_id", "?")
        for f in st.get("new_test_files", []) or []:
            if f not in new_test_files:
                errors.append(
                    f"subtask {sid!r} declares new_test_file {f!r} but no "
                    "content for that path appears in the top-level "
                    "new_test_files dict."
                )

    # 10. Dependency cycles
    by_id = {st["subtask_id"]: st for st in subtasks}
    for st in subtasks:
        for dep in st.get("dependencies", []) or []:
            if dep not in by_id:
                errors.append(
                    f"subtask {st['subtask_id']!r} depends on {dep!r}, which "
                    "is not a valid subtask_id."
                )

    if _has_cycle(by_id):
        errors.append("Subtask dependency graph contains a cycle.")

    return errors


def _has_cycle(by_id: dict) -> bool:
    """Standard DFS cycle detection on the dependency graph."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in by_id}

    def visit(sid: str) -> bool:
        color[sid] = GRAY
        for dep in by_id[sid].get("dependencies", []) or []:
            if dep not in color:
                continue
            if color[dep] == GRAY:
                return True
            if color[dep] == WHITE and visit(dep):
                return True
        color[sid] = BLACK
        return False

    for sid in by_id:
        if color[sid] == WHITE and visit(sid):
            return True
    return False
