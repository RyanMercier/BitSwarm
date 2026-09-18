"""Held-out acceptance contract for the harness itself.

The verify workflow reads this directory from the base branch, so a pull
request cannot rewrite the contract it is judged against. Keep these tests
about promises the harness makes to users, not implementation details.
"""

import pytest

from harness.models import parse_action
from harness.verify import MUTATION_EXIT, parse_report
from harness.workspace import normalize_diff_prefixes


def test_plain_unified_diff_headers_gain_git_prefixes():
    patch = "--- src/module.py\n+++ src/module.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
    normalized = normalize_diff_prefixes(patch)
    assert normalized.startswith("--- a/src/module.py\n+++ b/src/module.py\n")
    assert normalized.endswith("-a = 1\n+a = 2\n")


def test_git_style_headers_and_dev_null_are_left_alone():
    patch = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert normalize_diff_prefixes(patch) == patch


def test_removed_lines_starting_with_dashes_are_not_headers():
    patch = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1 @@\n---- not a header\n-y\n+z\n"
    assert normalize_diff_prefixes(patch) == patch


def test_only_supported_actions_are_accepted():
    assert parse_action('{"type":"finish","summary":"done"}')["type"] == "finish"
    with pytest.raises(ValueError):
        parse_action('{"type":"shell","command":"rm -rf /"}')
    with pytest.raises(ValueError):
        parse_action('{"type":"patch","patch":""}')


def test_mutated_source_is_inconclusive_not_a_pass(tmp_path):
    gate = parse_report(tmp_path / "missing.xml", MUTATION_EXIT, "")
    assert gate.outcome == "INCONCLUSIVE"
    assert "mutated" in gate.reason


def test_collection_failure_exit_code_is_inconclusive(tmp_path):
    assert parse_report(tmp_path / "missing.xml", 2, "").outcome == "INCONCLUSIVE"


def test_per_test_errors_are_recorded_per_test(tmp_path):
    xml = tmp_path / "report.xml"
    xml.write_text(
        '<testsuite><testcase classname="t" name="ok"/>'
        '<testcase classname="t" name="broken"><error message="fixture"/></testcase>'
        "</testsuite>"
    )
    gate = parse_report(xml, 1, "")
    assert gate.outcome == "FAIL"
    assert gate.tests == {"t::ok": "passed", "t::broken": "error"}


def test_exit_code_and_report_must_agree(tmp_path):
    xml = tmp_path / "report.xml"
    xml.write_text('<testsuite><testcase classname="t" name="ok"/></testsuite>')
    assert parse_report(xml, 1, "").outcome == "INCONCLUSIVE"
    assert parse_report(xml, 0, "").outcome == "PASS"
