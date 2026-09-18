# Live pilot evidence

This is a **separate follow-up**, after the original pilot exposed a test-coverage
gap. It contains fresh attempts on only the retry-delay task, with five additional
large-number cases withheld in the acceptance contract. Task wording is unchanged;
condition order is reviewed then solo. Both fresh attempts failed final acceptance.

Do not pool these results with the [original pilot](../pilot/README.md), which used
a different contract and three tasks. [Saved-patch replay](replay.json) reproduces
the failures. This demonstrates rejection of these defects, not reliable model
solutions to the extended contract.

Synthetic pilot: 1 authored Python tasks, contract v2-large-numerics, one model family, deterministic condition order, no statistical superiority claim. Equal maximum budgets do not mean equal actual spend. Dollar figures are Claude CLI estimates, not subscription invoices. Acceptance tests are withheld during each run, but public in the benchmark source. Passing these contracts does not establish general repository-editing capability.

| Condition | Accepted | Model calls | Median seconds | Estimated total |
|---|---:|---:|---:|---:|
| reviewed-fable | 0/1 | 11 | 157.9 | $1.827 |
| solo-fable | 0/1 | 8 | 99.7 | $1.288 |

Each run folder contains the selected action trace, result, provenance, and saved patch. runs.json retains every attempt in these batches. source_hashes.json identifies the runtime.
