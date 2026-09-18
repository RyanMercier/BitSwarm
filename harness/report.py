"""Static, escaped HTML report. No remote dependencies or scripts."""

import html
import json
from pathlib import Path

from harness.store import Store


def report(directory: Path) -> Path:
    store = Store(directory)
    state, events = store.load(), store.events()
    escape = lambda value: html.escape(json.dumps(value, indent=2, ensure_ascii=False))
    cards = "".join(
        f"<details><summary>{e['seq']}. {html.escape(e['kind'])}</summary>"
        f"<pre>{escape(e['data'])}</pre></details>"
        for e in events
    )
    result = state.get("result", {})
    fixture = state.get("config", {}).get("implementer", {}).get("kind") == "fixture"
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BitSwarm run report</title><style>
body{{font:16px/1.6 system-ui;background:#111827;color:#e5e7eb;max-width:1050px;margin:40px auto;padding:0 24px}}
h1{{font-size:36px}} .meta{{display:flex;gap:24px;flex-wrap:wrap}} .card,details{{background:#1f2937;padding:18px;border-radius:10px;margin:12px 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 monospace}}summary{{cursor:pointer}}a{{color:#93c5fd}}
</style><h1>BitSwarm / {html.escape(state.get("phase", "unknown").upper())}</h1>
<p>{"Scripted offline demonstration: no live model inference." if fixture else "Model-backed run: inspect adapter events for provider and usage."}</p>
<div class="meta"><span>Model calls: {state.get("turns", 0)}</span>
<span>Recorded execution: {state.get("elapsed", 0):.1f}s</span>
<span>Known cost estimate: ${state.get("known_cost_usd", 0):.4f}</span>
<span>Calls with unknown cost: {state.get("unknown_cost_calls", 0)}</span></div>
<div class="card"><strong>Task</strong><p>{html.escape(state.get("spec", ""))}</p>
<p>Execution mode: {html.escape(state.get("environment", {}).get("mode", "unknown"))}</p>
<p>Passing checks are evidence for this contract, not proof of arbitrary code correctness.</p></div>
<h2>Final verification</h2><pre>{escape(result)}</pre>
<h2>Execution trace</h2>{cards}</html>"""
    target = directory / "report.html"
    target.write_text(page, encoding="utf-8")
    return target
