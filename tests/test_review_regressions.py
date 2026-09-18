import asyncio
from types import SimpleNamespace

from validator.diff_merge import (
    SUITE_SENTINEL,
    apply_patches_in_order,
    collect_failing_nodeids,
)
from validator.merge import validate_patch_scope

PATCH = "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"


def test_real_pytest_failure_is_not_empty_set(tmp_path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    failures, _output = collect_failing_nodeids(str(tmp_path), ["test_bad.py"])
    assert failures and any("test_bad" in name for name in failures)


def test_collection_error_is_infrastructure_failure(tmp_path):
    (tmp_path / "test_bad.py").write_text("import missing_module_xyz")
    failures, _ = collect_failing_nodeids(str(tmp_path), ["test_bad.py"])
    assert SUITE_SENTINEL in failures


def test_plain_unified_diff_scope_checked():
    assert validate_patch_scope(PATCH, ["allowed.py"]) == ["other.py"]


def test_traditional_diff_in_subdirectory_keeps_path():
    patch = "--- src/pkg/mod.py\n+++ src/pkg/mod.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    assert validate_patch_scope(patch, ["src/pkg/mod.py"]) == []
    assert validate_patch_scope(patch, ["mod.py"]) == ["src/pkg/mod.py"]


def test_fixture_error_stays_per_test_not_suite_sentinel(tmp_path):
    (tmp_path / "test_mixed.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    raise RuntimeError('setup')\n"
        "def test_uses_broken(broken):\n"
        "    pass\n"
        "def test_ok():\n"
        "    pass\n"
        "def test_fails():\n"
        "    assert False\n"
    )
    failures, _ = collect_failing_nodeids(str(tmp_path), ["test_mixed.py"])
    assert SUITE_SENTINEL not in failures
    assert {n.rsplit("::", 1)[1] for n in failures} == {"test_uses_broken", "test_fails"}


def test_diff_scope_violation_never_applied(tmp_path):
    from harness.workspace import git

    git(tmp_path, "init", "-q")
    (tmp_path / "other.py").write_text("value = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    applied = apply_patches_in_order(
        {"subtasks": [{"subtask_id": "a", "modify_files": ["allowed.py"]}]},
        {"a": SimpleNamespace(patch=PATCH)},
        str(tmp_path),
        str(tmp_path / "patches"),
    )
    assert applied == {"a": False}
    assert (tmp_path / "other.py").read_text() == "value = 1\n"


def test_failed_patch_receives_no_credit(tmp_path, monkeypatch):
    from validator import diff_merge as dm

    monkeypatch.setattr(dm, "apply_patches_in_order", lambda *a, **kw: {"a": False})
    monkeypatch.setattr(dm, "find_diff_baseline", lambda *a: None)
    monkeypatch.setattr(dm, "run_gate_tests", lambda *a, **kw: (True, "pass"))
    monkeypatch.setattr(dm, "collect_failing_tests", lambda *a, **kw: (set(), "pass"))
    base = tmp_path / "base"
    base.mkdir()
    result = asyncio.run(
        dm.merge_and_test_diff(
            {
                "subtasks": [
                    {"subtask_id": "a", "complexity_weight": 1, "new_test_files": []}
                ]
            },
            {"a": SimpleNamespace(patch="invalid nonempty patch")},
            str(base),
            str(tmp_path / "out"),
        )
    )
    assert result["scores"]["a"] == 0
