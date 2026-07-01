# BitSwarm

The verification layer for AI-generated code. A Bittensor subnet that
turns natural-language feature specs into verified, merged code: a
coordinator decomposes the spec into executable contracts; independent
miners implement the pieces in isolation; a hermetic dual-gate harness
verifies that the components compose without breaking what was already
there; miners are paid only for verified work.

## Live results

- **All seven supported languages at 1.000 / 1.000** on a single
  language-agnostic Wordle spec (Python, TypeScript, Java, C#, C,
  C++, Rust) across two overnight Docker runs, zero API spend on
  the Claude Max subscription.
- **Diff mode on pallets/click**, a real 45,000-line open-source
  Python repo with 1,000+ existing tests: a verified 1.000 / 1.000
  added a new EnumChoice parameter type from a four-sentence spec.
  Two cooperating miners; zero new failures in the existing suite;
  13 minutes wall time; $0 cost.

## Why this beats a centralized orchestrator with subagents

The miner's local success signal is byte-for-byte the same
computation the validator runs at scoring time, which is the same
computation any auditor's replay produces. No point in the trust
chain asks anyone to take anyone's word. Read
[docs/WHY_BITSWARM.md](docs/WHY_BITSWARM.md) for the case study (a
live false-positive class we caught and closed during diff-mode
bring-up).

## Docs

- [docs/WHY_BITSWARM.md](docs/WHY_BITSWARM.md) - the verification
  thesis, with the case study.
- [docs/TESTNET.md](docs/TESTNET.md) - step-by-step runbook to
  register and run BitSwarm on the Bittensor test network.
- [docs/STATUS.md](docs/STATUS.md) - engineering state, every env
  var, the full roadmap.
- [BITSWARM_SPEC.md](BITSWARM_SPEC.md) - the v2 design spec.

This README is the quick-start and the multi-LLM cheat sheet.


## Multi-LLM support

A miner picks ONE backend per process via `MINER_BACKEND`. Validators
pick their coordinator backend independently via `COORDINATOR_BACKEND`.

| Backend | Miner | Coordinator | What it does | When to use it |
|---|:-:|:-:|---|---|
| `sdk` (default) | yes | yes | Anthropic Python SDK, metered API tokens | Anyone with an `ANTHROPIC_API_KEY` |
| `claude_code` | yes | yes | `claude` CLI subprocess on a Max / Pro / Team subscription | Local dev + smoke tests, zero API spend |
| `openai` | yes | not yet | Any OpenAI-compatible Chat Completions endpoint | Production miners: pick whichever provider gives the best score per dollar |

The `openai` backend uses the OpenAI Python SDK with a configurable
`base_url`, so it works with: OpenAI, DeepSeek, OpenRouter, Together,
Groq, Fireworks, Anthropic-via-OpenAI-compat, a local vLLM /
llama.cpp / Ollama server, or anything else that speaks the OpenAI
Chat Completions API with tool/function calling.

### Picking a backend

```bash
# A. Anthropic API (default; what you had before)
export ANTHROPIC_API_KEY=sk-ant-...
python -m miner.server                       # MINER_BACKEND defaults to "sdk"

# B. Free local testing on a Claude subscription
npm install -g @anthropic-ai/claude-code
claude auth login
export MINER_BACKEND=claude_code
python -m miner.server

# C. Production miner on any OpenAI-compatible provider
export MINER_BACKEND=openai
export MINER_OPENAI_API_KEY=...           # provider's key
export MINER_OPENAI_BASE_URL=...          # provider's URL (omit for OpenAI)
export MINER_OPENAI_MODEL=...             # provider's model id
python -m miner.server
```

### Provider examples (openai backend)

```bash
# DeepSeek (cheap, strong on code)
MINER_OPENAI_BASE_URL=https://api.deepseek.com
MINER_OPENAI_MODEL=deepseek-chat
MINER_OPENAI_API_KEY=sk-...

# OpenRouter (router across many providers)
MINER_OPENAI_BASE_URL=https://openrouter.ai/api/v1
MINER_OPENAI_MODEL=meta-llama/llama-3.3-70b-instruct
MINER_OPENAI_API_KEY=sk-or-...

# Local vLLM
MINER_OPENAI_BASE_URL=http://localhost:8000/v1
MINER_OPENAI_API_KEY=sk-local            # any non-empty string
MINER_OPENAI_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct

# Local Ollama
MINER_OPENAI_BASE_URL=http://localhost:11434/v1
MINER_OPENAI_API_KEY=sk-local
MINER_OPENAI_MODEL=qwen2.5-coder:32b

# OpenAI itself (defaults work)
MINER_OPENAI_API_KEY=sk-...
MINER_OPENAI_MODEL=gpt-4o-mini           # or gpt-5, etc.
```

