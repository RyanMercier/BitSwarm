"""harness.verify and harness.workspace, the parts the grader imports.

Moved verbatim from test_harness.py when the redteam branch cut the
rest of the harness product. Tests that needed the cut harness.demo
fixture were dropped with it.
"""
import pytest

from harness.verify import Verifier
from harness.workspace import (
    PolicyError,
    apply_patch,
    safe_path,
)


@pytest.mark.parametrize(
    "relative", ["../escape", "/absolute", ".git/config", "x\\y", "C:foo"]
)
def test_paths_reject_escape(tmp_path, relative):
    with pytest.raises(PolicyError):
        safe_path(tmp_path, relative)


def test_symlink_rejected(tmp_path):
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(PolicyError):
        safe_path(tmp_path, "link/file")


def test_actual_failure_detected(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n"
    )
    gate = Verifier("unsafe-local").gate(tmp_path)
    assert gate.outcome == "FAIL"
    assert "failed" in gate.tests.values()


def test_empty_suite_inconclusive(tmp_path):
    (tmp_path / "tests").mkdir()
    assert Verifier("unsafe-local").gate(tmp_path).outcome == "INCONCLUSIVE"


def test_collection_error_inconclusive(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        "import module_that_does_not_exist_xyz"
    )
    assert Verifier("unsafe-local").gate(tmp_path).outcome == "INCONCLUSIVE"


def test_mutating_test_rejected(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        'from pathlib import Path\ndef test_mutation():\n    Path("new.py").write_text("x")\n'
    )
    gate = Verifier("unsafe-local").gate(tmp_path)
    # Same classification as the container worker: the report is untrusted.
    assert gate.outcome == "INCONCLUSIVE" and "mutated" in gate.reason
    assert not (tmp_path / "new.py").exists()


def test_fixture_error_is_per_test_evidence_not_inconclusive(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mixed.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken():\n"
        "    raise RuntimeError('setup')\n"
        "def test_uses_broken(broken):\n"
        "    pass\n"
        "def test_ok():\n"
        "    pass\n"
    )
    gate = Verifier("unsafe-local").gate(tmp_path)
    assert gate.outcome == "FAIL"
    assert sorted(gate.tests.values()) == ["error", "passed"]


def test_traditional_diff_paths_keep_their_directory(tmp_path):
    from harness.workspace import git, normalize_diff_prefixes

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("value = 1\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    plain = (
        "--- src/module.py\n+++ src/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    )
    assert normalize_diff_prefixes(plain).startswith("--- a/src/module.py\n+++ b/src/")
    assert apply_patch(root, plain, ["src/module.py"]) == ["src/module.py"]
    assert (root / "src" / "module.py").read_text() == "value = 2\n"


def test_normalize_leaves_hunk_bodies_and_dev_null_alone():
    from harness.workspace import normalize_diff_prefixes

    patch = (
        "--- /dev/null\n+++ new.py\n@@ -0,0 +1,2 @@\n+--- not a header\n+x = 1\n"
        "--- a/old.py\n+++ b/old.py\n@@ -1,2 +1 @@\n---- removed\n-y = 2\n+z\n"
    )
    out = normalize_diff_prefixes(patch)
    assert out.startswith("--- /dev/null\n+++ b/new.py\n")
    assert "+--- not a header\n" in out
    assert "---- removed\n" in out
    assert out.count("--- a/old.py") == 1


def test_timeout_inconclusive(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        "import time\ndef test_slow():\n    time.sleep(30)\n"
    )
    assert Verifier("unsafe-local", timeout=1).gate(tmp_path).outcome == "INCONCLUSIVE"


def test_skips_are_recorded_not_failed(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_skip.py").write_text(
        "import pytest\n\n@pytest.mark.skip\ndef test_later():\n    pass\n\ndef test_ok():\n    pass\n"
    )
    gate = Verifier("unsafe-local").gate(tmp_path)
    assert gate.outcome == "PASS"
    assert sorted(gate.tests.values()) == ["passed", "skipped"]


def test_missing_docker_never_falls_back(tmp_path, monkeypatch):
    verifier = Verifier("docker")
    monkeypatch.setattr(
        verifier, "environment", lambda: (_ for _ in ()).throw(OSError("no docker"))
    )
    assert verifier.gate(tmp_path).outcome == "INCONCLUSIVE"


def test_docker_large_report_and_consistent_client_environment(tmp_path, monkeypatch):
    import subprocess

    import harness.verify as module

    source = tmp_path / "source"
    source.mkdir()
    (source / "example.py").write_text("value = 1\n")
    monkeypatch.setenv("DOCKER_HOST", "unix:///custom/docker.sock")
    verifier = Verifier("docker")
    monkeypatch.setenv("DOCKER_HOST", "unix:///changed/docker.sock")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1])
        assert kwargs["env"] is verifier.docker_env
        assert kwargs["env"]["DOCKER_HOST"] == "unix:///custom/docker.sock"
        return subprocess.CompletedProcess(cmd, 0, stdout="sha256:test\n")

    def fake_process(cmd, cwd, env, timeout, log_path):
        calls.append(cmd[1])
        assert env is verifier.docker_env
        cases = "".join(
            f'<testcase classname="large_suite" name="test_case_{i:05d}" />'
            for i in range(500)
        )
        output = (
            "\nBITSWARM_JUNIT_BEGIN\n<testsuite>"
            + cases
            + "</testsuite>\nBITSWARM_JUNIT_END\n"
        )
        assert len(output) > 16000
        log_path.write_text(output)
        return 0, output[-16000:]

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "run_process", fake_process)
    gate = verifier.gate(source)
    assert gate.outcome == "PASS"
    assert len(gate.tests) == 500
    assert len(gate.output) == 16000
    assert calls == ["image", "run", "rm"]

