"""miner.runtime.run_assignment without any transport.

Moved verbatim from test_chain_layer.py when the redteam branch cut
the Bittensor layer that file also covered.
"""
import asyncio
import os
import subprocess
import types

from protocol.schemas import TaskAssignment


def test_run_assignment_end_to_end(tmp_path):
    """run_assignment unbundles the repo, invokes the injected agent,
    and shapes the MinerResponse, without any transport."""
    from miner.runtime import run_assignment
    from protocol.transport import bundle_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text("print('hi')\n")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                    check=True, env=env)

    seen = {}

    async def fake_execute(subtask, repo_path, **kwargs):
        seen["subtask_id"] = subtask["subtask_id"]
        seen["repo_exists"] = os.path.isfile(
            os.path.join(repo_path, "hello.py"))
        seen["mode"] = kwargs.get("mode", "scaffold")
        return types.SimpleNamespace(
            patch="diff --git a/x b/x", tests_passed=True,
            test_output="1 passed", iterations_used=2,
            stop_reason="tests_passed", files_modified=["hello.py"],
        )

    task = TaskAssignment(
        task_id="t", subtask_id="s1",
        repo_bundle=bundle_repo(str(repo)),
        allowed_files=["hello.py"], timeout_seconds=30,
        mode="diff", target_stubs={"hello.py": "..."},
    )
    resp = asyncio.run(run_assignment(task, fake_execute))
    assert seen["repo_exists"] is True
    assert seen["mode"] == "diff"
    assert resp.stub_tests_passed is True
    assert resp.patch.startswith("diff --git")
    assert resp.stop_reason == "tests_passed"
    assert resp.execution_time_seconds > 0


def test_run_assignment_reports_agent_crash(tmp_path):
    from miner.runtime import run_assignment
    from protocol.transport import bundle_repo

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("x")
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo,
                    check=True, env=env)

    async def exploding_execute(subtask, repo_path, **kwargs):
        raise RuntimeError("agent exploded")

    task = TaskAssignment(task_id="t", subtask_id="s1",
                           repo_bundle=bundle_repo(str(repo)))
    resp = asyncio.run(run_assignment(task, exploding_execute))
    assert resp.stub_tests_passed is False
    assert resp.stop_reason == "error"
    assert "agent exploded" in resp.error_message

