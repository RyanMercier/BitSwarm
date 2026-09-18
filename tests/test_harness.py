"""Actual Git/pytest integration, adversarial artifacts, and durable recovery."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from harness.demo import prepare
from harness.models import ClaudeCLI, OpenAICompatible, parse_action
from harness.report import report
from harness.runtime import Runtime, replay
from harness.store import Store
from harness.verify import Gate, Verifier
from harness.workspace import (
    PolicyError,
    apply_patch,
    safe_path,
)


@pytest.fixture
def run_dir(tmp_path):
    directory = tmp_path / "run"
    prepare(directory)
    return directory


def test_collaboration_resume_and_replay(run_dir):
    runtime = Runtime(run_dir)
    paused = runtime.run(steps=2)
    assert paused["phase"] == "implementer"
    assert paused["turns"] == 2
    state = Runtime(run_dir).run()
    assert state["phase"] == "passed"
    assert state["findings"][0]["problem"] == "Upper bound is ignored."
    assert state["result"]["gates"]["acceptance"]["outcome"] == "PASS"
    assert replay(run_dir)["outcome"] == "PASS"
    events = runtime.store.events()
    reviewer_prompt = next(
        e["data"]["prompt"]
        for e in events
        if e["kind"] == "model.requested" and e["data"]["role"] == "reviewer"
    )
    assert "Implemented lower bound." not in reviewer_prompt
    assert "test_contract" not in reviewer_prompt
    assert "Scripted offline" in report(run_dir).read_text()


def test_replay_detects_changed_patch(run_dir):
    Runtime(run_dir).run()
    (run_dir / "patch.diff").write_text("tampered")
    with pytest.raises(PolicyError, match="exported patch"):
        replay(run_dir)


def test_contract_mutation_rejected_on_resume(run_dir):
    Runtime(run_dir).run(1)
    (run_dir / "contract" / "test_contract.py").write_text("assert True")
    with pytest.raises(PolicyError, match="contract"):
        Runtime(run_dir).run()


def test_interrupted_call_not_repeated(run_dir):
    store = Store(run_dir)
    state = store.load()
    state["pending"] = {"kind": "model", "turn": 0}
    state["turns"] = 1
    store.save(state, "model.requested")
    state = Runtime(run_dir).run()
    assert state["phase"] == "inconclusive"
    assert state["turns"] == 1
    assert store.events()[-1]["kind"] == "recovery.uncertain"


def test_cancel_and_budget(run_dir):
    (run_dir / "CANCEL").touch()
    assert Runtime(run_dir).run()["phase"] == "cancelled"


def test_turn_budget(run_dir):
    store = Store(run_dir)
    state = store.load()
    state["config"]["max_turns"] = 1
    store.save(state, "test.config")
    assert Runtime(run_dir).run()["phase"] == "inconclusive"


def test_lock_excludes_second_runner(run_dir):
    with (
        Store(run_dir).lock(),
        pytest.raises(RuntimeError, match="another runner"),
        Store(run_dir).lock(),
    ):
        pass


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


def test_plain_diff_cannot_escape_scope(run_dir):
    root = run_dir / "candidate"
    old = (root / "bounds.py").read_text()
    patch = "--- a/bounds.py\n+++ b/bounds.py\n@@ -1,2 +1,2 @@\n def clamp(value, low, high):\n-    return value\n+    return 0\n"
    with pytest.raises(PolicyError, match="unauthorized"):
        apply_patch(root, patch, ["other.py"])
    assert (root / "bounds.py").read_text() == old


def test_reviewer_cannot_patch(run_dir):
    runtime = Runtime(run_dir)
    with pytest.raises(PolicyError, match="reviewer"):
        runtime.tool(
            runtime.store.load(), "reviewer", {"type": "patch", "patch": "anything"}
        )


def test_protected_test_paths(run_dir):
    with pytest.raises(PolicyError, match="protected"):
        apply_patch(run_dir / "candidate", "anything", ["tests/test_existing.py"])


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


def test_reviewer_turn_cap_falls_through_to_verification(run_dir):
    store = Store(run_dir)
    state = store.load()
    state["config"]["max_review_turns"] = 1
    store.save(state, "test.config")
    # The reviewer fixture reads once and reviews on its second turn; with a
    # cap of one it never submits findings, so the candidate is verified as-is.
    state = Runtime(run_dir).run()
    assert state["phase"] == "failed"
    assert state["findings"] == []
    assert any(e["kind"] == "review.budget_exhausted" for e in store.events())


def test_replay_records_environment_mismatch_instead_of_refusing(run_dir, monkeypatch):
    Runtime(run_dir).run()
    original = Verifier.environment
    monkeypatch.setattr(
        Verifier,
        "environment",
        lambda self: {**original(self), "verifier_hash": "changed"},
    )
    result = replay(run_dir)
    assert result["outcome"] == "PASS"
    assert result["environment_match"] is False
    assert result["original_outcome"] == "PASS"


def test_unknown_adapter_rejected_before_snapshot(run_dir, tmp_path):
    from harness.runtime import create_run

    target = tmp_path / "bad"
    with pytest.raises(ValueError, match="unknown adapter"):
        create_run(
            target,
            run_dir / "baseline",
            run_dir / "contract",
            "spec",
            ["bounds.py"],
            {"implementer": {"kind": "claude_cli"}, "sandbox": "unsafe-local"},
        )
    assert not (target / "baseline").exists()


def test_read_tool_handles_non_utf8_and_byte_offsets(run_dir):
    runtime = Runtime(run_dir)
    state = runtime.store.load()
    root = run_dir / "candidate"
    (root / "bounds.py").write_bytes("héllo\n".encode() + b"\xff\xfe tail\n")
    first = runtime.tool(state, "implementer", {"type": "read", "path": "bounds.py", "offset": 2})
    assert "tail" in first["content"]
    assert first["next_offset"] == len("héllo\n".encode()) + len(b"\xff\xfe tail\n")
    with pytest.raises(PolicyError, match="regular file"):
        runtime.tool(state, "implementer", {"type": "read", "path": "tests"})


def test_evaluate_resumes_without_rerunning_finished_runs(run_dir, tmp_path):
    from harness.evaluate import evaluate

    config = Store(run_dir).load()["config"]
    manifest = tmp_path / "experiment.json"
    manifest.write_text(
        json.dumps(
            {
                "limits": {"sandbox": "unsafe-local"},
                "tasks": [
                    {
                        "name": "clamp",
                        "repo": str(run_dir / "baseline"),
                        "contract": str(run_dir / "contract"),
                        "spec": "Clamp both bounds",
                        "allowed": ["bounds.py"],
                    }
                ],
                "conditions": [
                    {
                        "name": "review",
                        "config": {
                            "implementer": config["implementer"],
                            "reviewer": config["reviewer"],
                        },
                    }
                ],
            }
        )
    )
    out = tmp_path / "experiment"
    first = evaluate(manifest, out)
    finished = out / "task-0-condition-0-repeat-0"
    events_before = len(Store(finished).events())
    second = evaluate(manifest, out)
    assert first["conditions"] == second["conditions"]
    assert len(Store(finished).events()) == events_before
    manifest.write_text(manifest.read_text().replace("Clamp both", "Clamp all"))
    with pytest.raises(ValueError, match="different manifest"):
        evaluate(manifest, out)


def test_timeout_inconclusive(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_bad.py").write_text(
        "import time\ndef test_slow():\n    time.sleep(30)\n"
    )
    assert Verifier("unsafe-local", timeout=1).gate(tmp_path).outcome == "INCONCLUSIVE"


def test_missing_passing_test_fails(run_dir, monkeypatch):
    results = iter(
        [
            Gate("PASS", {"a": "passed", "b": "passed"}),
            Gate("PASS", {"a": "passed"}),
            Gate("PASS", {"contract": "passed"}),
        ]
    )
    verifier = Verifier("unsafe-local")
    monkeypatch.setattr(verifier, "gate", lambda *a: next(results))
    result = verifier.verify(
        run_dir / "baseline", run_dir / "candidate", run_dir / "contract"
    )
    assert result["outcome"] == "FAIL"
    assert result["lost_passing_tests"] == ["b"]


def test_missing_docker_never_falls_back(tmp_path, monkeypatch):
    verifier = Verifier("docker")
    monkeypatch.setattr(
        verifier, "environment", lambda: (_ for _ in ()).throw(OSError("no docker"))
    )
    assert verifier.gate(tmp_path).outcome == "INCONCLUSIVE"


def test_json_response_rejects_unsupported_actions():
    with pytest.raises(ValueError):
        parse_action('{"type":"shell","command":"bad"}')


def test_claude_adapter_disables_direct_tools_and_api_auth(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")

    def fake(cmd, **kwargs):
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert "--dangerously-skip-permissions" not in cmd
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"result": '{"type":"finish"}', "usage": {}, "total_cost_usd": 0.1}
            ),
        )

    monkeypatch.setattr(subprocess, "run", fake)
    assert ClaudeCLI().complete("prompt", "implementer", 0, 5).cost_usd == 0.1


def test_claude_auth_failure_not_success(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0, stdout='{"is_error":true,"result":"Not logged in"}'
        ),
    )
    with pytest.raises(RuntimeError, match="Not logged in"):
        ClaudeCLI().complete("prompt", "implementer", 0, 5)


def test_plain_http_remote_endpoint_rejected():
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatible("http://example.com/v1", "test")


def test_html_escapes_model_output(run_dir):
    Store(run_dir).event("test", {"output": "<script>alert(1)</script>"})
    text = report(run_dir).read_text()
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_evaluation_counts_failed_solo_and_successful_review(run_dir, tmp_path):
    from harness.evaluate import evaluate

    config = Store(run_dir).load()["config"]
    manifest = tmp_path / "experiment.json"
    manifest.write_text(
        json.dumps(
            {
                "repeats": 1,
                "limits": {"sandbox": "unsafe-local"},
                "tasks": [
                    {
                        "name": "clamp",
                        "repo": str(run_dir / "baseline"),
                        "contract": str(run_dir / "contract"),
                        "spec": "Clamp both bounds",
                        "allowed": ["bounds.py"],
                    }
                ],
                "conditions": [
                    {"name": "solo", "config": {"implementer": config["implementer"]}},
                    {
                        "name": "review",
                        "config": {
                            "implementer": config["implementer"],
                            "reviewer": config["reviewer"],
                        },
                    },
                ],
            }
        )
    )
    result = evaluate(manifest, tmp_path / "experiment")
    assert result["conditions"]["solo"]["passed"] == 0
    assert result["conditions"]["review"]["passed"] == 1
    assert result["conditions"]["solo"]["cost_per_accepted_task"] is None


def test_compatible_adapter_request_and_unknown_cost(monkeypatch):
    import urllib.request

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": '{"type":"finish"}'},
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()

    def fake(request, timeout):
        payload = json.loads(request.data)
        assert payload["model"] == "test-model" and payload["max_tokens"] == 4096
        assert request.full_url == "http://localhost:8000/v1/chat/completions"
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    result = OpenAICompatible("http://localhost:8000/v1", "test-model").complete(
        "prompt", "reviewer", 0, 5
    )
    assert result.action["type"] == "finish"
    assert result.cost_usd is None
    assert result.usage["prompt_tokens"] == 10


def test_verification_runs_when_finish_uses_last_turn(run_dir):
    store = Store(run_dir)
    state = store.load()
    state["config"]["max_turns"] = 8
    store.save(state, "test.budget")
    assert Runtime(run_dir).run()["phase"] == "passed"


def test_malformed_patch_is_recoverable_tool_feedback(run_dir):
    root = run_dir / "candidate"
    original = (root / "bounds.py").read_text()
    with pytest.raises(PolicyError, match="without modifying"):
        apply_patch(root, "not a patch", ["bounds.py"])
    assert (root / "bounds.py").read_text() == original
    store = Store(run_dir)
    script = run_dir / "implementer-fixture.json"
    actions = json.loads(script.read_text())
    actions["implementer"].insert(0, {"type": "patch", "patch": "not a patch"})
    script.write_text(json.dumps(actions))
    assert Runtime(run_dir).run()["phase"] == "passed"
    assert any("error" in e["data"].get("result", {}) for e in store.events())


def test_resume_rejects_changed_verifier_environment(run_dir, monkeypatch):
    Runtime(run_dir).run(steps=1)
    original = Verifier.environment
    monkeypatch.setattr(
        Verifier,
        "environment",
        lambda self: {**original(self), "verifier_hash": "changed"},
    )
    state = Runtime(run_dir).run()
    assert state["phase"] == "inconclusive"
    assert state["turns"] == 1
    assert Store(run_dir).events()[-1]["kind"] == "environment.changed"


def test_gate_level_failure_cannot_turn_into_overall_pass(run_dir, monkeypatch):
    # Mutation detection returns FAIL with a reason and no trustworthy test set.
    # The set-difference regression calculation must not treat this as success.
    results = iter(
        [
            Gate("FAIL", reason="test run mutated candidate source"),
            Gate("FAIL", reason="test run mutated candidate source"),
            Gate("PASS", {"acceptance::test_ok": "passed"}),
        ]
    )
    verifier = Verifier("unsafe-local")
    monkeypatch.setattr(verifier, "gate", lambda *args: next(results))
    result = verifier.verify(
        run_dir / "baseline", run_dir / "candidate", run_dir / "contract"
    )
    assert result["outcome"] != "PASS"


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
