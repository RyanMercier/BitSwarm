# BitSwarm Status & Roadmap

*Last updated: 2026-07-06*

A status document covering where BitSwarm stands today, how the system
actually works in code, and the work between here and a credible
Bittensor subnet that accepts any inference backend (Anthropic, Google
Gemini, DeepSeek, Chutes-served models, local LLM, etc.).

This doc is honest about what's live-tested vs. wired-but-untested vs.
not built. Skim the [TL;DR](#tldr) for the headline; scroll the
[Roadmap](#roadmap) for what's left.


## TL;DR

BitSwarm decomposes a feature spec into parallel subtasks, ships each
to a miner agent that produces a patch, then merges and scores. It
works today in two end-to-end demo configurations and 311 unit /
integration tests cover the contracts.

**What works live (verified end-to-end):**
- **All seven languages at 1.000 / 1.000** on a single
  language-agnostic Wordle spec (Python, TypeScript, Java, C#, C,
  C++, Rust), across two overnight Docker runs on the claude_code
  backend at zero API spend. Java and Rust needed a retry after an
  Anthropic API outage; the retry landed both clean.
- **Diff mode on a real OSS codebase**: pallets/click (~45K lines,
  1,000+ existing tests). Two cooperating miners landed a new
  parameter type at a verified 1.000: real patches, additive gate
  passing on the merged result, zero regressions in the existing
  suite. The implementation miner's patch could not pass alone (the
  export lived in the other miner's file); the merge produced a
  passing whole from individually insufficient parts with per-miner
  attribution intact (0.800 / 0.200).
- **Hermetic verification harness**: the miner's local success
  signal is the same computation as the validator's scoring gate
  (patch applied to pristine baseline, tests run in an isolated
  environment). Closed a live false-positive class found during
  diff-mode bring-up; see docs/WHY_BITSWARM.md for the case study.

**What's wired but not live-tested:**
- Drop-and-replace recovery (replace failing patches by re-mining
  with merged context).
- Self-critique pass over generated stubs.
- Subprocess repair miner (the SDK version IS live-tested for
  Python).
- Mixed backend modes (SDK coordinator + subprocess miner).
- **OpenAI-compatible miner backend** (`MINER_BACKEND=openai`).
  Tool-schema translation + dispatcher are unit-tested; first
  pipeline run on a non-Anthropic provider (DeepSeek, OpenRouter,
  local vLLM, etc.) is the next polish task.
- Diff mode over HTTP: mode + target stubs + new-test content thread
  through TaskAssignment with backward-compatible defaults, and
  validator/server.run_task accepts mode="diff"; covered by dispatch
  tests, not yet exercised against live HTTP miners (the in-process
  demo runner is the live-tested path).
- Diff-mode repair loop: a failing additive gate triggers one
  claude-code repair scoped to the subtask's modify_files, then the
  gate re-runs (never trusting the repair stage's own report). Wired
  in validator/diff_merge.py; not yet observed firing on a live run.
- Diff-mode merge gates beyond Python: the merge-side gates now
  dispatch on the repo's build system, same as the miner-side
  hermetic replay. The additive gate runs each gate test file
  through the shared runner dispatch; the regression gate collects
  per-test failure identities where the runner's output supports it
  (pytest node ids, cargo test names, dotnet Failed lines, ctest
  failed-test section, vitest/jest JSON reporter, JUnit XML report
  files for mvn/gradle) and degrades to suite-level comparison
  otherwise (mocha, make), recording a sentinel failure rather than
  silently passing. Gate tests are physically moved out of the tree
  during suite-level regression runs so the additive contract never
  contaminates the regression signal. All of this is covered by 19
  dispatch/parser unit tests; the live diff-mode run so far is
  Python (pallets/click), so non-Python diff runs remain
  live-untested.

**Phase A shipped (built, chain-untested):**
- Bittensor protocol layer. Three Synapse subclasses
  (protocol/synapses.py), a rolling-EMA ScoreBook + weight submission
  (validator/weights.py), hidden-test commit-reveal
  (validator/holdback.py), a transport-agnostic miner runtime
  (miner/runtime.py) shared by the HTTP server and the axon, and the
  two neuron entry points (neurons/miner.py, neurons/validator.py).
  30 unit tests cover the chain layer without needing a live chain.
  The runbook to register and run on testnet is docs/TESTNET.md. What
  remains is operator action (wallets, faucet, registration) plus the
  first live weight-set, none of which can be unit-tested.

**What's not built yet:**
- Native Gemini backend (function-declaration shape is different
  enough to warrant its own adapter; OpenAI-compat covers most other
  providers).
- Real sandboxing (Docker exists but miners have full Bash).
- Cryptographic verification (anti-collusion mining).
- Test-first decomposition's live validation against a real run
  (the prompt is wired, the three-phase coordinator runs, but it
  hasn't yet been measured against the two-phase baseline on a hard
  spec).


## State of the system

### Test footprint

311 tests passing across 17 test files. Coverage by area:

| Area | Tests |
|---|---|
| Multi-language parsers (7 languages) | 95 |
| Language profile registry + prompt builder | 33 |
| Diff mode (prompts, validator, scaffolder, replay, merge gates) | 33 |
| HTTP dispatch + protocol + transport | 26 |
| Phase 1.5 contract checker | 22 |
| Drop-and-replace + recovery wiring | 12 |
| Cache | 10 |
| Critique + pre-flight | 11 |
| Test-first decomposition wiring | 6 |
| Bug-fix regressions | 13 |
| End-to-end multilang validation | 13 |
| Multi-LLM backend dispatch + tool translation | 8 |
| Chain layer (synapses, weights, holdback, miner runtime) | 17 |
| Language-generic merge gates (failure parsers + dispatch) | 19 |

Run with `pytest tests/` from the repo root. Full suite completes
in ~25 seconds.

### Live demo results

Two end-to-end runs against real Claude subprocesses on a Max
subscription (zero Anthropic API spend):

**Python Wordle** (`demo/run_pipeline.py --spec demo/spec_wordle.txt`):
```
[Coordinator] Validation passed on attempt 1
[pipeline] words:    PASSED in 60.1s
[pipeline] scorer:   PASSED in 28.0s
[pipeline] renderer: PASSED in 21.5s
[pipeline] game:     PASSED in 40.7s
[pipeline] cli:      PASSED in 39.3s
integration_tests   PASS (100%)
TOTAL               1.000 / 1.000
```

Output: 983 lines of working Python across 5 subtasks. Plays.

**C++ Wordle** (`demo/run_pipeline.py --spec demo/spec_wordle_cpp.txt`):
```
[pipeline] words:    PASSED in 184.6s   (62s in best run)
[pipeline] scorer:   PASSED in 27.4s
[pipeline] renderer: PASSED in 38.4s
[pipeline] game:     PASSED in 192.1s
[pipeline] cli:      MAX_ITERATIONS but patch applied + cross-compile OK
[Tier 0/1/2] all cross-compiles: PASSED
Integration tests: PASSED
```

`make wordle_bin && ./wordle_bin` produces an interactive Wordle that
respects `quit` and shows colored feedback. Score showed 0.500-0.750
across runs due to scorer artifacts since fixed.

### Branches

The previous two-branch split (`main` for SDK-only, `claude-code-backend`
for subprocess) has been consolidated. `claude-code-backend` is merged
into `main` so a single branch carries the full feature set: SDK,
Claude Code subprocess, AND a generic OpenAI-compatible backend that
covers DeepSeek / OpenRouter / vLLM / Ollama / Groq / Together / etc.

```
main   - SDK + Claude Code subprocess + OpenAI-compatible backends.
         311 tests. Miner picks one of three (sdk / claude_code /
         openai) via MINER_BACKEND. Coordinator picks one of two
         (sdk / claude_code) via COORDINATOR_BACKEND; openai-coord
         is on the roadmap.
```

Recent commits relevant to the merge + multi-LLM work:

```
(merge) Bring claude-code-backend onto main: subprocess miner,
        subprocess coordinator, drop-and-replace, critique, preflight,
        cache, multi-language profile registry.
(new)   Multi-LLM miner: agent_openai.py + MINER_BACKEND=openai
        dispatch + MINER_OPENAI_* config + test_backends.py.
```


## Architecture

### High-level flow

```
                 user spec (text)
                       |
                       v
       +-------------------------------------+
       | COORDINATOR (one trusted validator) |
       |                                     |
       |  Phase 1   plan: subtasks + shared  |
       |            types + weights          |
       |                                     |
       |  Phase 1.5 (test-first) write       |
       |            integration tests        |
       |                                     |
       |  Phase 2   write stubs that match   |
       |            the integration tests    |
       |                                     |
       |  Pre-flight: compile-check scaffold |
       |  Self-critique: spot drift          |
       |  Phase 1.5 validation: AST contract |
       +-------------------------------------+
                       |
                  decomposition
                       |
                       v
              +----------------+
              | SCAFFOLDER     |   git init + commit
              | writes stubs   |   "BitSwarm scaffolding"
              | to working repo|   <- this commit is the
              +----------------+      patch baseline forever
                       |
            git bundle + dispatch
              over HTTP to N miners
                       |
       +-----+-----+-----+-----+-----+
       v     v     v     v     v     v
      [M1] [M2] [M3] [M4] [M5] [M...N]      <- parallel agents,
       |     |     |     |     |     |         each in an isolated
       |     |     |     |     |     |         workspace copy
       +-----+-----+-----+-----+-----+
                       |
                  MinerResponses
                  (patches + test outputs)
                       |
                       v
       +-------------------------------------+
       | MERGE PIPELINE                      |
       |                                     |
       |  Compute dep tiers from subtasks    |
       |                                     |
       |  for each tier:                     |
       |    apply patches                    |
       |    cross-compile (run each miner's  |
       |      tests on the merged repo)      |
       |    if fail:                         |
       |      - drop-and-replace OR          |
       |      - repair miner OR              |
       |      - skip (mode dependent)        |
       |    commit tier                      |
       |                                     |
       |  run integration tests              |
       |  if fail: repair integration tests  |
       +-------------------------------------+
                       |
                       v
                +------------+
                | SCORER     |   complexity_weight per subtask
                |            |   * integration multiplier (0.5..1.0)
                |            |   = per-subtask score
                +------------+
                       |
                       v
               final scores + merged_repo
```

### Key invariants

1. **The scaffolding commit is the baseline.** All miner patches diff
   against the `BitSwarm scaffolding` git commit. Miners can do any
   git operations they want (commits, branches); patch generation
   ignores all of that and just diffs current state vs scaffolding.
   This is why drop-and-replace works: revert miner's files to that
   commit, re-mine, diff still computes correctly.

2. **Miners are sandboxed by workspace.** Each miner gets a fresh
   copy of the scaffolded repo (or, for drop-and-replace, the
   merged-state repo). They never touch shared state or each other's
   work directly. Their only output is a patch.

3. **The validator is trusted.** In the Bittensor model, validators
   are the trusted parties that orchestrate. This is fine for now;
   adding cryptographic verification (commit-reveal on tests, etc.)
   is roadmap work.

4. **Phase 1.5 validation is the contract.** When it passes, the
   subsequent miner runs are working from a known-consistent scaffold.
   It's currently Python-strong, weaker for C++ (parser doesn't
   handle overloaded constructors), TypeScript/Java/C#/Rust roughly
   the same shape.

5. **Cross-language is a parser problem, not an orchestration
   problem.** The miner / validator / merger don't know languages.
   The decomposer + the per-language profile registry + the parser
   layer are where multi-language lives.


## Component reference

Numbers in parens are approximate line counts.

### Coordinator stack

`config.py` (~85 lines)
: Env var resolution. Anthropic block: `ANTHROPIC_API_KEY`,
  `ANTHROPIC_BASE_URL`, `COORDINATOR_MODEL`, `MINER_MODEL`,
  `MAX_COORDINATOR_RETRIES`, `SUBTASK_TIMEOUT_SECONDS`,
  `SUPPORTED_LANGUAGES`. Backend selection: `MINER_BACKEND`,
  `COORDINATOR_BACKEND` (each `sdk` / `claude_code` / `openai`).
  OpenAI-compatible block (only used when `MINER_BACKEND=openai`):
  `MINER_OPENAI_API_KEY` (or `OPENAI_API_KEY`),
  `MINER_OPENAI_BASE_URL` (or `OPENAI_BASE_URL`),
  `MINER_OPENAI_MODEL` (or `OPENAI_MODEL`, default `gpt-4o-mini`).

`validator/lang_profiles.py` (330 lines)
: Per-language metadata registry. Seven `LanguageProfile`s with
  extensions, stub idiom, test framework, import conventions, default
  integration test filename, test command hint. `profile_for(language,
  repo_path)` resolves via explicit arg → `COORDINATOR_LANGUAGE` env
  → repo build-system markers (Cargo.toml, package.json, etc.) →
  Python fallback.

`validator/prompts.py` (~60 lines)
: The coordinator system prompt. Same text the POC used; not
  language-specific.

`validator/decomposer.py` (~500 lines)
: SDK-based coordinator. Three-phase API:
  - `build_user_message(repo_path, spec, prev_errors)` -> Phase 1
    prompt (structural plan)
  - `build_integration_test_prompt(decomp, repo_path, spec, lang)` ->
    Phase 1.5 prompt (test-first)
  - `build_file_generation_prompt(decomp, repo_path, spec, lang)` ->
    Phase 2 prompt (stubs that satisfy 1.5 tests)
  - `stream_json(...)` -> streamed JSON response with retries
  - `parse_json_response(text)` -> tolerates prose / fences /
    truncation
  - `call_coordinator(repo, spec, prev_errors, debug_dir)` -> three
    phases sequenced
  - `decompose(repo, spec, validate_fn, debug_dir)` -> retry loop +
    cache lookup + self-critique + backend dispatch

`validator/decomposer_cc.py` (~310 lines)
: Subprocess-backed coordinator. Same three phases via `claude -p`.
  Phase 2 uses the file-writing pattern (claude writes files to a
  tempdir, we harvest them) to dodge claude-code's
  `--output-format json` envelope size cap on large outputs.

`validator/critique.py` (180 lines)
: Self-critique pass. After Phase 2, asks Claude to read all
  scaffolded files and report cross-file interface drift as
  `ISSUE: ...` lines. Both SDK and subprocess backends. Failure modes
  return [] rather than block the pipeline. Disable with
  `BITSWARM_SKIP_CRITIQUE=1`.

`validator/preflight.py` (155 lines)
: Compile-check the scaffold against itself before miners run:
  - Python: `python -c "import each_module"`
  - TypeScript: `npx tsc --noEmit`
  - C/C++: `make tests/test_integration`
  - Rust: `cargo check --tests`
  - Java/C#: skipped (heavy build systems)
  Failures route to coordinator retry (advisory). Strict mode aborts
  via `BITSWARM_STRICT_PREFLIGHT=1`. Disable via
  `BITSWARM_SKIP_PREFLIGHT=1`.

`validator/cache.py` (115 lines)
: Decomposition cache. Hash of `(spec + repo_files + language +
  model + backend)` → 24-char key → JSON file at
  `~/.bitswarm/cache/decompositions/<key>.json`. Stale cache hits
  (validation rejects) fall through. Disable with
  `BITSWARM_NO_CACHE=1`. Relocate with `BITSWARM_CACHE_DIR`.

### Phase 1.5 contract validator

`validator/validator_checks.py` (~310 lines)
: Top-level dispatcher for `validate_decomposition(decomp, repo)`.
  Runs checks 1-9:
  - Syntax parse per file
  - Python alias-without-import lint
  - Imports resolve in stub files
  - Imports resolve in test files
  - Existing repo files that depend on scaffolded packages still
    resolve
  - No file path overlap between subtasks
  - Complexity weights sum to 1.0
  - No circular subtask dependencies
  - Every subtask has stub + test content
  - Cross-file contract check (via common module)
  - Verify stub tests fail when run (catches no-op tests)

`validator/validator_checks_common.py` (220 lines)
: Language-agnostic contract checker. Operates on `FileFacts`
  dataclasses produced by the parser layer. Generalized
  `check_interface_contracts` covers imports-name-exists + arity
  matching with cross-language constructor lookup (`__init__` /
  `constructor` / same-name / record-style class params).
  `check_no_circular_deps`. `check_fanout_warning` (advisory).

`validator/parsers/` (8 modules, ~2500 lines)
: Per-language parser layer. Each conforms to the `LanguageParser`
  protocol:
  - `parse(source, filepath) -> tree`
  - `module_path_for_file(filepath, tree, source) -> canonical ref`
  - `extract_imports(tree, source, filepath) -> list[ImportInfo]`
  - `extract_defined_names(tree, source) -> dict[name, CallableInfo]`
  - `extract_call_sites(tree, source) -> list[CallSite]`
  - `resolves(imp, repo_path, scaffolded_files, manifest) -> bool`

  Languages: `python.py` (stdlib `ast`), `typescript.py`, `java.py`,
  `csharp.py`, `c.py`, `cpp.py`, `rust.py` (each via tree-sitter).
  Registry at `parsers/__init__.py`; `.h` disambiguation via sibling
  files.

`validator/test_runners.py` (140 lines)
: Build-system-aware dispatch: detect `pytest` / `vitest` / `jest` /
  `mocha` / `mvn` / `gradle` / `dotnet` / `cargo` / `ctest` / `make`
  from on-disk markers. Falls back to compile-only for C/C++.

`validator/test_runner.py` (90 lines)
: Merge-time test runner (note singular vs plural; the plural one
  above is the build-system dispatcher). Per-language test commands.
  Falls back to binary exit-code signal when test-framework markers
  (PASSED/FAILED) aren't in stdout.

`validator/scaffolder.py` (~85 lines)
: Writes `shared_files` + `stub_files` + `stub_test_files` +
  `integration_test_files` to disk. Creates `__init__.py` for package
  dirs. Updates `requirements.txt`. Commits with message
  `BitSwarm scaffolding` (the patch baseline).

### Miner stack

`miner/agent.py` (~280 lines)
: SDK-based miner. Tool-use loop via `client.messages.create(...)`
  with `TOOL_DEFINITIONS`. Prompt caching headers + `cache_control`
  markers. Hard reset after repeated identical errors. Generates a
  patch by diffing against the scaffolding commit.

`miner/agent_cc.py` (~310 lines)
: Subprocess-backed miner. Spawns `claude -p` with the workspace as
  cwd, tools limited to `Read,Edit,Write,Bash,Glob,Grep`, no MCP, no
  WebFetch. Per-language test command via the lang_profiles registry.
  Same `MinerResult` output shape.

`miner/agent_openai.py` (~240 lines)
: Generic OpenAI-compatible miner. Drives any provider that exposes
  the OpenAI Chat Completions API (OpenAI, DeepSeek, OpenRouter,
  Together, Groq, Fireworks, vLLM, Ollama, llama.cpp). Translates
  Anthropic-style `TOOL_DEFINITIONS` (`input_schema`) into OpenAI
  function-calling shape (`function.parameters`), then runs the same
  tool-use loop as `agent.py` and returns the same `MinerResult`.
  Configured via `MINER_OPENAI_API_KEY` / `MINER_OPENAI_BASE_URL` /
  `MINER_OPENAI_MODEL`. Inherits `MinerResult` and `_generate_patch`
  from `miner.agent` (so the anthropic package is still pulled in;
  acceptable since it's a small dependency).

`miner/tools.py`
: Tool definitions for the SDK agent (file_read, file_write, bash).
  Workspace-scoped via `contextvars` to avoid race conditions when
  multiple miners share a process.

`miner/recovery.py`
: SDK agent's recovery logic. `StopReason` enum, retry-with-hard-
  reset when the same error repeats.

`miner/warm_start.py`
: Builds the warm-start message that goes into every SDK agent loop:
  subtask description + stub + test + sibling subtask file lists.
  Anthropic prompt cache marker on this message.

`miner/prompts.py`
: System prompt for the SDK miner agent. Not used by the subprocess
  miner (claude-code has its own).

`miner/server.py` (~150 lines)
: FastAPI server. `POST /task`, `GET/POST /status`. Backend selection
  via `_select_backend()`: `MINER_BACKEND=sdk|claude_code|openai`.
  Lazy import (so the SDK miner doesn't drag in subprocess code, the
  subprocess miner doesn't drag in openai, etc.). Single-task gate
  via non-blocking `asyncio.Lock.acquire()` → 409 if busy. Unknown
  backend values raise a clear `RuntimeError` at startup.

### Validator stack

`validator/server.py` (~270 lines)
: Orchestration server + `run_task()` coroutine. Workflow:
  1. Set up working repo (copy + git init + commit)
  2. `decompose()` (with cache + critique + retries)
  3. `write_scaffolding()`
  4. `dispatch_to_miners()` round-robin over miner URLs via httpx
  5. `merge_and_test()`
  6. Return scores

`validator/merge.py` (~330 lines)
: Tiered merge pipeline. `compute_tiers(subtasks)` orders by deps.
  Per tier: apply patches → cross-compile each miner → on failure
  dispatch via `_REPAIR_MODE`:
  - `patch` (default): SDK or subprocess repair miner
  - `replace`: drop-and-replace via `_try_drop_and_replace()`, fall
    back to patch
  - `off`: skip
  `_select_repair_backend()` auto-picks subprocess if
  `MINER_BACKEND=claude_code` and no API key.

`validator/drop_replace.py` (220 lines)
: Drop-and-replace recovery. `find_scaffolding_hash()` locates the
  scaffolding commit. `_revert_files_to_scaffolding()` reverts only
  the failing miner's `allowed_files`. `drop_and_replace_subtask()`
  copies the merged repo, reverts the miner's files, runs
  `execute_subtask` (subprocess or SDK), returns new `MinerResult`.
  `apply_replacement_patch()` lands the new patch in the merged repo.

`validator/repair.py` (~430 lines)
: SDK-based repair miner + integration repair. Reads traceback,
  extracts referenced dependency files, runs the SDK tool-use loop
  scoped to the miner's `allowed_files`. Same prompt-caching
  treatment as agent.py.

`validator/repair_cc.py` (190 lines)
: Subprocess-backed repair miner. Same interface as
  `validator/repair.py`. Spawns claude in cwd=merge_repo with a
  focused "fix the failure" prompt.

`validator/scorer.py` (~50 lines)
: `compute_scores(subtasks, miner_results, stub_results,
  integration_passed, integration_pass_ratio)`. Per-subtask score =
  `weight * integration_multiplier`. Integration multiplier =
  `0.5 + 0.5 * ratio` (so 0% integration halves the score; 100%
  passes them at full weight).

### Protocol layer

`protocol/schemas.py` (~60 lines)
: Pydantic models: `TaskAssignment`, `MinerResponse`,
  `StatusCheck`, `StatusResponse`, `ScoreReport`.

`protocol/transport.py` (~45 lines)
: `bundle_repo(path) -> base64 git bundle`,
  `unbundle_repo(b64, dest)`. Sole transport for repo state from
  validator to miner.


## Configuration matrix

| Env var | Used by | Default | Notes |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | All SDK paths | (none) | Required for SDK backends |
| `ANTHROPIC_BASE_URL` | All SDK clients | SDK default | Set to mock-server URL for offline testing |
| `COORDINATOR_MODEL` | SDK coordinator (and subprocess if `CC_COORDINATOR_MODEL` unset) | `claude-sonnet-4-20250514` | Full model name for SDK; alias OK for subprocess |
| `MINER_MODEL` | SDK miner + SDK repair | `claude-sonnet-4-20250514` | Same |
| `CC_COORDINATOR_MODEL` | Subprocess coordinator only | `sonnet` (alias) | Override per-component |
| `MINER_CC_MODEL` | Subprocess miner only | `sonnet` | Override per-component |
| `REPAIR_CC_MODEL` | Subprocess repair | falls back to `MINER_CC_MODEL` | |
| `MINER_CC_BINARY` | Subprocess miner | `claude` | Path to the `claude` CLI |
| `CC_COORDINATOR_BINARY` | Subprocess coordinator | falls back to `MINER_CC_BINARY` | |
| `REPAIR_CC_BINARY` | Subprocess repair | falls back to `MINER_CC_BINARY` | |
| `MINER_BACKEND` | `miner/server.py` | `sdk` | `sdk`, `claude_code`, or `openai` |
| `COORDINATOR_BACKEND` | `validator/decomposer.py` | `sdk` | `sdk` or `claude_code` (openai coord is roadmap) |
| `REPAIR_BACKEND` | `validator/merge.py` | auto | Explicit override of repair backend |
| `MINER_OPENAI_API_KEY` / `OPENAI_API_KEY` | `miner/agent_openai.py` | (none) | Required when `MINER_BACKEND=openai` |
| `MINER_OPENAI_BASE_URL` / `OPENAI_BASE_URL` | `miner/agent_openai.py` | OpenAI default | Point at any OpenAI-compatible provider |
| `MINER_OPENAI_MODEL` / `OPENAI_MODEL` | `miner/agent_openai.py` | `gpt-4o-mini` | Provider's model id |
| `MINER_OPENAI_MAX_TOKENS` | `miner/agent_openai.py` | 4096 | Per-completion output cap |
| `MINER_OPENAI_MAX_API_CALLS` | `miner/agent_openai.py` | 40 | Hard cap on tool-loop turns |
| `COORDINATOR_LANGUAGE` | Coordinator profile resolver | autodetect | One of: python, typescript, java, csharp, c, cpp, rust |
| `MINER_LANGUAGE` | `agent_cc.py` test command + `test_runner.py` | autodetect via `COORDINATOR_LANGUAGE` | Per-language test verification |
| `MAX_COORDINATOR_RETRIES` | `decompose()` | 3 | |
| `SUBTASK_TIMEOUT_SECONDS` | Validator HTTP dispatch | 300 | |
| `MINER_TIMEOUT_SECONDS` | `demo/run_pipeline.py` | 1200 | Per-miner subprocess timeout |
| `BITSWARM_NO_CACHE` | Cache | unset | `1` disables decomposition cache |
| `BITSWARM_CACHE_DIR` | Cache | `~/.bitswarm/cache/decompositions` | Override cache location |
| `BITSWARM_SKIP_CRITIQUE` | Critique | unset | `1` skips self-critique pass |
| `BITSWARM_SKIP_PREFLIGHT` | Pre-flight | unset | `1` skips pre-flight check |
| `BITSWARM_STRICT_PREFLIGHT` | `run_pipeline.py` | unset | `1` aborts pipeline on pre-flight error |
| `BITSWARM_TEST_FIRST` | Subprocess coordinator | `1` (on) | `0` falls back to two-phase flow |
| `BITSWARM_DISABLE_REPAIR` | `merge.py` | unset | `1` aliases to `BITSWARM_REPAIR_MODE=off` |
| `BITSWARM_REPAIR_MODE` | `merge.py` | `patch` | `patch` / `replace` / `off` |
| `MINER_HOST` / `MINER_PORT` | `miner/server.py` | `0.0.0.0:8081` | |
| `VALIDATOR_HOST` / `VALIDATOR_PORT` | `validator/server.py` | `0.0.0.0:8080` | |


## Backend coverage

| Component | Default | SDK (`sdk`) | Subprocess (`claude_code`) | OpenAI-compat (`openai`) | Switch |
|---|---|---|---|---|---|
| Coordinator (Phase 1, 1.5, 2) | SDK | `validator/decomposer.py` | `validator/decomposer_cc.py` | not yet | `COORDINATOR_BACKEND` |
| Miner (subtask agent loop) | SDK | `miner/agent.py` | `miner/agent_cc.py` | `miner/agent_openai.py` | `MINER_BACKEND` |
| Repair miner | auto | `validator/repair.py` | `validator/repair_cc.py` | not yet | `REPAIR_BACKEND` |
| Self-critique | follows coord | yes | yes | n/a | follows `COORDINATOR_BACKEND` |
| Drop-and-replace | follows miner | yes | yes | yes (via agent_openai) | follows `MINER_BACKEND` |
| Cache | n/a | n/a | n/a | n/a | no LLM |
| Pre-flight | n/a | n/a | n/a | n/a | no LLM |
| Profile registry | n/a | n/a | n/a | n/a | no LLM |

Common combos:

- **All SDK**: bills Anthropic API tokens. Set `ANTHROPIC_API_KEY`.
  This was the `main` branch pre-merge.
- **All subprocess**: zero API spend, uses the user's Claude
  subscription via OAuth in `~/.claude/.credentials.json`. Set
  `COORDINATOR_BACKEND=claude_code MINER_BACKEND=claude_code`.
- **Generic OpenAI miner** (production miners' choice): point
  `MINER_OPENAI_BASE_URL` at any OpenAI-compatible endpoint and set
  `MINER_OPENAI_API_KEY` + `MINER_OPENAI_MODEL`. Coordinator stays on
  SDK or subprocess.
- **Mixed**: e.g. `COORDINATOR_BACKEND=sdk` (strongest planner) +
  `MINER_BACKEND=openai` with DeepSeek for cheap parallel mining.
  Supported by dispatch + tool layer; first end-to-end live run is
  the next polish task.


## Multi-language support

Live-tested end-to-end: **Python** (1.000/1.000) and **C++**
(builds + plays, ~0.5-0.75 score across runs due to scorer artifacts
since fixed).

Wired with profiles + parser support but not yet live-tested:

- TypeScript / JavaScript (tree-sitter-typescript)
- Java (tree-sitter-java)
- C# (tree-sitter-c-sharp)
- C (tree-sitter-c)
- Rust (tree-sitter-rust)

Each profile carries: `display_name`, `extensions`, `phase2_intro`,
`stub_rules`, `test_rules`, `integration_rules`,
`integration_test_filename`, `test_command_hint`, plus alias mapping.
Resolution via explicit arg → `COORDINATOR_LANGUAGE` env → repo
auto-detect (Cargo.toml → rust, package.json → typescript, etc.) →
Python fallback.

A first run on each language will likely surface small profile
tweaks. The architecture is in place to add them.


## Pipeline walkthrough

What happens when you run:

```bash
git checkout claude-code-backend
COORDINATOR_BACKEND=claude_code MINER_BACKEND=claude_code \
  BITSWARM_REPAIR_MODE=replace \
  python demo/run_pipeline.py --spec demo/spec_wordle.txt --out out/run1
```

1. **Setup**: `out/run1/workspace/repo` copied from `demo/target_repo`,
   git init + commit.

2. **Cache check**: `validator/cache.py` hashes (spec, repo files,
   "python", model, "claude_code") into a key. If the cache file
   exists and validates → return cached decomposition, skip to
   step 7.

3. **Phase 1**: `validator/decomposer_cc.py:call_coordinator` runs
   `claude -p` with the Phase 1 prompt. Output: a JSON plan with
   `subtasks`, `shared_files`, `requirements_additions`.

4. **Phase 1.5** (test-first): another `claude -p` call writes the
   integration test files to a tempdir. These become the contract.

5. **Phase 2**: a third `claude -p` call writes stub files + stub
   tests to another tempdir. Phase 2 sees the Phase 1.5 integration
   tests inline as the contract. Files harvested back into the
   decomposition dict.

6. **Critique pass**: another `claude -p` call reads all generated
   files and reports `ISSUE: ...` lines for cross-file drift.
   Folded into the validate-decomposition error list.

7. **Validate decomposition**: `validator_checks.py` runs the full
   Phase 1.5 check suite (parsers, contracts, arity, weights,
   cycles, etc.). If errors → retry coordinator with errors as
   feedback.

8. **Cache save**: validated decomposition written to the cache
   file.

9. **Scaffold**: `validator/scaffolder.py` writes all files to disk,
   creates `__init__.py`s, commits as `BitSwarm scaffolding`.

10. **Pre-flight**: `validator/preflight.py` runs the language's
    compile-check (Python imports, tsc, make, etc.). Errors are
    advisory by default; with `BITSWARM_STRICT_PREFLIGHT=1` they
    abort the pipeline.

11. **Miners**: `demo/run_pipeline.py:_run_miners` topo-sorts the
    subtasks and runs `miner.agent_cc.execute_subtask` sequentially
    (or could be parallel; sequential keeps the demo legible). Each
    miner gets a copy of the scaffolded repo. Claude Code subprocess
    runs in that workspace, edits stub files, runs the per-language
    test command, iterates. Patch is `git diff <scaffolding> -- <allowed_files>`.

12. **Merge + cross-compile**: `validator/merge.py:merge_and_test`
    computes tiers, applies patches per tier, re-runs each miner's
    tests against the merged tree:
    - Pass → commit tier
    - Fail + `REPAIR_MODE=replace` → drop-and-replace (re-mine in
      merged context), apply new patch
    - Fail + `REPAIR_MODE=patch` → repair miner edits failing code
    - Fail + `REPAIR_MODE=off` → skip, score 0 for this subtask

13. **Integration tests**: run against the fully merged repo.

14. **Score**: per-subtask `weight * integration_multiplier`. Print
    summary + path to merged repo.


## Roadmap

Ordered by leverage. Items in `()` are rough complexity estimates.

### Near-term polish

1. **Live-test the wired-but-untested combos** (small)
   - Drop-and-replace against the C++ Wordle game-subtask failure
     mode it was built for.
   - Each of the 5 untested languages end-to-end on a simple spec.
   - Mixed backends (SDK coordinator + subprocess miner).
   - Each turns into one or two tweaks per language profile.

2. **Coordinator notebook** (small)
   - A short per-task markdown file the coordinator updates after
     each phase: "I chose to put Color in shared types because both
     scorer and renderer use it" / "I pinned Game's ctor to single
     string". Phase 2 sees it; critique sees it. Cheap intra-task
     memory without inter-miner coordination.

3. **Hardening of subprocess paths** (small)
   - Better stdout capture / debug logging on coordinator phase
     failures.
   - Per-phase timeouts that don't kill the whole pipeline.
   - Retry on transient subprocess errors (claude CLI sometimes
     stalls; we have it for SDK already).

### Multi-provider model support

Goal: any model. Anthropic, OpenAI, Gemini, DeepSeek, Chutes-served
models, locally-hosted LLMs via vLLM/Ollama, etc.

**Done (in this branch):**

- **OpenAI-compatible miner backend.** `miner/agent_openai.py` drives
  any Chat-Completions-shaped provider. Tool translation (Anthropic
  `input_schema` -> OpenAI `function.parameters`) is unit-tested.
  Dispatch via `MINER_BACKEND=openai`. Covers DeepSeek, OpenRouter,
  Together, Fireworks, Groq, vLLM, llama.cpp, Ollama, Anthropic
  via OpenAI-compat, etc.

**Remaining:**

4. **Live-test the openai backend end-to-end** (small)
   - Run the Python Wordle pipeline with `MINER_BACKEND=openai`
     against DeepSeek-Coder and a local vLLM, capture score deltas.
   - Surface and fix the inevitable small things (tool_choice
     quirks per provider, token-limit defaults, retry markers).

5. **Coordinator on the openai backend** (~half week)
   - Today coordinator is SDK-only or claude-code-only. Add
     `validator/decomposer_oai.py` so a fully-self-hosted setup
     (vLLM coord + vLLM miners) is possible.
   - Same shape as `decomposer_cc.py`; the heavy lifting is sharing
     the Phase 1 / 1.5 / 2 prompt set across backends without
     copy-paste drift.

6. **Native Gemini backend** (~1 week)
   - Gemini's function-declaration shape is different enough from
     OpenAI's that an OpenAI-compat wrapper is awkward. Adapter
     under `miner/agent_gemini.py` (or via Vertex's OpenAI-compat
     endpoint if that's good enough on tool use).

7. **Prompt caching strategy per provider** (~1 week)
   - Anthropic: explicit `cache_control` markers (current behavior).
   - OpenAI / DeepSeek: automatic prefix caching on supported models;
     just keep the warm-start message at the front. Already true.
   - Gemini: implicit caching via the SDK; needs different message
     ordering.
   - The coordinator's repeated prompts (Phase 1 shares the repo
     dump) are the biggest dollar lever once it works across all
     providers.

8. **Streaming output for non-Anthropic** (~half week)
   - `decomposer.py:stream_json` is Anthropic-streaming. Generalize
     for the openai coordinator when (5) lands.

### Bittensor subnet (Phase 5)

8. **Axon / Dendrite / synapse** (~3 weeks)
   - Replace the FastAPI miner + httpx-based validator dispatcher
     with the Bittensor primitives.
   - Define `TaskSynapse` matching our `TaskAssignment` schema and
     `MinerSynapse` for `MinerResponse`.
   - Validator becomes a `bt.Validator` with subnet config; miners
     become `bt.Miner` with axon serving.
   - Heartbeat / status synapse for miner availability.

9. **Weight setting + scoring loop** (~1 week)
   - After each task, validator computes per-miner scores (we
     already do this in `validator/scorer.py`).
   - EMA over recent tasks per miner UID.
   - `subtensor.set_weights()` per epoch.

10. **Metagraph queries** (~1 week)
    - Validator picks miners from the metagraph (active + adequate
      stake). Currently we hardcode miner URLs.
    - Health check / availability filter via the StatusSynapse.

11. **Real economics + incentives**
    - Decide on emission rates, reward curves, validator/miner
      split.
    - Implement validator stake requirement.
    - Burn or redistribute uncollected emissions.
    - Likely needs Bittensor-team consultation; not just code.

### Verification + anti-collusion

12. **Cryptographic test commitments** (~2 weeks)
    - Today the coordinator generates `stub_test_files` and
      `integration_test_files`; miners can read these and tailor
      their patches to pass. In an adversarial setting that's a
      collusion vector.
    - Validator commits to a test hash; reveals after miners submit.
    - Or: blinded test execution where the validator runs miners'
      code against test inputs miners never see.

13. **Cross-miner adversarial review** (~1 week)
    - Each miner reviews two others' patches before merge votes on
      whether they're internally consistent.
    - Validator weighs the votes.
    - Catches miners gaming tests when individual reviewers don't.

14. **Sandboxing** (~2 weeks)
    - Currently miners run with full Bash in a workspace dir. In a
      decentralized setting that's a serious security hole.
    - gVisor / Firecracker / Docker-with-no-network per miner run.
    - File system writes confined to workspace.

### v2 features

15. **Diff-style refactor mode** (~3-4 weeks)
    - Instead of "given a spec, build new code from stubs," support
      "given an existing codebase and a change request, generate
      coordinated patches across files."
    - Coordinator must understand existing code shape, not just
      design from scratch.
    - Miners apply edits to live files rather than fresh stubs.
    - Scoring shifts: "did the change land cleanly + did existing
      tests still pass + did new tests pass?".
    - Unlocks the biggest commercial use cases: refactoring,
      migrations, bug-fix swarms.

16. **Cross-task organizational memory** (~2 weeks)
    - Today the cache stores (spec, decomposition) pairs. Org-memory
      stores design patterns: "every chess-engine decomposition has
      handled castling rights at the move-validation tier, not the
      board representation tier."
    - Embedding-based retrieval of past similar decompositions.
    - Used as context during Phase 1 planning.

17. **Specialist miner pools** (~2 weeks)
    - Different miners optimized for different work types
      (algorithmic, web, systems, glue).
    - Validator routes subtasks by detected type.
    - Maps to subnet economics: miners stake on specialties they're
      good at.

18. **Wall-clock parallelism** (~1 week)
    - Today `demo/run_pipeline.py` runs miners sequentially for
      legibility. Real production should parallelize within tiers
      (tier-0 subtasks have no deps; run them concurrently).
    - The HTTP layer already supports this; just need
      `asyncio.gather` in the runner.


## Open issues / known limitations

- **C++ parser doesn't handle overloaded constructors.** Phase 1.5
  registers only the last-seen constructor. Spec pinning helps but
  isn't a fix.
- **TypeScript / Java / C# / Rust parsers haven't been live-tested.**
  Architecture is correct; first real runs will surface profile
  tweaks.
- **`make test` in C/C++ runs ALL binaries.** For the miner's
  in-isolation test we use a targeted `make tests/test_<sid>`
  command, but the cross-compile step in merge still uses
  per-test-file paths. Mixed mode works but reads weirdly.
- **Subprocess coordinator's Phase 2 silently produced empty output
  on large prompts when `--output-format json`.** Worked around by
  the file-writing pattern. If `claude-code` fixes the envelope size
  cap, we could go back to inline JSON.
- **The validator is trusted.** Single point of failure for both
  decomposition quality and verification honesty.
- **No multi-validator setup.** Each task runs against one
  validator. Bittensor's normal pattern.
- **Drop-and-replace's `MinerResult` overwrite race.** If two miners
  in the same tier fail and both invoke drop-and-replace
  sequentially, the second sees the first's replacement in
  `merge_repo`. That's actually probably good (more correct
  context) but worth knowing.
- **Pre-flight skips Java + C#** due to build-system weight. They'd
  benefit from a stub `dotnet build` / `mvn compile` pre-step but
  the cost is non-trivial.
- **Repair miner integration repair only fixes integration tests,
  not the impls.** A real interface drift sometimes needs both
  sides patched. The integration-repair prompt could trigger
  drop-and-replace on the offending subtask instead.


## Quick start

For someone picking up the repo for the first time.

```bash
git clone https://github.com/RyanMercier/BitSwarm.git
cd BitSwarm
pip install -r requirements.txt
python -m pytest tests/                    # 311 tests, ~25s

# Option A: Anthropic API
export ANTHROPIC_API_KEY=sk-ant-...
python demo/run_pipeline.py --spec demo/spec_wordle.txt --out out/run1

# Option B: Claude subscription (Max/Pro/Team), zero API spend
npm install -g @anthropic-ai/claude-code
claude auth login
export MINER_BACKEND=claude_code COORDINATOR_BACKEND=claude_code
python demo/run_pipeline.py --spec demo/spec_wordle.txt --out out/run1

# Option C: any OpenAI-compatible provider (DeepSeek, OpenRouter,
#           Together, Groq, vLLM, Ollama, ...). Coordinator stays on
#           SDK or claude_code; only the miner switches.
export MINER_BACKEND=openai
export MINER_OPENAI_API_KEY=sk-...
export MINER_OPENAI_BASE_URL=https://api.deepseek.com
export MINER_OPENAI_MODEL=deepseek-chat
python demo/run_pipeline.py --spec demo/spec_wordle.txt --out out/run1

# Then poke at the result:
cd out/run1/merged_repo
python -m pytest tests/
python -m wordle    # play the game BitSwarm just built
```

For a C++ run:

```bash
export MINER_LANGUAGE=cpp COORDINATOR_LANGUAGE=cpp
python demo/run_pipeline.py --spec demo/spec_wordle_cpp.txt --out out/cpp_run
cd out/cpp_run/merged_repo
make test
./wordle_bin
```

For the docker stack (Phase 4):

```bash
cd docker
echo "ANTHROPIC_API_KEY=sk-ant-..." > ../.env
docker compose --env-file ../.env up --build
# then POST to http://localhost:8080/submit
```


## File map

```
BitSwarm/
├── BITSWARM_SPEC.md            full architecture spec from the POC
├── CLAUDE.md                   project-local instructions
├── README.md
├── config.py                   env-var resolution
├── requirements.txt
│
├── protocol/
│   ├── schemas.py              pydantic TaskAssignment / MinerResponse
│   └── transport.py            git bundle <-> base64
│
├── miner/
│   ├── server.py               FastAPI server + backend selection
│   ├── agent.py                SDK miner (Anthropic tool-use loop)
│   ├── agent_cc.py             subprocess miner (claude -p)
│   ├── agent_openai.py         OpenAI-compatible miner (any provider)
│   ├── tools.py                tool definitions for SDK + openai miners
│   ├── recovery.py             retry / hard-reset state machine
│   ├── warm_start.py           cached warm-start prompt builder
│   └── prompts.py              SDK miner system prompt
│
├── validator/
│   ├── server.py               FastAPI orchestrator
│   ├── lang_profiles.py        per-language metadata registry
│   ├── decomposer.py           SDK coordinator (Phase 1, 1.5, 2)
│   ├── decomposer_cc.py        subprocess coordinator
│   ├── critique.py             self-critique pass
│   ├── preflight.py            scaffold compile-check
│   ├── cache.py                decomposition cache
│   ├── scaffolder.py           write stubs + git commit baseline
│   ├── validator_checks.py     Phase 1.5 dispatcher
│   ├── validator_checks_common.py  language-agnostic contract check
│   ├── parsers/
│   │   ├── types.py            LanguageParser protocol + dataclasses
│   │   ├── python.py           stdlib ast
│   │   ├── typescript.py       tree-sitter-typescript
│   │   ├── java.py             tree-sitter-java
│   │   ├── csharp.py           tree-sitter-c-sharp
│   │   ├── c.py                tree-sitter-c
│   │   ├── cpp.py              tree-sitter-cpp
│   │   └── rust.py             tree-sitter-rust
│   ├── test_runners.py         build-system dispatch
│   ├── test_runner.py          merge-time test runner
│   ├── merge.py                tiered merge + recovery
│   ├── drop_replace.py         drop-and-replace recovery
│   ├── repair.py               SDK repair miner
│   ├── repair_cc.py            subprocess repair miner
│   ├── scorer.py               per-subtask scoring
│   └── prompts.py              coordinator system prompt
│
├── docker/
│   ├── Dockerfile.miner
│   ├── Dockerfile.validator
│   └── docker-compose.yml
│
├── demo/
│   ├── run_pipeline.py         in-process end-to-end runner
│   ├── smoke_miner_cc.py       single-miner smoke
│   ├── smoke_coordinator_cc.py single-coord smoke
│   ├── spec_wordle.txt         Python demo
│   ├── spec_wordle_cpp.txt     C++ demo
│   ├── spec_minidb.txt         stretch demo
│   └── target_repo/            empty starter repo
│
├── tests/                      311 tests
│   ├── test_protocol.py
│   ├── test_transport.py
│   ├── test_dispatch.py
│   ├── test_bug_fixes.py
│   ├── test_validator_checks_python.py
│   ├── test_validator_multilang.py
│   ├── test_lang_profiles.py
│   ├── test_parsers_c.py       (and 5 more: cpp, csharp, java, rust, typescript)
│   ├── test_cache.py
│   ├── test_critique_preflight.py
│   ├── test_test_first.py
│   ├── test_drop_replace.py
│   └── test_backends.py        backend dispatch + tool translation
│
└── docs/
    └── STATUS.md               this file
```
