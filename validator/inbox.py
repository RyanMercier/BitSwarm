"""
Task inbox lifecycle.

The contract between task producers (the submission API in
validator/api.py, or an operator dropping files by hand) and the
validator loop that consumes them (neurons/validator.py).

A task is one JSON file in the inbox directory. State is encoded in
the filename suffix, so it survives crashes and needs no database:

  <id>.json           pending: waiting for a validator loop
  <id>.json.working   claimed: a loop is running it right now
  <id>.json.done      completed: result in <output>/<task_id>/result.json
  <id>.json.failed    errored: details in the validator log

Task document shape:

  {"task_id": "...", "spec": "...", "target_repo": "/abs/path",
   "mode": "scaffold" | "diff", "subtask_timeout": 1200}
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid

_SUFFIXES = {
    "": "pending",
    ".working": "working",
    ".done": "done",
    ".failed": "failed",
}


def submit_task(inbox_dir: str, spec: str, target_repo: str,
                 mode: str = "scaffold", subtask_timeout: int = 1200,
                 task_id: str | None = None) -> str:
    """Write a task file atomically (tmp + rename) and return its id."""
    if mode not in ("scaffold", "diff"):
        raise ValueError(f"mode must be 'scaffold' or 'diff', got {mode!r}")
    if not spec.strip():
        raise ValueError("spec is empty")
    task_id = task_id or uuid.uuid4().hex
    os.makedirs(inbox_dir, exist_ok=True)
    doc = {
        "task_id": task_id,
        "spec": spec,
        "target_repo": target_repo,
        "mode": mode,
        "subtask_timeout": int(subtask_timeout),
    }
    final = os.path.join(inbox_dir, f"{task_id}.json")
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    os.rename(tmp, final)
    return task_id


def task_status(inbox_dir: str, task_id: str) -> str:
    """pending / working / done / failed / unknown for a task id."""
    base = os.path.join(inbox_dir, f"{task_id}.json")
    for suffix, status in _SUFFIXES.items():
        if os.path.isfile(base + suffix):
            return status
    return "unknown"


def recover_orphaned(inbox_dir: str) -> list:
    """Requeue tasks stranded mid-run by a crash.

    A ``.working`` file with no live loop behind it would otherwise
    sit forever. Called once at validator startup, when by definition
    no other loop of this validator holds a claim.
    """
    recovered = []
    if not os.path.isdir(inbox_dir):
        return recovered
    for name in sorted(os.listdir(inbox_dir)):
        if not name.endswith(".json.working"):
            continue
        working = os.path.join(inbox_dir, name)
        original = working[: -len(".working")]
        os.rename(working, original)
        recovered.append(os.path.basename(original)[: -len(".json")])
    return recovered


def load_result(output_dir: str, task_id: str) -> dict | None:
    """The scored result document, if the task finished."""
    path = os.path.join(output_dir, task_id, "result.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_patch(output_dir: str, task_id: str) -> str | None:
    """The combined patch artifact, if the task produced one."""
    path = os.path.join(output_dir, task_id, "patch.diff")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_patch_artifact(merge_repo: str, dest_path: str) -> bool:
    """Export the user deliverable: one unified diff covering the
    whole verified change (scaffolding, miner patches, repairs) from
    the repo's first commit to its final state.

    Commits any loose repair edits first so nothing is dropped.
    Returns False (writing nothing) when the repo has no usable
    history.
    """
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "BitSwarm", "GIT_AUTHOR_EMAIL": "b@local",
           "GIT_COMMITTER_NAME": "BitSwarm", "GIT_COMMITTER_EMAIL": "b@local"}
    subprocess.run(["git", "add", "-A"], cwd=merge_repo,
                    capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "final", "--allow-empty"],
                    cwd=merge_repo, env=env, capture_output=True)
    first = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=merge_repo, capture_output=True, text=True,
    )
    root = (first.stdout or "").strip().splitlines()
    if first.returncode != 0 or not root:
        return False
    diff = subprocess.run(
        ["git", "diff", root[0], "HEAD"],
        cwd=merge_repo, capture_output=True, text=True,
    )
    if diff.returncode != 0:
        return False
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(diff.stdout or "")
    return True
