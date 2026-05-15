"""Tests for the decomposition cache."""
from __future__ import annotations

import os

from validator import cache


def test_key_is_deterministic_for_same_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_CACHE_DIR", str(tmp_path))
    (tmp_path / "spec.txt").write_text("hello")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n")

    k1 = cache.compute_key("hello", str(repo), "python", "sonnet")
    k2 = cache.compute_key("hello", str(repo), "python", "sonnet")
    assert k1 == k2


def test_key_changes_when_spec_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x\n")
    k1 = cache.compute_key("alpha", str(repo), "python", "sonnet")
    k2 = cache.compute_key("beta",  str(repo), "python", "sonnet")
    assert k1 != k2


def test_key_changes_when_repo_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("v1\n")
    k1 = cache.compute_key("spec", str(repo), "python", "sonnet")
    (repo / "a.py").write_text("v2\n")
    k2 = cache.compute_key("spec", str(repo), "python", "sonnet")
    assert k1 != k2


def test_key_changes_when_language_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    k_py = cache.compute_key("spec", str(repo), "python", "sonnet")
    k_rs = cache.compute_key("spec", str(repo), "rust",   "sonnet")
    assert k_py != k_rs


def test_key_changes_when_model_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    k_a = cache.compute_key("spec", str(repo), "python", "sonnet")
    k_b = cache.compute_key("spec", str(repo), "python", "opus")
    assert k_a != k_b


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_CACHE_DIR", str(tmp_path))
    decomp = {"subtasks": [{"subtask_id": "a"}], "shared_files": {"t.py": "x"}}
    saved = cache.save("k1", decomp)
    assert saved is not None
    loaded = cache.load("k1")
    assert loaded == decomp


def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_CACHE_DIR", str(tmp_path))
    assert cache.load("nonexistent") is None


def test_disable_env_var_skips_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("BITSWARM_NO_CACHE", "1")
    assert cache.save("k", {"x": 1}) is None
    # Even if a file exists on disk, the load should be a no-op.
    (tmp_path / "k.json").write_text('{"x": 2}')
    assert cache.load("k") is None


def test_evict_removes_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_CACHE_DIR", str(tmp_path))
    cache.save("k", {"x": 1})
    assert cache.load("k") is not None
    assert cache.evict("k") is True
    assert cache.load("k") is None
    # Second evict on the same key is a no-op.
    assert cache.evict("k") is False


def test_skip_noisy_directories(tmp_path):
    """``.git`` / ``__pycache__`` / ``node_modules`` should not contribute
    to the hash, so adding files in them doesn't bust the cache."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("x")

    k_before = cache.compute_key("s", str(repo), "python", "sonnet")
    git = repo / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.cpython-311.pyc").write_text("garbage")
    k_after = cache.compute_key("s", str(repo), "python", "sonnet")
    assert k_before == k_after
