"""
Decomposition cache.

Phase 1 + Phase 2 of the coordinator typically cost 2-4 minutes per
task. When the user iterates on a spec (typo fixes, clarifications,
constraint tweaks), the *same* decomposition often comes back. The
cache short-circuits that round trip: hash the inputs, look up the
result, return the cached decomposition unchanged.

Key inputs that go into the hash:
  - feature_spec (the full spec text)
  - target_repo state (every readable file's path + content)
  - language profile name (so a Python and a TypeScript run on the
    same spec get separate cache entries)
  - coordinator model name (so an Opus and a Sonnet decomposition
    don't collide)
  - coordinator backend (sdk vs claude_code — different model
    behaviour produces different output)

If the validator rejects the cached decomposition (Phase 1.5 errors,
e.g. the user's repo gained a new file that needs different stubs),
we fall through to a fresh decomposition. So the cache is always
correctness-safe; worst case it costs a fresh run.

Disable with ``BITSWARM_NO_CACHE=1``. Override location with
``BITSWARM_CACHE_DIR``.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any


DEFAULT_CACHE_DIR = os.path.expanduser("~/.bitswarm/cache/decompositions")

# Walking the target repo should skip noisy directories that bloat
# the hash without changing the coordinator's output.
_REPO_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "target", ".venv", "venv",
    "dist", "build", ".pytest_cache", ".mypy_cache", "out",
})


def cache_dir() -> str:
    return os.environ.get("BITSWARM_CACHE_DIR") or DEFAULT_CACHE_DIR


def is_disabled() -> bool:
    return os.environ.get("BITSWARM_NO_CACHE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def compute_key(feature_spec: str, repo_path: str, language: str,
                 model: str, backend: str = "") -> str:
    """Produce a stable, short hex key for a given coordinator input set."""
    h = hashlib.sha256()
    h.update(b"v2:")  # bump prefix if the cache schema ever changes
    h.update(feature_spec.encode("utf-8", errors="replace"))
    h.update(b"|")
    h.update(language.encode("utf-8"))
    h.update(b"|")
    h.update(model.encode("utf-8"))
    h.update(b"|")
    h.update(backend.encode("utf-8"))
    h.update(b"|")
    _hash_repo_files(h, repo_path)
    return h.hexdigest()[:24]


def _hash_repo_files(h: "hashlib._Hash", repo_path: str) -> None:
    """Update ``h`` with every readable file in ``repo_path``, in a
    deterministic order. Skips obvious noise (caches, vcs, builds)."""
    if not os.path.isdir(repo_path):
        return
    entries: list[tuple[str, bytes]] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = sorted(d for d in dirs if d not in _REPO_SKIP_DIRS)
        for fname in sorted(files):
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, repo_path).replace("\\", "/")
            try:
                with open(full, "rb") as f:
                    content = f.read()
            except OSError:
                continue
            entries.append((rel, content))
    for rel, content in entries:
        h.update(b"\x00path:")
        h.update(rel.encode("utf-8"))
        h.update(b"\x00size:")
        h.update(str(len(content)).encode("ascii"))
        h.update(b"\x00content:")
        h.update(content)


def load(key: str) -> dict[str, Any] | None:
    """Return the cached decomposition for ``key`` or None."""
    if is_disabled():
        return None
    path = os.path.join(cache_dir(), f"{key}.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save(key: str, decomposition: dict[str, Any]) -> str | None:
    """Persist ``decomposition`` under ``key``. Returns the path written
    (or None if the cache is disabled / write failed)."""
    if is_disabled():
        return None
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        path = os.path.join(cache_dir(), f"{key}.json")
        # Atomic-ish write: temp file then rename, so a SIGINT mid-write
        # doesn't leave a half-decomposition that future loads will read.
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(decomposition, f, indent=2)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def evict(key: str) -> bool:
    """Remove a cache entry. Returns True if a file was removed."""
    path = os.path.join(cache_dir(), f"{key}.json")
    try:
        os.unlink(path)
        return True
    except OSError:
        return False
