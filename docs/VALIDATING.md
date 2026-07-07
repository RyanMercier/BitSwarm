# Running a BitSwarm Validator

The validator is the trusted role: it decomposes specs, distributes
subtasks, merges patches, runs the gates, and converts scores into
on-chain weights. It is also the component that executes
miner-supplied code, so its security posture matters most. This guide
covers deployment, the sandbox, the user-facing API, and operations.

Registration and wallet steps live in docs/TESTNET.md; this guide is
what you run after your hotkey is on the subnet.

## What you need

- Linux host, Python 3.10+, git, docker.
- Language toolchains for the workloads you accept. The container
  image built from `docker/Dockerfile.base` bundles all seven
  (Python, Node, JDK+Maven, .NET, gcc/g++, Rust); build it once:

  ```bash
  docker build -f docker/Dockerfile.base -t bitswarm-base:latest .
  ```

- A coordinator backend: `COORDINATOR_BACKEND=sdk` with an
  `ANTHROPIC_API_KEY`, or `claude_code` on a Claude subscription.

## The three processes

A production validator runs as up to three processes sharing two
directories (the task inbox and the output dir):

1. **The neuron** (`python -m neurons.validator`): polls the inbox,
   runs the pipeline per task, records scores, submits weights at
   each tempo.
2. **The API** (`python -m validator.api`): the front door for
   users. Accepts specs over HTTP, drops them in the inbox, serves
   status, results, and the verified patch.
3. **Docker** (daemon): the sandbox for gate execution.

```bash
export COORDINATOR_BACKEND=claude_code
export BITSWARM_SANDBOX=docker           # hard-require the sandbox
export BITSWARM_API_KEYS=key-for-user-1,key-for-user-2

python -m neurons.validator \
  --netuid <N> --subtensor.network <finney|test> \
  --wallet.name my_validator --wallet.hotkey default \
  --inbox ./task_inbox --output ./validator_runs &

python -m validator.api \
  --inbox ./task_inbox --output ./validator_runs --port 8100 &
```

Crash safety is built in: tasks claimed by a loop that died are
requeued automatically on the next start, and per-miner scores
persist in `validator_runs/scorebook.json` across restarts.

## The sandbox (read this section)

Every gate run executes code a miner shipped. With
`BITSWARM_SANDBOX=docker` (recommended for anything public), gate
commands run inside a container with:

- no network (`--network=none`): no exfiltration, no callbacks;
- CPU, memory, and process-count ceilings
  (`BITSWARM_SANDBOX_CPUS`, `BITSWARM_SANDBOX_MEM`);
- only the repo under test mounted, nothing else visible;
- an allowlisted environment: your wallet, API keys, and shell
  environment never cross into the container.

Modes: `auto` (default: docker when available, loud warning and host
execution otherwise), `docker` (refuse to run gates unsandboxed),
`off` (development only). A production validator should set `docker`
so a misconfigured host fails fast instead of failing open.

What the sandbox does not cover: the coordinator's own LLM calls
(your trusted code), and miners' agents (their machines, their
risk; see docs/MINING.md).

## Serving users

Users interact with your API, not your neuron:

```bash
# Submit: a spec plus the repo as a base64 git bundle
curl -X POST http://validator:8100/tasks \
  -H "X-API-Key: key-for-user-1" -H "Content-Type: application/json" \
  -d '{"spec": "Add rate limiting to the fetch client, with tests",
       "mode": "diff", "repo_bundle": "'"$(git bundle create /dev/stdout --all | base64 -w0)"'"}'

# Poll
curl -H "X-API-Key: key-for-user-1" http://validator:8100/tasks/<id>

# Fetch the verified change when status is "done"
curl -H "X-API-Key: key-for-user-1" http://validator:8100/tasks/<id>/patch > change.diff
```

The patch endpoint returns one unified diff covering the whole
verified change; `result.json` alongside it carries the scores, the
gate outcomes, and the holdback commitment hash any third party can
use to audit the run.

Issue one API key per user (`BITSWARM_API_KEYS` is comma-separated)
and put a TLS-terminating reverse proxy in front for anything beyond
a trusted network.

## Scoring knobs

| Variable | Default | Meaning |
|---|---|---|
| `BITSWARM_HOLDBACK_FRACTION` | 0 | Fraction of gate tests hidden from miners (commit-reveal). Set 0.25 in production. |
| `BITSWARM_SCORE_WINDOW` | 20 | Rolling task window per miner |
| `BITSWARM_EMA_ALPHA` | 0.3 | Recency weighting of the average |
| `BITSWARM_REPAIR_MODE` | patch | One scoped repair attempt on a failed additive gate; `off` disables |
| `BITSWARM_MAX_BUNDLE_MB` | 64 | Repo bundle size ceiling, both directions |
| `--tempo-seconds` | 4320 | Weight submission cadence |

## Operational notes

- **Weights**: an all-zero vector is rejected by the chain; until the
  first scored task the validator logs and skips submission. After
  that, weight updates are automatic at each tempo.
- **Audit trail**: every task leaves
  `validator_runs/<task_id>/` with the decomposition debug output,
  per-subtask patches, the merge repo, `result.json`, and
  `patch.diff`. Keep these; they are your evidence when scores are
  disputed.
- **Upgrades**: assignments carry a protocol version; miners on an
  incompatible checkout answer `protocol_mismatch` instead of
  failing mid-run. Upgrade validator first, then miners.
- **Capacity**: one neuron processes one task at a time by design
  (subtasks within a task run on miners in parallel). Run more
  validators to scale task throughput.
