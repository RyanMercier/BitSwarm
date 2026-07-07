# BitSwarm Testnet Runbook

Step-by-step for taking BitSwarm from the local pipeline to a running
subnet on the Bittensor test network. Everything here is operator
action: wallet creation, getting test TAO, registration, and
launching the neurons. The code is on main; this doc is how you drive
it.

Cost: **zero real money.** Testnet runs entirely on test TAO, which
has no monetary value and comes free from the community faucet
channel on Discord. Real TAO is only involved at mainnet launch
(Phase C), and even there the subnet-registration lock is returned on
deregistration.

The design keeps the chain at the edges. The coordinator, miners,
merge pipeline, and gates are the same code the local pipeline runs
and the test suite covers. The neurons (`neurons/miner.py`,
`neurons/validator.py`) are thin transports that carry the existing
contracts over Axon/Dendrite and submit scores as weights. If a step
below fails, the failure is almost always in wallet/registration
plumbing, not in BitSwarm logic.


## 0. Prerequisites

- A Linux host (or WSL2). The validator needs the per-language
  toolchains if you plan diff-mode or multi-language tasks; the
  docker base image already bundles them.
- Python 3.10+ with the repo installed:

  ```bash
  pip install -r requirements.txt   # includes bittensor
  ```

- The `btcli` command (ships with the bittensor package):

  ```bash
  btcli --version
  ```

- An inference backend for the miner (`MINER_BACKEND`): a Claude
  subscription for `claude_code`, an `ANTHROPIC_API_KEY` for `sdk`,
  or a provider key for `openai` (Chutes, DeepSeek, OpenRouter, local
  vLLM). Same selection as the local runs.


## 1. Create wallets

You need two coldkey/hotkey pairs, one for the validator and one for
the miner (they can live on the same machine for a first bring-up).

```bash
# Validator
btcli wallet new_coldkey --wallet.name bitswarm_validator
btcli wallet new_hotkey  --wallet.name bitswarm_validator --wallet.hotkey default

# Miner
btcli wallet new_coldkey --wallet.name bitswarm_miner
btcli wallet new_hotkey  --wallet.name bitswarm_miner --wallet.hotkey default
```

Back up the mnemonics. On testnet the TAO is play money, but losing a
coldkey still means re-registering.


## 2. Get test TAO

Everything on testnet is paid in **test TAO**, a token with no
monetary value. You never spend real TAO to run on testnet. (Real
TAO only enters at mainnet, Phase C, where subnet registration has a
lock cost that is returned on deregistration.)

The automated faucet (`btcli wallet faucet`) is **disabled** on
testnet. Get test TAO from the community instead:

1. Join the Bittensor Discord (discord.com/invite/bittensor).
2. Go to the faucet / test-tao channel.
3. Post your validator and miner coldkey ss58 addresses and request
   test TAO. You need roughly 100+ test TAO on the validator coldkey
   to create a subnet (the lock cost fluctuates with demand), plus a
   small amount on each for registration.

Get your ss58 addresses to paste into Discord:

```bash
btcli wallet overview --wallet.name bitswarm_validator --subtensor.network test
btcli wallet overview --wallet.name bitswarm_miner     --subtensor.network test
```

Once the community sends test TAO, re-run the overview commands to
confirm the balances arrived before proceeding.


## 3. Get a subnet

Two paths:

- **Join an existing test subnet** someone gave you the netuid for.
  Skip to step 4 with that netuid.
- **Create your own** (recommended for isolated testing):

  ```bash
  btcli subnet create --subtensor.network test --wallet.name bitswarm_validator
  ```

  Note the netuid it prints. Every command below uses `--netuid
  <N>`; export it for convenience:

  ```bash
  export NETUID=<N>
  ```


## 4. Register both hotkeys on the subnet

```bash
btcli subnet register --netuid $NETUID --subtensor.network test \
  --wallet.name bitswarm_validator --wallet.hotkey default

btcli subnet register --netuid $NETUID --subtensor.network test \
  --wallet.name bitswarm_miner --wallet.hotkey default
```

Verify both show up in the metagraph:

```bash
btcli subnet metagraph --netuid $NETUID --subtensor.network test
```

You should see two UIDs with your two hotkeys.


## 5. Stake to the validator

Validators need stake to have their weights counted. On testnet, a
small self-stake is enough:

```bash
btcli stake add --netuid $NETUID --subtensor.network test \
  --wallet.name bitswarm_validator --wallet.hotkey default --amount 100
```


## 6. Launch the miner neuron

Pick a port reachable by the validator (open it in the firewall / set
`--axon.external_ip` if behind NAT).

```bash
export MINER_BACKEND=claude_code   # or sdk / openai (+ MINER_OPENAI_* vars)

python -m neurons.miner \
  --netuid $NETUID \
  --subtensor.network test \
  --wallet.name bitswarm_miner \
  --wallet.hotkey default \
  --axon.port 8091 \
  --logging.debug
```

On start it prints the resolved UID and the axon serving address. It
then waits for TaskSynapse and StatusSynapse. On testnet leave
`BITSWARM_MIN_VALIDATOR_STAKE=0` (the default) so your own validator
is accepted regardless of its small stake.


## 7. Launch the validator neuron

