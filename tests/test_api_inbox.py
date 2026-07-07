"""
Task inbox lifecycle (validator/inbox.py), submission API
(validator/api.py), and the production hardening around them:
protocol version gating, timeout clamps, and bundle size limits.
"""
import asyncio
import json
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from validator import inbox as inbox_mod


GIT_ENV = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git_repo(path, files=None):
    os.makedirs(path, exist_ok=True)
    for name, content in (files or {"a.txt": "x\n"}).items():
        full = os.path.join(path, name)
        os.makedirs(os.path.dirname(full) or path, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path,
                    check=True, env=GIT_ENV)
    return path


# ---------------------------------------------------------------- inbox

def test_submit_and_status_lifecycle(tmp_path):
    inbox = str(tmp_path / "inbox")
    tid = inbox_mod.submit_task(inbox, spec="do things",
                                 target_repo="/tmp/x", mode="scaffold")
    task_file = os.path.join(inbox, f"{tid}.json")
    assert inbox_mod.task_status(inbox, tid) == "pending"
    with open(task_file) as f:
        doc = json.load(f)
    assert doc["task_id"] == tid
    assert doc["spec"] == "do things"

    os.rename(task_file, task_file + ".working")
    assert inbox_mod.task_status(inbox, tid) == "working"
    os.rename(task_file + ".working", task_file + ".done")
    assert inbox_mod.task_status(inbox, tid) == "done"
    assert inbox_mod.task_status(inbox, "nope") == "unknown"


def test_submit_rejects_bad_input(tmp_path):
    inbox = str(tmp_path)
    with pytest.raises(ValueError, match="mode"):
        inbox_mod.submit_task(inbox, spec="x", target_repo="/t",
                               mode="yolo")
    with pytest.raises(ValueError, match="spec"):
        inbox_mod.submit_task(inbox, spec="  ", target_repo="/t")


def test_recover_orphaned_requeues_working_only(tmp_path):
    inbox = str(tmp_path)
    for name in ("a.json", "b.json.working", "c.json.done",
                  "d.json.working"):
        with open(os.path.join(inbox, name), "w") as f:
            f.write("{}")
    recovered = inbox_mod.recover_orphaned(inbox)
    assert recovered == ["b", "d"]
    names = sorted(os.listdir(inbox))
    assert names == ["a.json", "b.json", "c.json.done", "d.json"]
    # Idempotent when nothing is stranded.
    assert inbox_mod.recover_orphaned(inbox) == []


def test_write_patch_artifact_covers_whole_change(tmp_path):
    repo = _git_repo(str(tmp_path / "merge_repo"))
    with open(os.path.join(repo, "feature.py"), "w") as f:
        f.write("def feature():\n    return 42\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "apply s1"], cwd=repo,
                    check=True, env=GIT_ENV)
    # A loose, uncommitted repair edit must be included too.
    with open(os.path.join(repo, "a.txt"), "a") as f:
        f.write("repaired\n")

    dest = str(tmp_path / "out" / "patch.diff")
    assert inbox_mod.write_patch_artifact(repo, dest) is True
    with open(dest) as f:
        patch = f.read()
    assert "feature.py" in patch
    assert "repaired" in patch


def test_load_result_and_patch_missing(tmp_path):
    assert inbox_mod.load_result(str(tmp_path), "t1") is None
    assert inbox_mod.load_patch(str(tmp_path), "t1") is None


# ------------------------------------------------------------------ API

@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("BITSWARM_API_KEYS", "k-good")
    monkeypatch.delenv("BITSWARM_API_ALLOW_ANON", raising=False)
    from validator.api import create_app
    inbox = str(tmp_path / "inbox")
    output = str(tmp_path / "output")
    app = create_app(inbox, output)
    client = TestClient(app)
    return types.SimpleNamespace(client=client, inbox=inbox,
                                  output=output, tmp=tmp_path)


def test_create_app_refuses_keyless(tmp_path, monkeypatch):
    monkeypatch.delenv("BITSWARM_API_KEYS", raising=False)
    monkeypatch.delenv("BITSWARM_API_ALLOW_ANON", raising=False)
    from validator.api import create_app
    with pytest.raises(RuntimeError, match="BITSWARM_API_KEYS"):
        create_app(str(tmp_path / "i"), str(tmp_path / "o"))


def test_anon_flag_allows_keyless(tmp_path, monkeypatch):
    monkeypatch.delenv("BITSWARM_API_KEYS", raising=False)
    monkeypatch.setenv("BITSWARM_API_ALLOW_ANON", "1")
    from validator.api import create_app
    client = TestClient(create_app(str(tmp_path / "i"), str(tmp_path / "o")))
    assert client.get("/health").status_code == 200


def test_auth_required(api):
    r = api.client.post("/tasks", json={"spec": "x", "target_repo": "/t"})
    assert r.status_code == 401
    r = api.client.post("/tasks", json={"spec": "x", "target_repo": "/t"},
                         headers={"X-API-Key": "k-wrong"})
    assert r.status_code == 401


def test_submit_with_target_repo(api, tmp_path):
    target = _git_repo(str(tmp_path / "target"))
    r = api.client.post(
        "/tasks",
        json={"spec": "add a feature", "mode": "diff",
              "target_repo": target},
        headers={"X-API-Key": "k-good"},
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]
    with open(os.path.join(api.inbox, f"{tid}.json")) as f:
        doc = json.load(f)
    assert doc["target_repo"] == target
    assert doc["mode"] == "diff"

    r = api.client.get(f"/tasks/{tid}", headers={"X-API-Key": "k-good"})
    assert r.json() == {"task_id": tid, "status": "pending"}


