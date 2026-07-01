"""
Hidden-test commit-reveal.

Anti-overfitting mechanism: the coordinator holds back a fraction of
the gate tests from miners. Miners implement against the visible
tests; the validator scores against visible PLUS held-back tests. A
hash of the held-back set is committed before any miner starts, so
after scoring anyone can verify the validator did not invent tests to
punish (or invent passes to favor) a particular miner after seeing
the patches.

Selection is deterministic given (test set, seed): validators
re-running the same decomposition derive the same split, which is
what makes passive re-verification meaningful.

Enabled via BITSWARM_HOLDBACK_FRACTION (default 0 = off, since a
solo-validator dev loop gains nothing from hiding tests from
itself). Turned on for testnet in the runbook.
"""
from __future__ import annotations

import hashlib
import json
import os


def holdback_fraction() -> float:
    try:
        return max(0.0, min(0.9, float(
            os.environ.get("BITSWARM_HOLDBACK_FRACTION", "0"))))
    except ValueError:
        return 0.0


def select_holdback(test_files: dict[str, str], fraction: float,
                     seed: str) -> tuple[dict, dict]:
    """Deterministically split test files into (visible, held).

    - Never holds anything back when fewer than 2 files exist: miners
      must always have at least one visible gate to iterate against.
    - Always leaves at least one file visible.
    - Selection order is by sha256(seed + path), so the split is
      stable for a given decomposition and unpredictable to miners
      (the seed is the task id, which miners see only after the
      commit hash is already recorded).
    """
    if fraction <= 0 or len(test_files) < 2:
        return dict(test_files), {}
    n_hold = min(len(test_files) - 1,
                  max(1, int(len(test_files) * fraction)))
    ranked = sorted(
        test_files,
        key=lambda p: hashlib.sha256((seed + p).encode()).hexdigest(),
    )
    held_paths = set(ranked[:n_hold])
    visible = {p: c for p, c in test_files.items() if p not in held_paths}
    held = {p: c for p, c in test_files.items() if p in held_paths}
    return visible, held


def commit_hash(held: dict) -> str:
    """Canonical sha256 over the held-back set. Empty set -> ""."""
    if not held:
        return ""
    canonical = json.dumps(held, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_reveal(held: dict, committed: str) -> bool:
    """True iff the revealed set matches the pre-mining commitment."""
    return commit_hash(held) == committed


def apply_holdback(decomposition: dict, seed: str) -> dict:
    """Split the decomposition's gate tests, stashing the held set.

    Mutates and returns the decomposition:
      - diff mode: splits ``new_test_files``
      - scaffold mode: splits ``integration_test_files``
      - adds ``holdback_tests`` (path -> content) and
        ``holdback_commit`` (sha256)
    No-op when the fraction is 0 or there is nothing to split.
    """
    fraction = holdback_fraction()
    key = ("new_test_files" if decomposition.get("mode") == "diff"
            else "integration_test_files")
    tests = decomposition.get(key, {}) or {}
    visible, held = select_holdback(tests, fraction, seed)
    decomposition[key] = visible
    decomposition["holdback_tests"] = held
    decomposition["holdback_commit"] = commit_hash(held)
    if held:
        print(f"  [holdback] holding back {len(held)}/{len(tests)} gate "
              f"test file(s); commit={decomposition['holdback_commit'][:12]}")
    return decomposition


def reveal_into_repo(decomposition: dict, repo_path: str) -> list[str]:
    """Write the held-back tests into a merge repo at scoring time.

    Verifies the reveal against the commitment first; a mismatch
    raises, because a validator whose holdback set does not match its
    own commitment is either buggy or dishonest and must not score.
    Returns the list of written paths (empty when no holdback).
    """
    held = decomposition.get("holdback_tests", {}) or {}
    committed = decomposition.get("holdback_commit", "")
    if not held:
        return []
    if not verify_reveal(held, committed):
        raise RuntimeError(
            "holdback reveal does not match the pre-mining commitment; "
            "refusing to score"
        )
    written = []
    for rel, content in held.items():
        full = os.path.join(repo_path, rel)
        parent = os.path.dirname(full) or "."
        os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(rel)
    print(f"  [holdback] revealed {len(written)} held-back test file(s) "
          f"into merge repo (commit verified)")
    return written
