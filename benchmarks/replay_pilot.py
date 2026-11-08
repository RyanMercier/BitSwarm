"""Recheck every completed pilot patch without invoking any model."""

import argparse
import json
from pathlib import Path

from harness.runtime import replay, write_json


def replay_all(pilot: Path, output: Path):
    results = []
    for row in json.loads((pilot / "runs.json").read_text()):
        run = Path(row["run"])
        result = replay(run)
        results.append(
            {
                "task": row["task"],
                "condition": row["condition"],
                "original_phase": row["phase"],
                "replay_outcome": result["outcome"],
                "environment_match": result["environment_match"],
                "baseline_hash": result["baseline_hash"],
                "candidate_hash": result["candidate_hash"],
                "contract_hash": result["contract_hash"],
            }
        )
    write_json(output, results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(replay_all(args.pilot, args.out), indent=2))