def test_submit_validation_errors(api):
    hdr = {"X-API-Key": "k-good"}
    assert api.client.post("/tasks", json={"spec": "x"},
                            headers=hdr).status_code == 400
    assert api.client.post(
        "/tasks", json={"spec": "x", "target_repo": "/a",
                         "repo_bundle": "b64"},
        headers=hdr).status_code == 400
    assert api.client.post(
        "/tasks", json={"spec": " ", "target_repo": "/a"},
        headers=hdr).status_code == 400
    assert api.client.post(
        "/tasks", json={"spec": "x", "mode": "yolo", "target_repo": "/a"},
        headers=hdr).status_code == 400
    assert api.client.post(
        "/tasks", json={"spec": "x", "target_repo": "/does/not/exist"},
        headers=hdr).status_code == 400


def test_submit_with_repo_bundle(api, tmp_path):
    from protocol.transport import bundle_repo
    source = _git_repo(str(tmp_path / "user_repo"),
                        files={"calc.py": "def add(a, b): return a + b\n"})
    bundle = bundle_repo(source)
    r = api.client.post(
        "/tasks",
        json={"spec": "add subtract()", "mode": "diff",
              "repo_bundle": bundle},
        headers={"X-API-Key": "k-good"},
    )
    assert r.status_code == 200
    tid = r.json()["task_id"]
    unbundled = os.path.join(api.output, "uploads", tid, "repo")
    assert os.path.isfile(os.path.join(unbundled, "calc.py"))
    with open(os.path.join(api.inbox, f"{tid}.json")) as f:
        assert json.load(f)["target_repo"] == unbundled


def test_status_done_attaches_result(api):
    tid = inbox_mod.submit_task(api.inbox, spec="x", target_repo="/t")
    base = os.path.join(api.inbox, f"{tid}.json")
    os.rename(base, base + ".done")
    result_dir = os.path.join(api.output, tid)
    os.makedirs(result_dir)
    with open(os.path.join(result_dir, "result.json"), "w") as f:
        json.dump({"task_id": tid, "total": 1.0}, f)

    r = api.client.get(f"/tasks/{tid}", headers={"X-API-Key": "k-good"})
    assert r.json()["status"] == "done"
    assert r.json()["result"]["total"] == 1.0

    missing = api.client.get("/tasks/nope",
                              headers={"X-API-Key": "k-good"})
    assert missing.status_code == 404


def test_patch_endpoint(api):
    tid = inbox_mod.submit_task(api.inbox, spec="x", target_repo="/t")
    hdr = {"X-API-Key": "k-good"}
    assert api.client.get(f"/tasks/{tid}/patch",
                           headers=hdr).status_code == 404
    result_dir = os.path.join(api.output, tid)
    os.makedirs(result_dir)
    with open(os.path.join(result_dir, "patch.diff"), "w") as f:
        f.write("diff --git a/calc.py b/calc.py\n")
    r = api.client.get(f"/tasks/{tid}/patch", headers=hdr)
    assert r.status_code == 200
    assert r.text.startswith("diff --git")


def test_health_counts(api):
    inbox_mod.submit_task(api.inbox, spec="x", target_repo="/t")
    tid2 = inbox_mod.submit_task(api.inbox, spec="y", target_repo="/t")
    base = os.path.join(api.inbox, f"{tid2}.json")
    os.rename(base, base + ".working")
    body = api.client.get("/health").json()
    assert body == {"status": "ok", "pending": 1, "working": 1}


# ------------------------------------------------- protocol hardening

def test_protocol_mismatch_rejected_before_work(tmp_path):
    from miner.runtime import run_assignment
    from protocol.schemas import TaskAssignment

    async def must_not_run(subtask, repo_path, **kwargs):
        raise AssertionError("agent must not run on version mismatch")

    task = TaskAssignment(protocol_version=99, task_id="t",
                           subtask_id="s1", repo_bundle="")
    resp = asyncio.run(run_assignment(task, must_not_run))
    assert resp.stop_reason == "protocol_mismatch"
    assert "v99" in resp.error_message


def test_timeout_clamped_to_miner_ceiling(tmp_path, monkeypatch):
    import miner.runtime as runtime
    from protocol.schemas import TaskAssignment
    from protocol.transport import bundle_repo

    repo = _git_repo(str(tmp_path / "repo"))
    monkeypatch.setattr(runtime, "MAX_TASK_SECONDS", 1)

    async def slow_execute(subtask, repo_path, **kwargs):
        await asyncio.sleep(3)
        return types.SimpleNamespace(
            patch="p", tests_passed=True, test_output="",
            iterations_used=1, stop_reason="tests_passed",
            files_modified=[])

    task = TaskAssignment(task_id="t", subtask_id="s1",
                           repo_bundle=bundle_repo(repo),
                           timeout_seconds=10_000_000)
    resp = asyncio.run(runtime.run_assignment(task, slow_execute))
    assert resp.stop_reason == "timeout"


def test_bundle_size_caps(tmp_path, monkeypatch):
    from protocol.transport import bundle_repo, unbundle_repo

    repo = _git_repo(str(tmp_path / "repo"),
                      files={"big.txt": "data " * 200_000})
    monkeypatch.setenv("BITSWARM_MAX_BUNDLE_MB", "0.0001")
    with pytest.raises(ValueError, match="BITSWARM_MAX_BUNDLE_MB"):
        bundle_repo(repo)
    with pytest.raises(ValueError, match="refusing to decode"):
        unbundle_repo("x" * 2000, str(tmp_path / "dest"))