```bash
export COORDINATOR_BACKEND=claude_code   # or sdk

python -m neurons.validator \
  --netuid $NETUID \
  --subtensor.network test \
  --wallet.name bitswarm_validator \
  --wallet.hotkey default \
  --inbox ./task_inbox \
  --output ./validator_runs \
  --tempo-seconds 900 \
  --logging.debug
```

The validator syncs the metagraph, polls `./task_inbox` for task
files, and submits weights every `--tempo-seconds` (set low for
testing; production tracks the real ~72-minute tempo).


## 8. Submit a task

Two intakes, same inbox. The HTTP API is what real users hit
(details in docs/VALIDATING.md):

```bash
export BITSWARM_API_KEYS=testkey
python -m validator.api --inbox ./task_inbox --output ./validator_runs --port 8100 &

curl -X POST http://localhost:8100/tasks \
  -H "X-API-Key: testkey" -H "Content-Type: application/json" \
  -d '{"spec": "Build a small calculator module with tests",
       "mode": "scaffold", "target_repo": "/absolute/path/to/starter/repo"}'
# poll /tasks/<id>; fetch /tasks/<id>/patch when done
```

Or drop a JSON file into the inbox directly. Scaffold mode (build
from a spec):

```bash
cat > task_inbox/task1.json <<'EOF'
{
  "spec": "Build a small calculator module with add, sub, mul, div functions and pytest tests. Pure Python, no dependencies.",
  "target_repo": "/absolute/path/to/an/empty/starter/repo",
  "mode": "scaffold"
}
EOF
```

Diff mode (modify an existing repo):

```bash
cat > task_inbox/task2.json <<'EOF'
{
  "spec": "Add a `multiply` function to calc/ops.py and use it in calc/main.py's run().",
  "target_repo": "/absolute/path/to/existing/repo",
  "mode": "diff"
}
EOF
```

The validator claims the file (renames to `.working`), runs the full
pipeline, dispatches subtasks to available miners over Dendrite,
merges, scores, records per-hotkey scores, and writes
`validator_runs/<task_id>/result.json`. On success the task file
becomes `.done`; on failure, `.failed`.


## 9. Verify weights landed on chain

After a tempo boundary with at least one scored task:

```bash
btcli subnet metagraph --netuid $NETUID --subtensor.network test
```

The miner UID should show a nonzero weight from your validator. That
weight is the on-chain record of verified work. This is the milestone
that proves the loop closed: a spec went in, a miner produced a
verified patch, and the validator committed a score to the
blockchain.


## 10. Turn on hidden-test holdback (recommended for real testing)

With a single validator scoring its own tasks, holdback is optional.
To exercise the anti-overfitting path, set the fraction on the
validator before launch:

```bash
export BITSWARM_HOLDBACK_FRACTION=0.25   # hold back 25% of gate tests
```

The coordinator now commits a hash of the held-back tests before
mining and reveals them only at scoring. `result.json` records the
`holdback_commit` so a third party can later verify the reveal.


## Configuration reference (neuron-specific)

| Variable / flag | Default | Meaning |
|---|---|---|
| `--netuid` | (required) | Subnet id |
| `--subtensor.network` | (required) | `test` for testnet |
| `--tempo-seconds` (validator) | 4320 | Seconds between weight submissions |
| `--poll-seconds` (validator) | 15 | Task-inbox poll interval |
| `--axon.port` (miner) | (bittensor default) | Port the miner serves on |
| `MINER_BACKEND` | `sdk` | Miner inference backend |
| `COORDINATOR_BACKEND` | `sdk` | Coordinator backend |
| `BITSWARM_MIN_VALIDATOR_STAKE` | 0 | Min stake for a validator to be accepted by a miner |
| `BITSWARM_HOLDBACK_FRACTION` | 0 | Fraction of gate tests held back |
| `BITSWARM_SCORE_WINDOW` | 20 | Rolling task window for the EMA |
| `BITSWARM_EMA_ALPHA` | 0.3 | EMA smoothing (higher = more weight on recent) |


## Troubleshooting

- **"hotkey not registered on netuid"**: the neuron's wallet hotkey
  is not in the metagraph. Re-run the register command from step 4
  and confirm with `btcli subnet metagraph`.
- **Miner never receives tasks**: check the validator's
  `available_miners()` log line. If your miner is not listed, the
  StatusSynapse is not reaching it (firewall / wrong external IP), or
  the miner blacklisted the validator for insufficient stake (set
  `BITSWARM_MIN_VALIDATOR_STAKE=0`).
- **Weights rejected**: an all-zero weight vector is rejected by the
  chain. This is expected until at least one task has been scored;
  the validator logs "no scored miners yet; skipping submission".
- **Large repo bundle errors**: TaskSynapse carries a base64 git
  bundle. For big targets, raise the axon/dendrite body-size limits
  (bittensor's `--axon.max_workers` and the request-size config) or
  scope tasks to smaller repos on testnet.


## What testnet proves, and what it does not

Proves: the full loop runs on chain infrastructure. Spec in, verified
patch out, score committed as weight, reproducible by any validator.

Does not prove yet (Phase B/C work): multi-validator consensus under
real disagreement, economic equilibrium (do miners profit at the
chosen emission rate), the challenge market, and the Attested-CI
product surface. Those are the testnet-to-mainnet roadmap in
docs/STATUS.md.
