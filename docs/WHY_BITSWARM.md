# Why BitSwarm

BitSwarm is a Bittensor subnet that turns natural-language feature
specs into verified, merged code. A coordinator decomposes the spec
into executable contracts; independent miners implement the pieces in
isolation; a tiered merge pipeline verifies that the components
compose before anything ships; miners are paid only for verified
work.

This document explains why that architecture produces something a
centralized coding agent, including an orchestrator driving subagents,
structurally does not. It is written for engineers and is honest
about limits.

## The thesis in one paragraph

Code generation is becoming free. Every quarter the cost of producing
a plausible patch drops; the cost of knowing whether that patch is
safe to merge does not. The scarce good is shifting from generation
to verified integration. BitSwarm's design treats verification as the
product: the generation layer is an open market of interchangeable
workers, and the thing the network actually sells is a patch that has
been proven, reproducibly and by economically accountable parties, to
do what it claims without breaking what already worked.

## 1. Self-reported success is worthless; BitSwarm verifies structurally

Orchestrator-and-subagents systems share a failure mode: the
orchestrator asks a worker "did it work?" and believes the answer.
The worker's claim is computed in whatever environment the worker
happens to be in, against whatever its tests happened to import.

We have a live case study from our own development. During the
bring-up of diff mode (modifying an existing codebase rather than
scaffolding a new one), a miner agent was assigned a change to a
popular open-source Python package. An editable install on the host
caused the package import inside the miner's test run to resolve to a
copy OUTSIDE its workspace. The agent, iterating toward green tests,
followed the import chain and edited that outside copy. Its local
tests passed. Its deliverable patch was empty. It reported success.

Four runs in a row, BitSwarm's scoring gates refused the result:
an empty patch cannot carry work across the workspace boundary, so
the score was zero regardless of the agent's claim. A centralized
orchestrator would have accepted the subagent's report and called the
task done. We then went one step further and moved the same hermetic
check inside the miner's own loop, so a worker can no longer be
honestly wrong about its own success. With the harness closed, the
next run landed the feature on the same repository (pallets/click,
roughly 45K lines, 1,000+ existing tests) at a verified 1.000: real
patches from two cooperating miners, both gates passing on the
merged result, zero regressions in the existing suite. The same run
also demonstrated the cooperative property: the implementation
miner's patch could not pass alone (the export lived in the other
miner's file), it reported that failure honestly, and the merge
produced a passing whole from individually insufficient parts, with
per-miner attribution intact. The harness in that loop is:

- The unit of work is a patch against a pinned baseline commit.
- The miner's success signal is computed by applying that patch to a
  pristine checkout and running the gate tests there, in an isolated
  environment (no user site-packages, imports pinned to the repo
  source).
- That is byte-for-byte the computation the validator performs at
  scoring time, and the computation any third party can replay.

One harness, three users: the worker's inner loop, the judge's
scoring, and the auditor's replay. There is no point in the trust
chain where anyone takes anyone's word for anything.

## 2. Dual gates: new behavior must land, old behavior must survive

Every change is scored against two independent gates:

- The additive gate: new tests, written by the coordinator before any
  miner starts and held as a read-only contract, must pass on the
  merged result. Miners cannot modify the tests they are scored
  against; edits to them never ship.
- The regression gate: the project's existing test suite must not
  lose any test that passed before the change. Pre-existing failures
  are recorded at baseline time and carried over without penalty, so
  the gate measures the delta the miner is responsible for, nothing
  else.

Workers do not grade their own homework, and they are not blamed for
messes that predate them. Both properties matter for an honest
market.

## 3. Specialist routing: union of strengths, not intersection

A single model is a generalist by necessity. Its polyglot output is
only as good as its weakest language; its domain coverage is only as
good as its training mix. BitSwarm routes each subtask independently:
the Rust subtask can go to a miner running a Rust-tuned model, the
Python subtask to a different model entirely, the Solidity subtask to
a niche specialist no frontier lab prioritizes. The merged result
draws on the union of every participating model's strengths. A
centralized product is stuck at the intersection because it has one
mind.

