"""Credential-free, explicitly scripted collaboration and repair example."""

import json
from pathlib import Path

from harness.runtime import create_run
from harness.workspace import git


def prepare(directory: Path, sandbox="unsafe-local"):
    fixture = directory.parent / (directory.name + "-fixture")
    fixture.mkdir(parents=True, exist_ok=False)
    repo = fixture / "repo"
    repo.mkdir()
    (repo / "numbers.py").write_text("def clamp(value, low, high):\n    return value\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_existing.py").write_text(
        "from numbers import clamp\n\ndef test_inside():\n    assert clamp(3, 0, 10) == 3\n"
    )
    # Avoid shadowing Python's standard library numbers module.
    (repo / "numbers.py").rename(repo / "bounds.py")
    test = repo / "tests" / "test_existing.py"
    test.write_text(test.read_text().replace("from numbers", "from bounds"))
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "demo baseline")
    contract = fixture / "contract"
    contract.mkdir()
    (contract / "test_contract.py").write_text(
        "from bounds import clamp\n\ndef test_low():\n    assert clamp(-2, 0, 10) == 0\n"
        "\ndef test_high():\n    assert clamp(12, 0, 10) == 10\n"
        "\ndef test_inside():\n    assert clamp(3, 0, 10) == 3\n"
    )
    first = "--- a/bounds.py\n+++ b/bounds.py\n@@ -1,2 +1,2 @@\n def clamp(value, low, high):\n-    return value\n+    return max(value, low)\n"
    repair = "--- a/bounds.py\n+++ b/bounds.py\n@@ -1,2 +1,2 @@\n def clamp(value, low, high):\n-    return max(value, low)\n+    return min(max(value, low), high)\n"
    script = fixture / "actions.json"
    script.write_text(
        json.dumps(
            {
                "implementer": [
                    {"type": "read", "path": "bounds.py"},
                    {"type": "patch", "patch": first},
                    {"type": "test"},
                    {"type": "finish", "summary": "Implemented lower bound."},
                ],
                "reviewer": [
                    {"type": "read", "path": "bounds.py"},
                    {
                        "type": "review",
                        "findings": [
                            {
                                "path": "bounds.py",
                                "problem": "Upper bound is ignored.",
                                "evidence": "clamp(12, 0, 10) returns 12 rather than 10.",
                            }
                        ],
                    },
                ],
                "repair": [
                    {"type": "patch", "patch": repair},
                    {"type": "finish", "summary": "Fixed upper bound."},
                ],
            }
        )
    )
    adapter = {"kind": "fixture", "path": str(script.resolve())}
    return create_run(
        directory,
        repo,
        contract,
        "Clamp values to inclusive low/high bounds.",
        ["bounds.py"],
        {"implementer": dict(adapter), "reviewer": dict(adapter), "sandbox": sandbox},
    )
