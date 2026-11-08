# BitSwarm Verify: GitHub Action

Verify a pull request against acceptance tests the pull request cannot see or
edit, in a network-less container, with a verdict you can replay later. No
model is involved. It is built for pull requests written by coding agents, where
"the tests pass" is only meaningful if the agent could not touch the tests.

## What it checks

1. **Scope.** Every changed path must match the `allow` patterns. With
   `protect-tests` on (the default), test files, test directories, dotfiles,
   and Python packaging or pytest configuration are refused regardless of
   `allow`. A pull request that weakens a test fails before any test runs, and
   the summary says which path caused it. Repositories whose human pull
   requests routinely edit tests can turn protection off and rely on the
   held-out contract and the regression gate.
2. **Regression.** The existing suite under `tests/` runs on the base commit and
   on the candidate. Any previously passing test that no longer passes fails
   the verdict. Tests already broken at base are reported, not blamed.
3. **Acceptance.** The directory named by `contract-path` is read from
   `contract-ref` (the base branch by default), copied out of git, and run
   against the candidate. The pull request's own copy of that directory is
   ignored.

The verdict is PASS, FAIL, or INCONCLUSIVE. Timeouts, collection errors,
missing reports, a sandbox that is unavailable, and test runs that rewrite
their own source are INCONCLUSIVE and fail the step by default. They are never
reported as a pass.

## Minimal workflow

```yaml
name: Verify pull request
on: [pull_request]
permissions:
  contents: read
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: RyanMercier/BitSwarm@main
        with:
          contract-path: acceptance
          allow: 'src/* README.md'
```

Put pytest files in `acceptance/` on your base branch. Merge them before the
agent's pull request opens, or the contract is not held out. The action's
default image is `python:3.12-slim` plus pytest; if your code needs
dependencies, point `dockerfile` at one that installs them and keeps
`USER 65534:65534` (see `docker/Dockerfile.self` in this repository for the
image BitSwarm verifies itself with).

Gate only agent-authored pull requests by labelling them and adding
`if: contains(github.event.pull_request.labels.*.name, 'ai-authored')` to the
job.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `base` | pull request base sha | Baseline commit |
| `head` | `github.sha` | Candidate commit. On pull requests this is the merge result |
| `contract-path` | `acceptance` | Directory of pytest files, read from `contract-ref` |
| `contract-ref` | base | Commit or branch the contract is read from |
| `allow` | `*` | Whitespace-separated fnmatch patterns; `*` matches across `/` |
| `image` | `bitswarm-verify:local` | Sandbox image tag; built from `dockerfile` |
| `dockerfile` | action's stdlib image | Dockerfile relative to the repository |
| `gate-timeout` | `300` | Seconds per gate, up to 600 |
| `max-repo-files`, `max-repo-mb` | `3000`, `30` | Snapshot limits |
| `pids-limit`, `memory-mb` | `64`, `512` | Processes plus threads, and memory, per test container |
| `fail-on-inconclusive` | `true` | Fail the step on INCONCLUSIVE |
| `protect-tests` | `true` | Refuse changes to tests and configuration. Turn off for repositories whose human pull requests edit tests; the contract stays held out |
| `comment` | `false` | Post the summary as a pull request comment (needs `pull-requests: write`) |
| `artifact` | `full` | Upload the whole run directory (`full`), the summary files, or nothing |

Outputs: `outcome`, `patch-hash`, `run`.

## What you get back

- A job summary with the verdict, base and head, the contract ref, the patch
  hash, the image ID, per-gate test counts, each gate's peak processes and
  memory against its limits, and the acceptance tests that did not pass.
- A gate that fails after its container hit the process or memory limit is
  INCONCLUSIVE, with the cgroup counters in its reason. An exhausted sandbox is
  never reported as a regression in the patch.
- An artifact containing the run directory. Download it and run
  `bitswarm replay <dir>` to rebuild the candidate from the recorded baseline
  and patch and rerun the gates. The result says whether the verifier and image
  still match the ones that produced the original verdict.

## Running it locally

```bash
pip install .
python -m harness.action --repo . --base origin/main --head HEAD \
  --contract-path acceptance --out out/verify --image bitswarm-harness:local
```

Add `--sandbox unsafe-local` to skip Docker for trusted code; tests then run on
the host.

## Limits in v0

- Python and pytest only. Existing tests must live under `tests/`.
- One image per repository. Dependency resolution is a build step, not
  something the action infers.
- Snapshots are capped at 3,000 tracked files and 30 MB by default.
- Renames appear as a delete and an add, so both paths must be allowed.
- The container is a strong boundary against accidents and a weak one against
  deliberately hostile test code: candidate code and pytest share an interpreter.
- The contract must already exist on `contract-ref`. For the first pull request
  that adds it, set `contract-ref` to `${{ github.sha }}` once, then remove it.
