# BitSwarm local coding harness

The new `harness/` package is a Linux/WSL implementation of a bounded
implementer -> independent reviewer -> repair -> verifier workflow. The older
miner/validator/neuron code remains experimental and is not the default harness.

## Run the offline demo

From the repository root, with Python 3.11+ and Git installed:

`uv sync --frozen` installs the locked local package; subsequent commands can
also use `uv run bitswarm` in place of `python -m harness`.

```bash
uv run --no-project --with pytest==8.3.5 python -m harness demo \
  --out out/my-demo --sandbox unsafe-local
uv run --no-project --with pytest==8.3.5 python -m harness replay out/my-demo
```

Open `out/my-demo/report.html`. This is explicitly a **scripted fixture**, not
a recorded success by live models: an implementer misses the upper clamp bound,
an independent reviewer identifies the defect, and a repair fixes it. The same
state machine and verifier run real provider-backed sessions.

`unsafe-local` is only for trusted fixtures. It runs repository tests on the host;
minimal environment forwarding is not a security boundary. It never activates
automatically. The default mode requires Docker and fails before model spend if
the daemon/image is unavailable.

For isolated execution:

```bash
docker build -f docker/Dockerfile.harness -t bitswarm-harness:local .
uv run --no-project --with pytest==8.3.5 python -m harness demo --out out/container-demo
```

The provided image supports stdlib-only Python projects plus pytest. Build a
task-specific dependency image for other repositories. Dependency resolution is
an explicit preparation step, never an agent-triggered host pip install. Runtime
records the resolved image ID and reuses that ID for subsequent gates and replay.

For Snap Docker, the daemon may see a private `/tmp`. Choose an existing staging
directory visible at the same absolute path to the host and daemon:

```bash
mkdir -p "$PWD/out/docker-gates"
export BITSWARM_GATE_TMPDIR="$PWD/out/docker-gates"
```

Bind mounts use `--mount`, which fails when the daemon cannot find the source
instead of silently creating an empty directory. Remote Docker daemons need
shared paths and are not validated by the local pilot. Docker client environment
is held constant within each verifier instance; it is not forwarded into tests.

## What works

- Git commit snapshots, per-run workspaces, actual changed-path scope checks.
- Structured read/search/patch/development-test actions with no direct model shell.
- SQLite state and events, exclusive runner locks, step-boundary pause/resume.
- Separate reviewer context: task, patch, files, and its own observations; no
  implementer justification is injected into initial review.
- One review/repair cycle; immutable final acceptance tests withheld from models.
- Independent baseline/regression/acceptance gates using pytest JUnit XML.
- Previously passing tests that go missing, become skipped, or fail reject the
  candidate. A newly added test that skips is recorded, not treated as a
  regression. Every acceptance test must run and pass; a skipped contract test
  is a failure.
- Per-test errors (fixture setup, teardown) count against that test only, so a
  baseline with a few broken tests can still be evaluated by set difference.
- Missing runners, timeouts, collection errors, empty collections, invalid
  reports, and test runs that rewrite their own source are inconclusive.
- A reviewer has its own turn budget (`max_review_turns`, default 8). Exhausting
  it records no findings and proceeds to verification instead of starving it.
- No model can edit acceptance tests through harness tools.
- Artifact hashes, patch export, verification replay, escaped static HTML traces.
  Replay records `environment_match` rather than refusing when the verifier or
  image has changed since the original verdict; both environments are kept.
- Patches may use `a/` `b/` prefixes or plain `--- path` headers; plain headers
  gain prefixes before git sees them so directory paths survive `-p1`.
- Explicit fixture, Claude CLI, and OpenAI-compatible Chat Completions adapters.
- A small task/condition evaluation runner with cost incompleteness preserved.

## Pause, resume, inspect, cancel

```bash
python -m harness demo --out out/paused --sandbox unsafe-local --steps 2
python -m harness inspect out/paused
python -m harness resume out/paused
python -m harness report out/paused
python -m harness cancel out/paused
```

Pauses at completed action boundaries are resumable. A crash during an uncertain
model call or tool action is **inconclusive**, not silently retried. Start a new
attempt after inspecting its trace. Candidate/contract tampering fails checkpoint
validation. Cancellation is checked at action boundaries; ongoing operations have
individual timeouts. Budgets limit model calls and recorded execution time; a
running operation may consume its bounded timeout before the next budget check.

## Run real models

Example `models.json`:

```json
{
  "sandbox": "docker",
  "image": "bitswarm-harness:local",
  "max_turns": 20,
  "max_review_turns": 8,
  "max_seconds": 600,
  "call_timeout": 90,
  "gate_timeout": 60,
  "max_repo_files": 3000,
  "max_repo_mb": 30,
  "implementer": {"kind": "claude-cli", "binary": "claude", "call_budget": 0.5},
  "reviewer": {
    "kind": "openai-compatible",
    "endpoint": "https://api.openai.com/v1",
    "model": "YOUR_MODEL_ID",
    "key_env": "OPENAI_API_KEY"
  }
}
```