If a provider supports OpenAI's `tools` / `function_call` shape but
NOT a `tool_choice="auto"` value, the request will currently fail.
Most major providers do support it. Patches welcome for the corner
cases.

### Mixing backends

Coordinator and miner are independent. Common combos:

```bash
# Plan with the strongest available model, mine with whatever is cheap
COORDINATOR_BACKEND=sdk MINER_BACKEND=openai \
  MINER_OPENAI_BASE_URL=https://api.deepseek.com \
  MINER_OPENAI_MODEL=deepseek-chat \
  python demo/run_pipeline.py --spec demo/spec_wordle.txt

# Everything on the local subscription, zero spend
COORDINATOR_BACKEND=claude_code MINER_BACKEND=claude_code \
  python demo/run_pipeline.py --spec demo/spec_wordle.txt
```


## Quick start

```bash
git clone https://github.com/RyanMercier/BitSwarm.git
cd BitSwarm
pip install -r requirements.txt

# Pick a backend (see Multi-LLM section above) then run the demo:
python demo/run_pipeline.py --spec demo/spec_wordle.txt --out out/run1

# Poke at the result:
cd out/run1/merged_repo
python -m pytest tests/
python -m wordle      # play the game BitSwarm just built
```

C++ run:

```bash
export MINER_LANGUAGE=cpp COORDINATOR_LANGUAGE=cpp
python demo/run_pipeline.py --spec demo/spec_wordle_cpp.txt --out out/cpp_run
cd out/cpp_run/merged_repo
make test
./wordle_bin
```


## Running the test suite

```bash
pip install -r requirements.txt
pip install pytest
python -m pytest tests/                    # ~5s, 292 tests
```


## Layout (high level)

```
config.py              env-var resolution (all three backends)
miner/
  server.py            FastAPI; routes MINER_BACKEND to one of:
  agent.py             ... Anthropic SDK
  agent_cc.py          ... claude CLI subprocess
  agent_openai.py      ... any OpenAI-compatible endpoint
  tools.py             tool definitions (file_read/write, bash, list_files)
  recovery.py          retry / hard-reset state machine
  warm_start.py        pre-loaded context for the first turn
  runtime.py           transport-agnostic execution (shared by HTTP + axon)
validator/
  server.py            orchestration server
  decomposer.py        Phase 1 / 1.5 / 2 coordinator (SDK)
  decomposer_cc.py     subprocess coordinator
  scaffolder.py        write stubs + git commit baseline
  merge.py             tiered merge + recovery (dispatches diff_merge)
  diff_merge.py        diff-mode dual-gate scoring
  weights.py           rolling-EMA scores + on-chain weight submission
  holdback.py          hidden-test commit-reveal
  scorer.py            per-subtask scoring
  parsers/             per-language tree-sitter parsers
protocol/              pydantic schemas + synapses + repo bundling
neurons/               Bittensor entry points (miner.py, validator.py)
docker/                Dockerfile.miner + Dockerfile.validator
demo/                  in-process pipeline runner + specs
tests/                 292 tests
docs/STATUS.md         full status, architecture, roadmap
docs/TESTNET.md        testnet runbook
```


## Diff mode (modify an existing codebase)

```bash
python demo/run_pipeline_diff.py \
  --target /path/to/existing/repo \
  --spec   /path/to/change_spec.txt \
  --out    out/diff_run_1
```

The coordinator decomposes the change into per-file modification
subtasks, each with a target-state stub (the post-edit public API as
real source code). Miners verify their own work by replaying their
patch onto a pristine baseline in a hermetic environment, which is
the same computation the validator's scoring gates run. Scoring is
dual-gate: the coordinator's new tests must pass on the merged
result, and the existing test suite must not lose any
previously-passing test.

## Status

Live-tested: all seven languages at 1.000 on the Wordle spec
(scaffold mode), and a verified 1.000 on pallets/click (diff mode).
The `openai` backend is wired and covered by dispatch +
tool-translation tests but has not yet had a full pipeline run on a
non-Anthropic provider; that run is the next milestone. Diff mode's
miner-side replay is language-generic via the build-system runner
dispatch; the merge-side gates are Python-first today.

Roadmap and known limitations: [docs/STATUS.md](docs/STATUS.md).
