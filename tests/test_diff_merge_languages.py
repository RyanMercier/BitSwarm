"""
Language-generic merge gates (validator/diff_merge.py).

The additive and regression gates dispatch on the repo's detected
build system, mirroring the miner-side hermetic replay. These tests
cover the dispatch seams and the per-runner failure parsers with
canned output, so nothing beyond Python is required to run them.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validator import diff_merge
from validator.diff_merge import (
    SUITE_SENTINEL,
    _parse_cargo_failures,
    _parse_ctest_failures,
    _parse_dotnet_failures,
    _parse_js_json_failures,
    _parse_junit_xml_failures,
    _stash_paths,
    collect_failing_tests,
    run_gate_tests,
)


def _proc(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------- parsers

def test_cargo_failure_parse():
    out = (
        "running 3 tests\n"
        "test tests::test_add ... ok\n"
        "test tests::test_div ... FAILED\n"
        "test integration::test_flow ... FAILED\n"
        "\n"
        "failures:\n"
        "    tests::test_div\n"
        "test result: FAILED. 1 passed; 2 failed\n"
    )
    assert _parse_cargo_failures(out) == {
        "tests::test_div", "integration::test_flow",
    }


def test_cargo_parse_ignores_ok_and_ignored():
    out = (
        "test a::b ... ok\n"
        "test a::c ... ignored\n"
    )
    assert _parse_cargo_failures(out) == set()


def test_dotnet_failure_parse():
    out = (
        "  Determining projects to restore...\n"
        "  Failed Calc.Tests.DivTests.DividesByZero [12 ms]\n"
        "  Passed Calc.Tests.AddTests.AddsTwo [1 ms]\n"
        "Failed!  - Failed:     1, Passed:     1, Skipped:     0\n"
    )
    assert _parse_dotnet_failures(out) == {
        "Calc.Tests.DivTests.DividesByZero",
    }


def test_ctest_failure_parse():
    out = (
        "Test project /build\n"
        "    Start 1: test_words\n"
        "1/2 Test #1: test_words .......   Passed    0.01 sec\n"
        "2/2 Test #2: test_scorer ......***Failed    0.02 sec\n"
        "\n"
        "The following tests FAILED:\n"
        "          2 - test_scorer (Failed)\n"
        "          3 - test_render (Timeout)\n"
        "Errors while running CTest\n"
    )
    assert _parse_ctest_failures(out) == {"test_scorer", "test_render"}


def test_js_json_failure_parse():
    blob = (
        'npm noise line\n'
        '{"numTotalTests": 2, "testResults": [{"name": "calc.test.ts", '
        '"assertionResults": ['
        '{"status": "passed", "fullName": "calc adds"},'
        '{"status": "failed", "fullName": "calc divides"}'
        ']}]}'
    )
    assert _parse_js_json_failures(blob) == {"calc divides"}


def test_js_json_parse_returns_none_without_json():
    assert _parse_js_json_failures("not json at all") is None


def test_junit_xml_failure_parse(tmp_path):
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-com.foo.CalcTest.xml").write_text(
        '<?xml version="1.0"?>\n'
        '<testsuite name="com.foo.CalcTest" tests="2" failures="1">\n'
        '  <testcase classname="com.foo.CalcTest" name="testAdd"/>\n'
        '  <testcase classname="com.foo.CalcTest" name="testDiv">\n'
        '    <failure message="expected 2 but was 3"/>\n'
        '  </testcase>\n'
        '</testsuite>\n'
    )
    assert _parse_junit_xml_failures(str(tmp_path)) == {
        "com.foo.CalcTest#testDiv",
    }


def test_junit_xml_none_when_no_reports(tmp_path):
    assert _parse_junit_xml_failures(str(tmp_path)) is None


# ------------------------------------------------------------ stash paths

def test_stash_paths_roundtrip(tmp_path):
    f1 = tmp_path / "tests" / "test_new.py"
    f1.parent.mkdir()
    f1.write_text("gate test content")
    keep = tmp_path / "tests" / "test_old.py"
    keep.write_text("existing test")

    with _stash_paths(str(tmp_path), ["tests/test_new.py", "missing.py"]):
        assert not f1.exists()
        assert keep.exists()
    assert f1.read_text() == "gate test content"


# ------------------------------------------- collect_failing_tests dispatch

def test_collect_pytest_repo_delegates_to_nodeids(tmp_path, monkeypatch):
    calls = {}

    def fake_nodeids(repo, files, timeout=600):
        calls["args"] = (repo, files)
        return {"tests/test_a.py::test_x"}, "out"

    monkeypatch.setattr(diff_merge, "collect_failing_nodeids", fake_nodeids)
    failing, _ = collect_failing_tests(
        str(tmp_path), ["tests/test_a.py"], exclude_paths=[],
    )
    assert failing == {"tests/test_a.py::test_x"}
    assert calls["args"] == (str(tmp_path), ["tests/test_a.py"])


def test_collect_cargo_repo_parses_failures(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")

    def fake_run(cmd, **kwargs):
        assert cmd[:2] == ["cargo", "test"]
        return _proc(101, "test tests::test_div ... FAILED\n")

    monkeypatch.setattr(diff_merge.subprocess, "run", fake_run)
    failing, _ = collect_failing_tests(str(tmp_path))
    assert failing == {"tests::test_div"}


def test_collect_sentinel_on_unparseable_failure(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")

    def fake_run(cmd, **kwargs):
        return _proc(101, "error[E0308]: mismatched types\n"
                          "error: could not compile `demo`\n")

    monkeypatch.setattr(diff_merge.subprocess, "run", fake_run)
    failing, _ = collect_failing_tests(str(tmp_path))
    assert failing == {SUITE_SENTINEL}


def test_collect_green_suite_returns_empty(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
    monkeypatch.setattr(diff_merge.subprocess, "run",
                         lambda cmd, **kw: _proc(0, "test result: ok."))
    failing, _ = collect_failing_tests(str(tmp_path))
    assert failing == set()


def test_collect_stashes_gate_tests_during_suite_run(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
    gate = tmp_path / "tests" / "test_gate.rs"
    gate.parent.mkdir()
    gate.write_text("#[test] fn gate() {}")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["gate_present_during_run"] = gate.exists()
        return _proc(0, "")

    monkeypatch.setattr(diff_merge.subprocess, "run", fake_run)
    collect_failing_tests(str(tmp_path),
                           exclude_paths=["tests/test_gate.rs"])
    assert seen["gate_present_during_run"] is False
    assert gate.exists()


def test_collect_compile_only_project_has_no_suite(tmp_path):
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n")
    failing, msg = collect_failing_tests(str(tmp_path))
    assert failing == set()
    assert "no suite runner" in msg


# ------------------------------------------------- run_gate_tests dispatch

def test_gate_pytest_repo_uses_batch_path(tmp_path, monkeypatch):
    calls = {}

    def fake_batch(repo, files, timeout=300):
        calls["args"] = (repo, files)
        return True, "ok"

    monkeypatch.setattr(diff_merge, "run_pytest_files", fake_batch)
    passed, _ = run_gate_tests(str(tmp_path), ["tests/test_a.py"])
    assert passed is True
    assert calls["args"] == (str(tmp_path), ["tests/test_a.py"])


def test_gate_non_pytest_repo_dispatches_run_test(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
    calls = []

    def fake_run_test(test_file, repo_root, timeout=60, extra_env=None):
        calls.append((test_file, repo_root, extra_env))
        return _proc(0, "test result: ok.")

    monkeypatch.setattr(diff_merge, "run_test", fake_run_test)
    passed, out = run_gate_tests(
        str(tmp_path), ["tests/test_a.rs", "tests/test_b.rs"],
    )
    assert passed is True
    assert len(calls) == 2
    assert calls[0][0] == "tests/test_a.rs"
    assert calls[0][2]["PYTHONNOUSERSITE"] == "1"
    assert "tests/test_b.rs" in out


def test_gate_non_pytest_failure_propagates(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'demo'\n")
    monkeypatch.setattr(
        diff_merge, "run_test",
        lambda tf, rr, timeout=60, extra_env=None: _proc(101, "FAILED"),
    )
    passed, _ = run_gate_tests(str(tmp_path), ["tests/test_a.rs"])
    assert passed is False


def test_gate_empty_test_list_passes(tmp_path):
    passed, msg = run_gate_tests(str(tmp_path), [])
    assert passed is True
    assert "no tests" in msg
