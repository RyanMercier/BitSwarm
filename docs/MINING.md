# Running a BitSwarm Miner

How to earn TAO by running a miner. A miner receives one scoped
subtask at a time, runs a coding agent against it, and returns a
patch. Payment follows verified work: the validator scores your patch
against tests you partly cannot see, folds the score into a rolling
average, and submits it as an on-chain weight that drives your
emissions.

## What you need

- A Linux host (or WSL2) with Python 3.10+, git, and the repo
  installed (`pip install -r requirements.txt`).
- An inference backend. This is your main cost lever and entirely
  your choice; the validator never sees or cares which model wrote
  your patch:

  | `MINER_BACKEND` | What it uses | Cost profile |
  |---|---|---|
  | `sdk` | Anthropic API (`ANTHROPIC_API_KEY`) | metered tokens |
  | `claude_code` | `claude` CLI on a Claude subscription | flat subscription |
  | `openai` | any OpenAI-compatible endpoint (DeepSeek, OpenRouter, Chutes, local vLLM/Ollama) | provider-dependent, can be near-zero on local GPUs |

- A registered hotkey on the subnet and an open port for your axon
  (registration steps in docs/TESTNET.md).

## Start it

```bash
export MINER_BACKEND=openai
export MINER_OPENAI_BASE_URL=https://api.deepseek.com
export MINER_OPENAI_MODEL=deepseek-chat
export MINER_OPENAI_API_KEY=sk-...

python -m neurons.miner \
  --netuid <N> \
  --subtensor.network <finney|test> \
  --wallet.name my_miner --wallet.hotkey default \
  --axon.port 8091
```

## How you get paid, mechanically

1. A validator pings you with a StatusSynapse; you answer available.
2. You receive a TaskSynapse: a git bundle of the repo, your
   subtask's files, the contracts to satisfy, and the visible gate
   tests. You work only inside your slice.
3. Your agent iterates until its own hermetic replay passes: your
   patch, applied to a pristine baseline, run in an isolated
   environment. This is the same computation the validator will run,
   so your local green is meaningful.
4. You return the patch. The validator merges all subtasks, runs the
   additive gate (its tests, including held-back ones you never saw)
   and the regression gate (nothing that worked before may break),
   and scores you: complexity weight x gate result x regression
   multiplier.
5. Scores accumulate in the validator's rolling average (last 20
   tasks, exponentially weighted toward recent). At each tempo the
   normalized averages go on chain as weights, and emissions follow.

## What actually maximizes earnings

- **Ship real patches.** An empty patch scores zero no matter what
  your agent claims; the harness checks the artifact, not the
  report. Overfitting to the visible tests also fails: part of the
  gate is hidden behind a hash commitment until scoring.
- **Reliability beats raw speed.** The rolling average rewards
  consistent verified completions over occasional brilliance.
- **Pick your model per economics.** A cheap local coder model that
  passes gates earns exactly what a frontier model earns on the same
  subtask. Score-per-dollar is the number to optimize.
- **Stay honest about being busy.** A busy miner answers
  `stop_reason="busy"` immediately (built in); validators reroute
  instead of timing out on you.

## Protecting your host

- Your agent executes model-written shell commands in its workspace.
  Run the miner inside the provided container
  (`docker/Dockerfile.miner`) or a VM; never on a host with keys or
  credentials you care about.
- `BITSWARM_MAX_TASK_SECONDS` (default 3600) caps how long any
  assignment can hold your miner, regardless of what a validator
  requests.
- `BITSWARM_MAX_BUNDLE_MB` (default 64) refuses oversized repo
  bundles before decoding them.
- `BITSWARM_MIN_VALIDATOR_STAKE` ignores requests from hotkeys below
  a stake floor (default 0; raise it on mainnet).
- Protocol version mismatches are rejected up front with
  `stop_reason="protocol_mismatch"` so you never burn compute on an
  incompatible assignment.

## Troubleshooting

- **Registered but no tasks**: confirm your axon port is reachable
  from outside (firewall, `--axon.external_ip` behind NAT) and that
  your hotkey shows in `btcli subnet metagraph`.
- **Tasks arrive but score zero**: read your own hermetic replay
  output; if it fails locally it fails at scoring. The most common
  cause is a patch touching files outside your subtask scope (they
  are stripped).
- **`protocol_mismatch`**: your BitSwarm checkout is older or newer
  than the validator's. `git pull` and restart.
