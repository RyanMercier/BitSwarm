# Live pilot evidence

**Audit caveat:** both retry-delay candidates passed this original contract but
failed additional numerical probes. See [post-hoc audit](posthoc-audit.json),
[strengthened-contract follow-up](../followup/README.md), and
[case study](../../LINKEDIN_ARTICLE.md). Original scores below are preserved;
they are not a claim that either implementation is correct for every valid input.

Synthetic pilot: 3 authored Python tasks, contract v1, one model family, deterministic condition order, no statistical superiority claim. Equal maximum budgets do not mean equal actual spend. Dollar figures are Claude CLI estimates, not subscription invoices. Acceptance tests are withheld during each run, but public in the benchmark source. Passing these contracts does not establish general repository-editing capability.

| Condition | Accepted | Model calls | Median seconds | Estimated total |
|---|---:|---:|---:|---:|
| reviewed-fable | 3/3 | 29 | 78.1 | $4.110 |
| solo-fable | 3/3 | 17 | 45.9 | $2.339 |

Each run folder contains the selected action trace, result, provenance, and saved patch. runs.json retains every attempt in these batches. source_hashes.json identifies the runtime.

All six saved patches were also reconstructed and checked again; see
[replay results](replay.json). The original condition order was solo then reviewed
for each task. The separate follow-up reverses that order for the retry task and
uses a stronger contract, so its results must not be pooled with this table.

Reproduce from the repository root with a working Docker image and Claude login:

```bash
uv run python benchmarks/prepare.py --out out/pilot-tasks
uv run bitswarm evaluate --manifest out/pilot-tasks/manifest.json --out out/pilot
uv run python benchmarks/robustness.py --pilot out/pilot --out out/robustness
uv run bitswarm evaluate --manifest out/robustness/tasks/manifest.json --out out/followup
uv run python benchmarks/replay_pilot.py --pilot out/pilot --out out/replay.json
```

These commands rerun stochastic models and consume usage; matching the historical
outcomes is not guaranteed. Only recorded results, not deterministic reproduction
of model behavior, are claimed. Timings include model/tool execution on one
workstation, sometimes under other validation load, and exclude preparation.

The runtime has been revised since these runs (see `../../HARNESS_REVIEW.md`).
`source_hashes.json` and each `provenance.json` identify the code that produced
this evidence. A replay under the current verifier reports
`environment_match: false` and records both environments alongside its verdict.
