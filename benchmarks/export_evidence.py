"""Export only this repository's synthetic pilot as portable public evidence.

This is deliberately not a secret scrubber for arbitrary customer repositories.
Full private runs stay in out/. Public exports omit prompts, host paths and stdout.
"""

import argparse
import html
import json
import statistics
from pathlib import Path

from prepare import TASKS
from robustness import RETRY_PROBES

from harness.runtime import write_json
from harness.store import Store
from harness.workspace import digest


def without_output(value):
    if isinstance(value, dict):
        return {k: without_output(v) for k, v in value.items() if k != "output"}
    if isinstance(value, list):
        return [without_output(v) for v in value]
    return value


def export(experiments: list[Path], target: Path, contract_version="v1"):
    target.mkdir(parents=True, exist_ok=False)
    tasks = {task["name"]: task for task in TASKS}
    rows = []
    for batch, experiment in enumerate(experiments):
        for row in json.loads((experiment / "runs.json").read_text()):
            task = tasks[row["task"]]
            expected_contract = task["contract"]
            if (
                contract_version == "v2-large-numerics"
                and task["name"] == "retry-delay"
            ):
                expected_contract += RETRY_PROBES
            run = Path(row["run"])
            state = Store(run).load()
            if (
                (run / "baseline" / "solution.py").read_text() != task["source"]
                or (run / "contract" / "test_contract.py").read_text()
                != expected_contract
                or state["spec"] != task["spec"]
            ):
                raise ValueError("Only the unchanged synthetic pilot can be exported")
            identity = f"batch-{batch}-{row['task']}-{row['condition']}-{row['repeat']}"
            artifact = target / identity
            artifact.mkdir()
            public_row = {**row, "run": identity, "batch": batch}
            rows.append(public_row)
            events = Store(run).events()
            trace = [
                {"seq": e["seq"], "kind": e["kind"], "data": without_output(e["data"])}
                for e in events
                if e["kind"]
                in (
                    "model.responded",
                    "action.completed",
                    "model.failed",
                    "budget.exhausted",
                )
            ]
            write_json(artifact / "trace.json", trace)
            write_json(
                artifact / "result.json", without_output(state.get("result", {}))
            )
            provenance = {
                "contract_version": contract_version,
                "task": task["spec"],
                "base_commit": state["base_commit"],
                "environment": state["environment"],
                "models": {
                    role: {
                        k: state["config"][role][k]
                        for k in ("kind", "model", "call_budget")
                    }
                    for role in ("implementer", "reviewer")
                    if role in state["config"]
                },
                "limits": {
                    k: state["config"][k]
                    for k in ("max_turns", "max_seconds", "call_timeout")
                },
                "baseline_hash": state["baseline_hash"],
                "contract_hash": state["contract_hash"],
                "trace_note": "Prompts, stdout and host paths omitted; this is a selected action trace.",
            }
            write_json(artifact / "provenance.json", provenance)
            if (run / "patch.diff").exists():
                (artifact / "patch.diff").write_bytes((run / "patch.diff").read_bytes())
    summary = {}
    for condition in sorted({row["condition"] for row in rows}):
        group = [row for row in rows if row["condition"] == condition]
        passed = sum(row["phase"] == "passed" for row in group)
        costs = sum(row.get("known_cost_usd", 0) for row in group)
        complete = all(
            "known_cost_usd" in row and row.get("unknown_cost_calls") == 0
            for row in group
        )
        summary[condition] = {
            "attempts": len(group),
            "passed": passed,
            "median_seconds": statistics.median(
                row["seconds"] for row in group if "seconds" in row
            ),
            "model_calls": sum(row.get("turns", 0) for row in group),
            "estimated_usd": costs,
            "cost_complete": complete,
            "estimated_usd_per_pass": costs / passed if passed and complete else None,
        }
    write_json(target / "runs.json", rows)
    write_json(target / "summary.json", summary)
    write_json(
        target / "source_hashes.json",
        {
            str(path): digest(path.read_bytes())
            for path in sorted(Path("harness").glob("*.py"))
        },
    )
    table = "\n".join(
        f"| {name} | {s['passed']}/{s['attempts']} | {s['model_calls']} | {s['median_seconds']:.1f} | ${s['estimated_usd']:.3f} |"
        for name, s in summary.items()
    )
    note = (
        f"Synthetic pilot: {len({r['task'] for r in rows})} authored Python tasks, contract {contract_version}, "
        "one model family, deterministic condition order, "
        "no statistical superiority claim. Equal maximum budgets do not mean equal actual spend. "
        "Dollar figures are Claude CLI estimates, not subscription invoices. "
        "Acceptance tests are withheld during each run, but public in the benchmark source. "
        "Passing these contracts does not establish general repository-editing capability."
    )
    (target / "README.md").write_text(
        "# Live pilot evidence\n\n" + note + "\n\n"
        "| Condition | Accepted | Model calls | Median seconds | Estimated total |\n"
        "|---|---:|---:|---:|---:|\n" + table + "\n\n"
        "Each run folder contains the selected action trace, result, provenance, and saved patch. "
        "runs.json retains every attempt in these batches. source_hashes.json identifies the runtime.\n"
    )
    body = "".join(
        f"<tr><td>{html.escape(row['task'])}</td><td>{html.escape(row['condition'])}</td>"
        f"<td>{html.escape(row['phase'])}</td><td>{row.get('seconds', 0):.1f}s</td>"
        f"<td><a href='{row['run']}/trace.json'>Trace</a> · "
        f"<a href='{row['run']}/result.json'>Verification</a> · "
        f"<a href='{row['run']}/patch.diff'>Patch</a></td></tr>"
        for row in rows
    )
    (target / "index.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>BitSwarm live pilot evidence</title><style>"
        "body{font:16px/1.6 system-ui;max-width:1100px;margin:50px auto;padding:20px;color:#17263a;background:#f5f7fa}"
        "table{border-collapse:collapse;width:100%;background:white}td,th{padding:14px;text-align:left;border-bottom:1px solid #dde3eb}"
        "a{color:#125cb0}pre{white-space:pre-wrap} .table{overflow-x:auto}</style>"
        "<h1>BitSwarm: live coding-agent pilot</h1><p>" + html.escape(note) + "</p>"
        '<div class="table"><table><tr><th>Task</th><th>Condition</th><th>Outcome</th><th>Time</th><th>Evidence</th></tr>'
        + body
        + '</table></div><p><a href="summary.json">Summary JSON</a> · <a href="runs.json">All attempts</a></p></html>'
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiments", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--contract-version", choices=["v1", "v2-large-numerics"], default="v1"
    )
    args = parser.parse_args()
    print(
        json.dumps(export(args.experiments, args.out, args.contract_version), indent=2)
    )
