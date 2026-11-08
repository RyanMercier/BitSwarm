"""Small explicit experiment runner; no automatic model selection or spending."""

import json
import statistics
from pathlib import Path

from harness.report import report
from harness.runtime import Runtime, create_run, write_json
from harness.store import Store

TERMINAL = ("passed", "failed", "inconclusive", "cancelled")


def _row(task: dict, condition: dict, repetition: int, state: dict, run: Path) -> dict:
    return {
        "task": task["name"],
        "condition": condition["name"],
        "repeat": repetition,
        "phase": state["phase"],
        "turns": state.get("turns", 0),
        "seconds": state.get("elapsed", 0.0),
        "known_cost_usd": state.get("known_cost_usd", 0.0),
        "unknown_cost_calls": state.get("unknown_cost_calls", 0),
        "fixture": condition["config"].get("implementer", {}).get("kind") == "fixture",
        "run": str(run),
    }


def evaluate(manifest_path: Path, output: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    repeats = manifest.get("repeats", 1)
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats must be 1..10")
    tasks, conditions = manifest["tasks"], manifest["conditions"]
    if not tasks or not conditions or len(tasks) * len(conditions) * repeats > 200:
        raise ValueError("evaluation must contain 1..200 runs")
    # Resumable: an interrupted evaluation reuses finished runs instead of
    # paying for them again. The manifest must be byte-identical.
    output.mkdir(parents=True, exist_ok=True)
    saved_manifest = output / "manifest.json"
    if saved_manifest.exists():
        if json.loads(saved_manifest.read_text()) != manifest:
            raise ValueError("output directory holds a different manifest")
    else:
        write_json(saved_manifest, manifest)
    rows = []
    for ti, task in enumerate(tasks):
        for ci, condition in enumerate(conditions):
            for repetition in range(repeats):
                run = output / f"task-{ti}-condition-{ci}-repeat-{repetition}"
                try:
                    if run.exists():
                        state = Store(run).load()
                        if state.get("phase") not in TERMINAL:
                            state = Runtime(run).run()
                    else:
                        config = {**manifest.get("limits", {}), **condition["config"]}
                        create_run(
                            run,
                            Path(task["repo"]),
                            Path(task["contract"]),
                            task["spec"],
                            task["allowed"],
                            config,
                            task.get("base", "HEAD"),
                        )
                        state = Runtime(run).run()
                    report(run)
                    rows.append(_row(task, condition, repetition, state, run))
                except Exception as exc:  # noqa: BLE001 -- failed experiments must remain in the denominator
                    rows.append(
                        {
                            "task": task["name"],
                            "condition": condition["name"],
                            "repeat": repetition,
                            "phase": "inconclusive",
                            "error_type": type(exc).__name__,
                            "run": str(run),
                        }
                    )
                write_json(output / "runs.json", rows)
    summary = {}
    for condition in conditions:
        selected = [r for r in rows if r["condition"] == condition["name"]]
        passed = sum(r["phase"] == "passed" for r in selected)
        cost_known = all(
            "known_cost_usd" in r and not r.get("unknown_cost_calls") for r in selected
        )
        costs = sum(r.get("known_cost_usd", 0) for r in selected)
        latencies = [r["seconds"] for r in selected if "seconds" in r]
        summary[condition["name"]] = {
            "runs": len(selected),
            "passed": passed,
            "acceptance_rate": passed / len(selected),
            "known_total_cost_usd": costs,
            "cost_per_accepted_task": costs / passed if passed and cost_known else None,
            "cost_complete": cost_known,
            "median_seconds": statistics.median(latencies) if latencies else None,
        }
    result = {
        "conditions": summary,
        "note": "Fixture runs test orchestration, not model capability. "
        "No statistical superiority claim is made from this summary.",
    }
    write_json(output / "summary.json", result)
    return result
