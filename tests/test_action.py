"""The GitHub Action path: patch from two commits, contract from a ref, verdict."""

import json
import os
from pathlib import Path

import pytest

from harness.action import (
    export_contract,
    main,
    scope,
    summary_markdown,
    verify_pull_request,
)
from harness.workspace import git

CONFIG = {"sandbox": "unsafe-local"}


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "acceptance").mkdir()
    (root / "bounds.py").write_text("def clamp(value, low, high):\n    return value\n")
    (root / "tests" / "test_existing.py").write_text(
        "from bounds import clamp\n\ndef test_inside():\n    assert clamp(3, 0, 10) == 3\n"
    )
    (root / "acceptance" / "test_contract.py").write_text(
        "from bounds import clamp\n\ndef test_low():\n    assert clamp(-2, 0, 10) == 0\n"
        "\ndef test_high():\n    assert clamp(12, 0, 10) == 10\n"
    )
    git(root, "init", "-q")
    base = commit(root, "base")
    return root, base


def test_scope_matching_uses_fnmatch_across_directories():
    changed = ["src/a/b.py", "src/c.py", "docs/x.md"]
    allowed, outside = scope(changed, ["src/*.py"])
    assert allowed == ["src/a/b.py", "src/c.py"]
    assert outside == ["docs/x.md"]
    assert scope(changed, ["*"]) == (changed, [])


def test_contract_is_read_from_the_ref_not_the_working_tree(repo, tmp_path):
    root, base = repo
    (root / "acceptance" / "test_contract.py").write_text("def test_tampered():\n    assert False\n")
    contract = export_contract(root, base, "acceptance", tmp_path / "export")
    assert "test_high" in (contract / "test_contract.py").read_text()
    with pytest.raises(ValueError, match="no acceptance directory"):
        export_contract(root, base, "missing", tmp_path / "export2")


def test_passing_pull_request(repo, tmp_path):
    root, base = repo
    (root / "bounds.py").write_text(
        "def clamp(value, low, high):\n    return min(max(value, low), high)\n"
    )
    head = commit(root, "fix clamp")
    result = verify_pull_request(root, base, head, "acceptance", None, ["*"], tmp_path / "out", dict(CONFIG))
    assert result["outcome"] == "PASS", result
    assert result["changed_paths"] == ["bounds.py"]
    assert (tmp_path / "out" / "patch.diff").read_text().startswith("diff --git")
    assert (tmp_path / "out" / "report.html").exists()
    text = summary_markdown(result)
    assert "PASS" in text and "acceptance | PASS | 2 passed" in text


def test_failing_pull_request_lists_acceptance_failures(repo, tmp_path):
    root, base = repo
    (root / "bounds.py").write_text("def clamp(value, low, high):\n    return max(value, low)\n")
    head = commit(root, "half fix")
    result = verify_pull_request(root, base, head, "acceptance", None, ["*"], tmp_path / "out", dict(CONFIG))
    assert result["outcome"] == "FAIL"
    assert "test_contract::test_high" in summary_markdown(result)


def test_touching_tests_is_a_fail_verdict(repo, tmp_path):
    root, base = repo
    (root / "bounds.py").write_text(
        "def clamp(value, low, high):\n    return min(max(value, low), high)\n"
    )
    (root / "tests" / "test_existing.py").write_text("def test_inside():\n    assert True\n")
    head = commit(root, "weaken tests")
    result = verify_pull_request(root, base, head, "acceptance", None, ["*"], tmp_path / "out", dict(CONFIG))
    assert result["outcome"] == "FAIL"
    assert "protected path" in result["reason"]
    assert (tmp_path / "out" / "patch.diff").exists()


def test_protection_off_still_holds_out_the_contract(repo, tmp_path):
    root, base = repo
    (root / "bounds.py").write_text("def clamp(value, low, high):\n    return max(value, low)\n")
    (root / "tests" / "test_existing.py").write_text("def test_inside():\n    assert True\n")
    (root / "acceptance" / "test_contract.py").write_text("def test_easy():\n    assert True\n")
    head = commit(root, "weaken everything")
    result = verify_pull_request(
        root, base, head, "acceptance", None, ["*"], tmp_path / "out", dict(CONFIG),
        protect_tests=False,
    )
    # The weakened existing test is accepted; the weakened contract is not,
    # because the contract came from base, where test_high still exists.
    assert result["outcome"] == "FAIL"
    acceptance = result["gates"]["acceptance"]["tests"]
    # Test identities carry pytest's rootdir-relative module path.
    assert acceptance[next(k for k in acceptance if k.endswith("::test_high"))] == "failed"
    assert "test protection off" in summary_markdown(result)
    # Replay re-applies the patch under the policy the verdict was made with.
    from harness.runtime import replay

    again = replay(tmp_path / "out")
    assert again["outcome"] == "FAIL" and again["environment_match"] is True


def test_paths_outside_allow_list_fail_before_any_gate(repo, tmp_path):
    root, base = repo
    (root / "README.md").write_text("docs\n")
    head = commit(root, "docs")
    result = verify_pull_request(root, base, head, "acceptance", None, ["*.py"], tmp_path / "out", dict(CONFIG))
    assert result["outcome"] == "FAIL"
    assert result["unauthorized_paths"] == ["README.md"]
    assert not (tmp_path / "out").exists()


def test_no_changes_is_inconclusive(repo, tmp_path):
    root, base = repo
    result = verify_pull_request(root, base, base, "acceptance", None, ["*"], tmp_path / "out", dict(CONFIG))
    assert result["outcome"] == "INCONCLUSIVE"


def test_missing_contract_is_inconclusive(repo, tmp_path):
    root, base = repo
    (root / "bounds.py").write_text("x = 1\n")
    head = commit(root, "change")
    result = verify_pull_request(root, base, head, "nowhere", None, ["*"], tmp_path / "out", dict(CONFIG))
    assert result["outcome"] == "INCONCLUSIVE"
    assert "no acceptance directory" in result["reason"]


def test_main_writes_github_outputs_and_exit_codes(repo, tmp_path, monkeypatch):
    root, base = repo
    (root / "bounds.py").write_text(
        "def clamp(value, low, high):\n    return min(max(value, low), high)\n"
    )
    head = commit(root, "fix clamp")
    outputs, summary = tmp_path / "outputs", tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = main(
        [
            "--repo", str(root), "--base", base, "--head", head,
            "--out", str(tmp_path / "out"), "--sandbox", "unsafe-local",
        ]
    )
    assert code == 0
    assert "outcome=PASS" in outputs.read_text()
    assert "BitSwarm verify" in summary.read_text()
    action = json.loads((tmp_path / "out" / "action.json").read_text())
    assert action["patch_hash"] and action["gates"]["acceptance"]["outcome"] == "PASS"
    assert main(
        ["--repo", str(root), "--base", base, "--head", base, "--out", str(tmp_path / "out2"),
         "--sandbox", "unsafe-local", "--fail-on-inconclusive", "false"]
    ) == 0
    assert main(
        ["--repo", str(root), "--base", base, "--head", base, "--out", str(tmp_path / "out3"),
         "--sandbox", "unsafe-local"]
    ) == 1
    assert os.environ["GITHUB_OUTPUT"] == str(outputs)
