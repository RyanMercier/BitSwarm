"""Post-hoc audit, kept separate from the original frozen acceptance results."""

import argparse
import json
from pathlib import Path

from prepare import prepare

from harness.runtime import write_json
from harness.verify import Verifier

RETRY_PROBES = """

@pytest.mark.parametrize("value,cap,expected", [
    ("0" * 5000, 60.0, 0.0),
    ("0" * 5000 + "1", 60.0, 1.0),
    ("9" * 5000, 60.0, 60.0),
    ("9" * 400, 60.0, 60.0),
    ("1" + "0" * 24, 1e308, 1e24),
], ids=["many-zeroes", "leading-zeroes", "huge-integer", "float-overflow", "large-cap"])
def test_large_numeric_values(value, cap, expected):
    assert retry_delay(value, NOW, cap) == expected
"""


def audit(pilot: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = prepare(output / "tasks", reverse_conditions=True)
    manifest = json.loads(manifest_path.read_text())
    manifest["tasks"] = [
        task for task in manifest["tasks"] if task["name"] == "retry-delay"
    ]
    task = manifest["tasks"][0]
    contract = Path(task["contract"])
    path = contract / "test_contract.py"
    path.write_text(path.read_text() + RETRY_PROBES)
    task["contract_version"] = "v2-large-numerics"
    write_json(manifest_path, manifest)
    rows = []
    for row in json.loads((pilot / "runs.json").read_text()):
        if row["task"] != "retry-delay":
            continue
        gate = Verifier().gate(Path(row["run"]) / "candidate", contract)
        rows.append(
            {
                "condition": row["condition"],
                "original_outcome": row["phase"],
                "audit_outcome": gate.outcome,
                "tests": gate.tests,
                "reason": gate.reason,
            }
        )
    write_json(
        output / "audit.json",
        {
            "note": "Post-hoc tests added after reviewing the first pilot; original scores unchanged.",
            "results": rows,
        },
    )
    return {
        "audit": [
            {
                "condition": row["condition"],
                "outcome": row["audit_outcome"],
                "failed_tests": sum(
                    status == "failed" for status in row["tests"].values()
                ),
            }
            for row in rows
        ],
        "next_manifest": str(manifest_path),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.pilot, args.out), indent=2))
