# Validation record, September 17, 2026

All results below were obtained locally in Ubuntu on WSL. Remote CI has not been
run or claimed. The Docker daemon is version 29.6.1. Container verification used
image `sha256:15a6fa48ed5a7640677e2c5e81d4073c98a233abe78de433c9619beb7739acac`.
Per-run provenance and runtime file hashes are in the evidence directories.

| Check | Observed result |
|---|---|
| Full repository suite | 381 passed, 7 skipped; one upstream Starlette deprecation warning |
| Focused harness unit suite | 35 passed; included in the repository total |
| Opt-in live Docker suite | 6 passed; these were 6 of the skipped tests in the default run |
| Original live pilot | 3/3 solo and 3/3 reviewed passed the original contracts |
| Original saved-patch replay | All 6 reproduced PASS |
| Post-hoc retry-parser audit | Both previously passing candidates failed extended probes |
| Fresh strengthened-contract runs | 0/1 solo, 0/1 reviewed passed; failures retained |
| Strengthened-contract replay | Both reproduced FAIL |
| Ruff / diff whitespace | Passed on new harness, benchmark, and focused test code |

The test counts include existing legacy coverage; they are not all new tests.
Pilot acceptance counts measure the named finite contracts. Do not combine
original and strengthened-contract outcomes into one performance claim.

The live pilot used the authenticated Claude CLI with `claude-fable-5-1`. CLI
tools/hooks were disabled, repository context was explicit, and candidate tests
ran in containers. The model roles use the same model family. No second provider
or mixed-provider performance result is claimed.

## Commands

```bash
uv run python -m pytest tests/test_harness.py -q -p no:cacheprovider
BITSWARM_DOCKER_TESTS=1 uv run python -m pytest tests/test_harness_docker.py -q -p no:cacheprovider
uv run --with ruff ruff check harness benchmarks tests/test_harness.py tests/test_harness_docker.py
git diff --check
```

The full legacy suite was run in a uv environment with pytest 8.3.5, anthropic,
openai, fastapi, httpx, gitpython, tree-sitter, and the TypeScript, Java, C#, C,
C++, and Rust tree-sitter grammar packages, using `python -m pytest tests/ -q
-p no:cacheprovider`.

The workstation's Snap Docker daemon has a private `/tmp`. The test launcher
provided a daemon-visible `BITSWARM_GATE_TMPDIR`. WSL root built the image and
ran container checks. Live model launches dropped to the normal user's UID with
socket access only in that process's supplementary groups; permanent account
groups and socket ACLs were not modified. The portable runtime does not perform
that privilege setup.

## Addendum after the second review pass (same day)

| Check | Observed result |
|---|---|
| Focused harness unit suite | 43 passed (eight new regression tests) |
| Validator regression suite | 7 passed with the provider SDKs installed |
| Diff whitespace | Passed |

The live Docker suite and the pilot were not rerun after these changes. The
evidence directories still carry the hashes of the runtime that produced them,
so a replay under the current verifier reports `environment_match: false` and
records both environments. See `docs/HARNESS_REVIEW.md` for the fixes.
