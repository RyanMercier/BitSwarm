"""Guarded, logged access to the BitSwarm miner tools for experiment runs.

Every tool call still goes through ``miner.tools.run_tool`` unchanged:
the same schemas, the same ``allowed_files`` scope check, the same bash
denylist, and bash executes in the Docker sandbox (``--network=none``).
This module adds three things around it.

1. Host-escape guards. ``file_read``, ``file_write`` and ``list_files``
   run on the HOST, and ``bash`` runs in a container that can create
   symlinks inside the mounted workspace. A symlink created through bash
   would let a host-side file tool read or write outside the workspace.
   So path tools are refused when the resolved path leaves the
   workspace or the write target is a symlink, and after every bash call
   any symlink that resolves outside the workspace is removed. These
   blocks are logged as ``blocked_actions`` (reason ``PATH ESCAPE``,
   ``SYMLINK WRITE``, ``SYMLINK ESCAPE``): an attempt is data.
2. No host-side git in the workspace. The agent can edit ``.git/config``
   from bash (``core.fsmonitor``, filter drivers, hooks), and any git
   command the host then runs there would execute it. Workspace changes
   are measured with an lstat/hash walk instead, and the shipped patch
   is built in a fresh, host-owned clone (``extract_patch``).
3. A full event log: every call, its arguments, its output, and after
   each bash call the set of workspace paths it changed.

The agent's sandboxed bash gets PYTHONPATH=<repo>/src:<repo> and
PYTHONNOUSERSITE=1, the same import environment the grader's hermetic
gates use (``validator.diff_merge.isolated_test_env``), so the local
signal and the scored signal see the same code.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from collections import Counter

from miner.tools import configure, run_tool

NOISE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
GIT_WATCH = (".git/config", ".git/hooks", ".git/info/attributes")
VALIDATION_PREFIXES = {
    "SCOPE VIOLATION": "SCOPE VIOLATION",
    "INTERFACE VIOLATION": "INTERFACE VIOLATION",
    "Blocked command pattern": "BASH DENYLIST",
    "Path traversal denied": "PATH TRAVERSAL",
}
MAX_LOG_OUTPUT = 20000


def _inside(root_real: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == root_real or real.startswith(root_real + os.sep)


def snapshot(root: str) -> dict[str, str]:
    """{relative path: fingerprint} without following symlinks or running
    git. Symlinks are recorded by target; ``.git`` is skipped except for
    the files that can make host-side git execute code."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        keep = []
        for d in dirnames:
            rel = os.path.join(rel_dir, d) if rel_dir else d
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                out[rel] = "symlink:" + os.readlink(full)
                continue
            if d in NOISE_DIRS:
                continue
            if rel == ".git":
                for watch in GIT_WATCH:
                    wp = os.path.join(root, watch)
                    if os.path.isdir(wp) and not os.path.islink(wp):
                        for f in sorted(os.listdir(wp)):
                            if f.endswith(".sample"):
                                continue
                            fp = os.path.join(wp, f)
                            out[os.path.join(watch, f)] = _fingerprint(fp)
                    elif os.path.lexists(wp):
                        out[watch] = _fingerprint(wp)
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            rel = os.path.join(rel_dir, f) if rel_dir else f
            out[rel] = _fingerprint(os.path.join(dirpath, f))
    return out


def _fingerprint(path: str) -> str:
    try:
        st = os.lstat(path)
    except OSError:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        return "symlink:" + os.readlink(path)
    if not stat.S_ISREG(st.st_mode):
        return f"special:{st.st_mode:o}"
    if st.st_size > 5_000_000:
        return f"big:{st.st_size}:{st.st_mtime_ns}"
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def changed_paths(before: dict, after: dict) -> list[str]:
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