```bash
python -m harness run --repo /absolute/repository --base HEAD \
  --spec request.md --contract /absolute/acceptance-tests \
  --allow package/module.py --config models.json --out out/real-run
```

Set only the API-key environment variable needed by the selected HTTP adapter.
The Claude CLI adapter removes inherited Anthropic API credentials, disables CLI
tools/hooks/MCP auto-discovery, and calls from an empty temporary directory. It
passes repository context explicitly; the CLI does not receive workspace access.
Authenticate the CLI beforehand. Claude subscription automation may use a separate
credit allocation; do not assume interactive Max allowances cover these calls.
The CLI per-call budget is a provider-side estimate, not a universal billing cap.

The compatible adapter currently targets nonstreaming Chat Completions responses
with text JSON actions and a `max_tokens` limit. Providers/models needing another
request shape must use a dedicated adapter; do not silently drop output limits.
HTTP token usage is recorded but cost remains unknown without a pricing policy.

The two provider adapters have contract tests. Live solo and reviewer workflows
using Fable 5.1 were exercised in the [synthetic pilot](evidence/pilot/README.md).
This validates separate role contexts using one model family. Mixed-provider
collaboration remains unmeasured.

## Verify an existing patch without an LLM

```bash
python -m harness verify --repo /absolute/repository --base HEAD \
  --contract /absolute/acceptance-tests --patch change.diff \
  --allow package/module.py --out out/verify-run
```

The baseline must be a Git commit. Uncommitted source changes are deliberately not
included. Existing tests currently reside under `tests/`. Acceptance files must be
pytest files in a separate directory. Scopes are exact regular-file paths; test
files, test configuration, dotfiles, symlinks, and submodules are outside initial
support. Keep the run output outside the source repo for large real tasks.
Snapshots default to 3,000 tracked files and 30 MB; raise `max_repo_files` and
`max_repo_mb` in the config for larger repositories. Export uses one `ls-tree`
and one `cat-file --batch`, so cost does not grow with git process count.

## Evaluation

`python -m harness evaluate --manifest experiment.json --out out/experiment`

```json
{
  "repeats": 3,
  "limits": {"max_turns": 20, "max_seconds": 600, "sandbox": "docker"},
  "tasks": [{"name": "task-1", "repo": "/absolute/repo", "base": "COMMIT",
    "contract": "/absolute/tests", "spec": "Bounded change", "allowed": ["src/module.py"]}],
  "conditions": [{"name": "solo", "config": {
    "implementer": {"kind": "claude-cli", "call_budget": 0.5}}}]
}
```

Add reviewer conditions explicitly. Keep total budgets equal; count failed runs
in cost per accepted task. This command invokes the configured live models and
consumes their usage. Fixture runs measure wiring only. It writes raw run rows,
per-run reports, and a summary; it does not infer statistical superiority.

Evaluation is resumable. Rerunning with the same manifest and output directory
reuses every run that reached a terminal phase and resumes any that did not;
a different manifest against the same directory is refused.

## Trust boundaries and honest limitations

The local operator, baseline selection, contracts, runtime, Docker daemon, and
dependency image are trusted. Models have scoped tools and cannot directly modify
run state. Tests run on disposable copies. Production Docker mode has no network,
read-only source/contract mounts, dropped capabilities, nonroot user, read-only
root filesystem, and memory/process/CPU limits. Passing tests is behavioral
evidence, not proof of correctness or resistance to every malicious submission.

Python source and pytest run in the same interpreter: deliberately hostile code
can interfere with test execution/reporting inside that interpreter. This harness
does **not** claim a cryptographically trustworthy hostile-miner judge. Stronger
adversarial verification needs external process-boundary checks and independent
oracles. Existing contract tests also cannot prove an arbitrary natural-language
request is fully satisfied.

Local traces contain prompts, source excerpts, patches and test output. Keep runs
private and review/redact before publishing; no automatic source-secret scrubber
is claimed. No hidden chain-of-thought is collected. Model calls are bounded but
the CLI adapter is not a substitute for a provider billing control.

Not yet shipped: automatic recovery of uncertain actions, provider failover,
learned routing, parallel implementers, streaming transport, a statistically
powered repository benchmark, Windows-native execution, or multi-tenant hosting.

## Tests

```bash
uv run --no-project --with pytest==8.3.5 python -m pytest tests/test_harness.py
```

The full legacy suite additionally needs the dependencies in `requirements.txt`.
Dedicated regression tests in `tests/test_review_regressions.py` cover the earlier
false-positive regression collector and patch-scope failures.
