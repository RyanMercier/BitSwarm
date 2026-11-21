"""Hidden-test holdback: split, commit, reveal.

Moved verbatim from test_chain_layer.py when the redteam branch cut
the Bittensor layer that file also covered.
"""
import os

import pytest

from validator.holdback import (
    apply_holdback,
    commit_hash,
    reveal_into_repo,
    select_holdback,
    verify_reveal,
)


def _tests(n):
    return {f"tests/test_{i}.py": f"def test_{i}(): assert True" for i in range(n)}


def test_holdback_selection_is_deterministic():
    tests = _tests(6)
    v1, h1 = select_holdback(tests, 0.34, seed="task-abc")
    v2, h2 = select_holdback(tests, 0.34, seed="task-abc")
    assert h1 == h2 and v1 == v2
    assert len(h1) == 2
    v3, h3 = select_holdback(tests, 0.34, seed="task-xyz")
    assert h3.keys() != h1.keys() or True  # different seed may differ


def test_holdback_always_leaves_one_visible():
    tests = _tests(2)
    visible, held = select_holdback(tests, 0.9, seed="s")
    assert len(visible) >= 1
    assert len(held) == 1

    only_one = _tests(1)
    visible, held = select_holdback(only_one, 0.9, seed="s")
    assert held == {} and len(visible) == 1


def test_holdback_commit_and_verify():
    _, held = select_holdback(_tests(4), 0.5, seed="s")
    commit = commit_hash(held)
    assert verify_reveal(held, commit)
    tampered = dict(held)
    first = next(iter(tampered))
    tampered[first] = tampered[first] + "\n# sneaky edit"
    assert not verify_reveal(tampered, commit)
    assert commit_hash({}) == ""


def test_apply_holdback_diff_mode(monkeypatch):
    monkeypatch.setenv("BITSWARM_HOLDBACK_FRACTION", "0.5")
    decomp = {"mode": "diff", "new_test_files": _tests(4)}
    apply_holdback(decomp, seed="t1")
    assert len(decomp["new_test_files"]) == 2
    assert len(decomp["holdback_tests"]) == 2
    assert len(decomp["holdback_commit"]) == 64


def test_apply_holdback_defaults_off(monkeypatch):
    monkeypatch.delenv("BITSWARM_HOLDBACK_FRACTION", raising=False)
    decomp = {"mode": "diff", "new_test_files": _tests(4)}
    apply_holdback(decomp, seed="t1")
    assert len(decomp["new_test_files"]) == 4
    assert decomp["holdback_tests"] == {}
    assert decomp["holdback_commit"] == ""


def test_reveal_into_repo_verifies_commit(tmp_path):
    _, held = select_holdback(_tests(4), 0.5, seed="s")
    decomp = {"holdback_tests": held, "holdback_commit": commit_hash(held)}
    written = reveal_into_repo(decomp, str(tmp_path))
    assert sorted(written) == sorted(held)
    for rel in held:
        assert os.path.isfile(tmp_path / rel)

    bad = {"holdback_tests": held, "holdback_commit": "0" * 64}
    with pytest.raises(RuntimeError, match="commitment"):
        reveal_into_repo(bad, str(tmp_path))