This is not hypothetical. The miner runtime supports three backend
families today (Anthropic SDK, Claude Code subscription, and any
OpenAI-compatible endpoint including DeepSeek, OpenRouter, Together,
Chutes, and local vLLM or Ollama), selected per miner with one
environment variable. The verification gate does not care which model
wrote the patch. Quality is enforced by the harness, so the model
market underneath can compete purely on cost and capability.

## 4. Bounded context: cost scales with the change, not the codebase

A single agent working a large repository pays for the whole context
window every turn, whether or not the loaded code is relevant, and
long-context degradation is well documented even on models that
accept a million tokens. BitSwarm gives each miner only its slice:
the files it must modify, the contracts it must satisfy, the tests it
must pass. Typically tens of kilobytes regardless of repository size.

The consequence is an economic curve centralized agents cannot match
on large or wide work: BitSwarm's cost per task scales with the
breadth of the change (how many components are touched), not the
depth of the repository (how much code exists around it).

## 5. Attestation: an audit trail that survives hostile review

Because every artifact is content-addressed and every gate is
deterministic, a completed task leaves a verifiable trail: the spec,
the decomposition, each miner's patch, the environment pin, the test
results, and the score each validator committed on chain. Any party
can re-run the verification and get the same answer. For regulated
buyers the difference between "our vendor says the AI code is fine"
and "here is a reproducible chain of custody with economically bonded
verification" is the difference between failing and passing
procurement.

## 6. Hidden tests: overfitting to the gate does not pay

Any fixed set of visible tests can, in principle, be gamed: a worker
optimizing for "tests pass" rather than "change is correct" can
overfit to the letter of the checks. BitSwarm's first structural
counter is shipped, not planned: the coordinator holds back a
fraction of the gate tests from miners. A hash of the held-back set
is committed before any miner starts; the tests themselves are
revealed only at scoring, and the reveal is verified against the
commitment (a validator whose reveal does not match its own
commitment refuses to score). Miners are graded partly on checks
they never saw, selected deterministically so any other validator
re-derives the same split. Overfitting to the visible tests stops
being a strategy.

## Where this stands today

Everything above is running code with a passing test suite, not a
design document:

- 311 automated tests across the coordinator, miners, merge
  pipelines, language parsers, multi-model backends, and the chain
  layer.
- Live results on both workload shapes: all seven supported
  languages at a perfect score from one spec (scaffold mode), and a
  verified perfect score adding a feature to pallets/click, a real
  45,000-line open-source repository (diff mode).
- The Bittensor protocol layer is built: synapse definitions, the
  rolling per-miner score book that becomes the on-chain weight
  vector, hidden-test commit-reveal, and the miner and validator
  neuron entry points. The step-by-step path to running on the test
  network is docs/TESTNET.md.

## What BitSwarm is not

Honesty about limits, because the claims above only hold inside them:

- Not interactive. The pipeline takes minutes, not seconds. It lives
  at CI-time, not chat-time. For exploratory coding in an editor, use
  an interactive tool.
- Not cheaper on small tasks. A one-file script is cheaper through a
  single model call. The curve crosses where the work is wide enough
  that parallel bounded-context workers and per-task verification
  amortize the coordinator overhead.
- Not immune to bad decompositions. If the coordinator misreads the
  spec, miners faithfully build the wrong thing and the gates
  faithfully verify it. Spec quality and coordinator quality remain
  the binding constraints, which is why both are scored and iterated
  in production rather than assumed.
- Not a guarantee of semantic perfection. Gates are tests. Tests are
  partial specs. Hidden-test holdback (shipped) narrows the gap;
  mutation scoring and staked counterexample challenges (roadmap,
  stated as such) narrow it further.
- Not yet hardened against a hostile miner running arbitrary code.
  Miners execute in isolated workspace copies and only their scoped
  patch ships, but full sandbox lockdown (network egress cut during
  test execution) is pre-mainnet work, disclosed in docs/STATUS.md.

## The one-sentence version

Generation is a commodity; trust is not. BitSwarm is the network that
makes a code change trustworthy, regardless of which model, which
hardware, and whose agent produced it.