class ToolSession:
    """One agent's tool access for one run."""

    def __init__(self, repo_root: str, allowed_files: list[str], test_files: list[str],
                 mode: str, target_stubs: dict | None, log_path: str,
                 max_calls: int | None = None):
        self.root = os.path.abspath(repo_root)
        self.root_real = os.path.realpath(self.root)
        self.allowed = list(allowed_files)
        self.max_calls = max_calls
        configure(self.root, self.allowed, test_files, mode=mode,
                  target_stubs=target_stubs or {})
        src = os.path.join(self.root, "src")
        paths = [p for p in (src, self.root) if os.path.isdir(p)]
        os.environ["PYTHONPATH"] = os.pathsep.join(paths)
        os.environ["PYTHONNOUSERSITE"] = "1"
        self.baseline = snapshot(self.root)
        self.counts: Counter = Counter()
        self.blocked: list[dict] = []
        self.bash_touched: set[str] = set()
        self.calls = 0
        self.log_path = log_path
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

    # ---------------------------------------------------------- guards
    def _guard(self, name: str, params: dict) -> str | None:
        if name not in ("file_read", "file_write", "list_files"):
            return None
        rel = params.get("path", "." if name == "list_files" else "")
        full = os.path.normpath(os.path.join(self.root, rel))
        if not _inside(self.root_real, full):
            return "PATH ESCAPE"
        if name == "file_write" and os.path.islink(full):
            return "SYMLINK WRITE"
        return None

    def _purge_escaping_symlinks(self) -> list[dict]:
        removed = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            for n in list(dirnames) + filenames:
                full = os.path.join(dirpath, n)
                if os.path.islink(full) and not _inside(self.root_real, full):
                    target = os.readlink(full)
                    os.unlink(full)
                    removed.append({"path": os.path.relpath(full, self.root),
                                    "target": target})
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        return removed

    # ------------------------------------------------------------ call
    def call(self, name: str, params: dict) -> dict:
        started = time.time()
        self.calls += 1
        self.counts[name] += 1
        event: dict = {"i": self.calls, "t": round(started, 3), "tool": name,
                       "params": params}

        if self.max_calls is not None and self.calls > self.max_calls:
            result = {"success": False,
                      "output": "Tool budget exhausted. Stop calling tools and finish."}
            event["budget_exhausted"] = True
        else:
            reason = self._guard(name, params)
            if reason:
                result = {"success": False,
                          "output": f"{reason}: {params.get('path')} resolves outside "
                                    f"your workspace or is a symlink. Denied."}
                self.blocked.append({"tool": name, "path": params.get("path"),
                                     "reason": reason, "i": self.calls})
            else:
                before = snapshot(self.root) if name == "bash" else None
                result = run_tool(name, params)
                if not result["success"]:
                    for prefix, label in VALIDATION_PREFIXES.items():
                        if result["output"].startswith(prefix):
                            entry = {"tool": name, "reason": label, "i": self.calls}
                            if "path" in params:
                                entry["path"] = params["path"]
                            if "command" in params:
                                entry["command"] = params["command"]
                            self.blocked.append(entry)
                if name == "bash":
                    removed = self._purge_escaping_symlinks()
                    for r in removed:
                        self.blocked.append({"tool": "bash", "reason": "SYMLINK ESCAPE",
                                             "i": self.calls, **r})
                    after = snapshot(self.root)
                    touched = changed_paths(before, after)
                    self.bash_touched.update(touched)
                    event["workspace_changes"] = touched
                    if removed:
                        event["symlinks_removed"] = removed

        event["success"] = result["success"]
        out = result["output"]
        event["output"] = out if len(out) <= MAX_LOG_OUTPUT else (
            out[:MAX_LOG_OUTPUT // 2] + "\n[...]\n" + out[-MAX_LOG_OUTPUT // 2:])
        event["seconds"] = round(time.time() - started, 3)
        with open(self.log_path, "a") as f:
            f.write(json.dumps(event) + "\n")
        return result

    def summary(self) -> dict:
        final = snapshot(self.root)
        return {
            "tool_calls": dict(self.counts),
            "blocked_actions": self.blocked,
            "files_touched_via_bash": sorted(self.bash_touched),
            "files_changed_total": changed_paths(self.baseline, final),
        }


def extract_patch(baseline_bundle_clone: str, workspace: str,
                  allowed_files: list[str]) -> tuple[str, list[str]]:
    """Build the shipped patch without running git in the workspace.

    Copies each allowed file's bytes (regular files only) from the
    workspace into ``baseline_bundle_clone`` (a fresh, host-owned clone
    of the frozen baseline) and diffs there. Same semantics as
    ``miner.agent._generate_patch``: the diff of ``allowed_files``
    against the baseline commit, including new and deleted files.
    """
    notes: list[str] = []
    if not allowed_files:
        return "", ["empty allowed_files: empty patch"]
    for rel in allowed_files:
        src = os.path.join(workspace, rel)
        dst = os.path.join(baseline_bundle_clone, rel)
        if not os.path.lexists(src):
            if os.path.exists(dst):
                os.remove(dst)
                notes.append(f"{rel}: deleted by agent")
            continue
        st = os.lstat(src)
        if stat.S_ISLNK(st.st_mode):
            notes.append(f"{rel}: is a symlink in the workspace; baseline content kept")
            continue
        if not stat.S_ISREG(st.st_mode):
            notes.append(f"{rel}: not a regular file; baseline content kept")
            continue
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.copyfile(src, dst)
    subprocess.run(["git", "add", "-A", "--", *allowed_files],
                   cwd=baseline_bundle_clone, capture_output=True)
    diff = subprocess.run(["git", "diff", "--cached", "HEAD", "--", *allowed_files],
                          cwd=baseline_bundle_clone, capture_output=True, text=True)
    return diff.stdout or "", notes


def patch_files(patch: str) -> list[str]:
    """Paths a unified git diff touches (added, modified, or deleted)."""
    files = set()
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            files.add(line[len("diff --git a/"):].split(" b/", 1)[0])
    return sorted(files)
