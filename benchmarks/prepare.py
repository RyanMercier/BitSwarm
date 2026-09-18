"""Prepare transparent synthetic tasks. Preparation never invokes a model.

Run from repository root: python benchmarks/prepare.py --out out/pilot-tasks
Then explicitly execute the generated manifest with bitswarm evaluate.
"""

import argparse
import json
from pathlib import Path

from harness.workspace import git

TASKS = [
    {
        "name": "interval-union",
        "spec": "Implement merge_intervals(intervals) for finite numeric closed intervals. "
        "Return sorted tuples merging overlapping or touching intervals transitively. "
        "Accept iterable input, preserve the caller's data, return [] for empty input, "
        "and raise ValueError for any interval whose start exceeds its end.",
        "source": "def merge_intervals(intervals):\n    return list(intervals)\n",
        "existing": "from solution import merge_intervals\n\ndef test_single():\n    assert merge_intervals([(1, 2)]) == [(1, 2)]\n",
        "contract": """from solution import merge_intervals
import pytest

@pytest.mark.parametrize("items,expected", [
    ([], []), ([(3, 4), (1, 2)], [(1, 2), (3, 4)]),
    ([(1, 4), (2, 3)], [(1, 4)]),
    ([(5, 7), (1, 3), (3, 5)], [(1, 7)]),
    ([(2, 2), (2, 2)], [(2, 2)]),
    ([(-3, -1), (-2, 0), (1.5, 2)], [(-3, 0), (1.5, 2)]),
])
def test_union(items, expected):
    before = list(items)
    assert merge_intervals(items) == expected
    assert items == before

def test_iterator():
    assert merge_intervals(iter([(2, 4), (1, 3)])) == [(1, 4)]

def test_invalid():
    with pytest.raises(ValueError):
        merge_intervals([(3, 2)])
""",
    },
    {
        "name": "dependency-order",
        "spec": "Implement dependency_order(graph), a mapping from string node names to "
        "iterables of prerequisite names. Return every key and prerequisite-only node "
        "exactly once, prerequisites before dependents. At each step choose the "
        "lexicographically smallest currently eligible node. Deduplicate edges, do not "
        "mutate inputs, return [] for empty input, and raise ValueError on any cycle "
        "including a disconnected cycle or self-edge.",
        "source": "def dependency_order(graph):\n    return sorted(graph)\n",
        "existing": "from solution import dependency_order\n\ndef test_single():\n    assert dependency_order({'a': []}) == ['a']\n",
        "contract": """from solution import dependency_order
import pytest

@pytest.mark.parametrize("graph,expected", [
    ({}, []), ({"a": ["z"]}, ["z", "a"]),
    ({"c": ["a"], "a": [], "b": []}, ["a", "b", "c"]),
    ({"a": ["b"], "c": [], "b": []}, ["b", "a", "c"]),
    ({"d": ["b", "c"], "c": ["a"], "b": ["a"]}, ["a", "b", "c", "d"]),
    ({"b": ["a", "a"]}, ["a", "b"]),
])
def test_order(graph, expected):
    before = {k: list(v) for k, v in graph.items()}
    assert dependency_order(graph) == expected
    assert graph == before

@pytest.mark.parametrize("graph", [{"a": ["a"]}, {"a": ["b"], "b": ["a"]},
    {"ok": [], "x": ["y"], "y": ["x"]}])
def test_cycle(graph):
    with pytest.raises(ValueError):
        dependency_order(graph)

def test_generator():
    assert dependency_order({"b": iter(["a", "a"])}) == ["a", "b"]
""",
    },
    {
        "name": "retry-delay",
        "spec": "Implement retry_delay(value, now, cap=60.0). Parse an HTTP Retry-After "
        "value as either ASCII decimal integer seconds (surrounding whitespace allowed) "
        "or an RFC HTTP date via email.utils.parsedate_to_datetime. now is an aware "
        "datetime. Return seconds clamped to [0, cap] as a float. Missing, malformed, "
        "negative, fractional or naive-date values return None. A nonfinite or negative "
        "cap raises ValueError even when value is missing. Past valid dates return 0.0. "
        "Do not read the wall clock; use the supplied now.",
        "source": "def retry_delay(value, now, cap=60.0):\n    return None\n",
        "existing": "from solution import retry_delay\nfrom datetime import datetime, timezone\n\ndef test_missing():\n    assert retry_delay(None, datetime(2026, 1, 1, tzinfo=timezone.utc)) is None\n",
        "contract": """from solution import retry_delay
from datetime import datetime, timezone
import pytest

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

@pytest.mark.parametrize("value,expected", [(" 12 ", 12.0), ("0", 0.0),
    ("999", 60.0), ("Thu, 01 Jan 2026 00:00:30 GMT", 30.0),
    ("Wed, 31 Dec 2025 23:59:59 GMT", 0.0)])
def test_valid(value, expected):
    actual = retry_delay(value, NOW)
    assert actual == expected
    assert isinstance(actual, float)

@pytest.mark.parametrize("value", [None, "", "-1", "1.5", "+2", "١٢", "nonsense",
    "Thu, 01 Jan 2026 00:00:30"])
def test_invalid(value):
    assert retry_delay(value, NOW) is None

@pytest.mark.parametrize("cap", [-1, float("inf"), float("nan")])
def test_bad_cap(cap):
    with pytest.raises(ValueError):
        retry_delay(None, NOW, cap)

def test_cap():
    assert retry_delay("4", NOW, 2.5) == 2.5
""",
    },
]


def prepare(output: Path, repeats=1, reverse_conditions=False):
    output.mkdir(parents=True, exist_ok=False)
    tasks = []
    for task in TASKS:
        root = output / task["name"]
        repo, contract = root / "repo", root / "contract"
        (repo / "tests").mkdir(parents=True)
        contract.mkdir()
        (repo / "solution.py").write_text(task["source"])
        (repo / "tests" / "test_existing.py").write_text(task["existing"])
        (contract / "test_contract.py").write_text(task["contract"])
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "Synthetic benchmark baseline")
        tasks.append(
            {
                "name": task["name"],
                "spec": task["spec"],
                "repo": str(repo.resolve()),
                "contract": str(contract.resolve()),
                "base": git(repo, "rev-parse", "HEAD").stdout.decode().strip(),
                "allowed": ["solution.py"],
            }
        )
    model = {"kind": "claude-cli", "model": "claude-fable-5-1", "call_budget": 1.0}
    manifest = {
        "repeats": repeats,
        "limits": {
            "max_turns": 12,
            "max_seconds": 600,
            "call_timeout": 120,
            "sandbox": "docker",
        },
        "tasks": tasks,
        "conditions": [
            {"name": "solo-fable", "config": {"implementer": model}},
            {
                "name": "reviewed-fable",
                "config": {"implementer": model, "reviewer": model},
            },
        ],
    }
    if reverse_conditions:
        manifest["conditions"].reverse()
    path = output / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--reverse-conditions", action="store_true")
    args = parser.parse_args()
    print(prepare(args.out, args.repeats, args.reverse_conditions))
