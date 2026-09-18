# Harness review evidence

On September 17, 2026, Claude Fable 5.1 (`claude-fable-5-1`) reviewed
`harness/runtime.py` and `harness/verify.py` through the authenticated Claude CLI.
The helper received source text with tools and hooks disabled. This was a live
code review, not a live multi-model benchmark run.

Both reported findings were confirmed and fixed:

1. Docker verification extracted JUnit XML from a 16 KB display tail. Larger
   suites lost the opening marker and became inconclusive. Extraction now uses
   the full log with a 2 MB bound; only displayed output is truncated.
2. Image inspection and cleanup inherited the caller's Docker environment,
   while execution used a temporary HOME and omitted Docker configuration.
   Every Docker client operation now uses the same environment snapshot.
   These client variables are not automatically forwarded into the container.

The regression test supplies 500 test records and changes DOCKER_HOST after
verifier creation. It checks full report parsing and identical environments for
inspection, execution, and cleanup. Docker calls are mocked; this does not prove
container isolation or daemon compatibility.

Additional local regression tests cover malformed-patch recovery, refusing to
resume after verifier changes, and rejecting gate-level failures with no test
records. The offline fixture also exercises pause, resume, and saved-patch replay.

Follow-up validation used the existing Docker daemon through WSL root without
changing the socket ACL or permanent user groups. Live model runs drop to the
normal user's UID, with socket access inherited only by the launcher process.
Containers continue to execute as UID 65534. This is a workstation-specific
launcher, not part of the portable runtime or a multi-tenant security boundary.

Actual Docker execution exposed Snap's private `/tmp`: volume shorthand mounted
empty directories instead of the intended test tree. The harness now accepts
`BITSWARM_GATE_TMPDIR` for daemon-visible staging and uses explicit bind mounts
that fail if the source is missing. Six live integration tests pass, including
500-test XML, timeout, source mutation, collection failure, assertion failure,
and nonroot/network/read-only restrictions. See the pilot evidence for live
model results. Remote CI has not been claimed as executed.

## Second review pass (September 17, 2026, Claude Fable 5.1 in Claude Code)

A second read of the harness and the validator changes found two defects and
several robustness gaps. All were fixed with regression tests in
`tests/test_harness.py` and `tests/test_review_regressions.py`.

1. **Per-test errors collapsed the regression gate.** `parse_report` classified
   any `<error>` testcase as INCONCLUSIVE. The legacy diff-mode collector then
   replaced the whole failing set with the suite sentinel, and the regression
   gate failed for every miner whenever the baseline suite contained one
   erroring fixture. Reproduced with a three-test file (pass, fail, fixture
   error). Errors are now per-test statuses; collection errors and interrupted
   runs still arrive as non-0/1 exit codes and remain inconclusive.
2. **Traditional diffs lost their directory.** `git apply` strips one leading
   path component by default, so `--- src/module.py` resolved to `module.py`
   and failed the scope check in both the harness and the validator.
   `normalize_diff_prefixes` now adds `a/` and `b/` to plain headers, skipping
   hunk bodies by line count so removed lines starting with `--` are untouched.
3. Mutation of the source tree during a test run is INCONCLUSIVE in both
   sandbox modes (it was FAIL locally and INCONCLUSIVE in Docker).
4. Replay records `environment_match`, the recorded environment, and the replay
   environment instead of refusing to run under a changed verifier or image.
5. Adapters are constructed in `create_run`, so a misspelled `kind` fails before
   the repository is snapshotted.
6. The reviewer has its own turn budget (`max_review_turns`); exhausting it
   proceeds to verification with no findings and an event in the trace.
7. `read` uses byte offsets on a binary handle and decodes with replacement, so
   a multibyte split or a non-UTF-8 file no longer fails the action.
8. `evaluate` resumes: finished runs are reused, unfinished ones are resumed,
   and a different manifest against the same directory is refused.
9. `snapshot` uses one `ls-tree -l` and one `cat-file --batch` instead of two
   git processes per file, and the file and byte limits are configurable.
10. The Claude CLI adapter checks the exit code before parsing JSON.
11. Dead integration-repair selection removed from `validator/merge.py`; the
    acceptance contract is never repaired.
12. CI now installs the provider SDKs and runs the validator regression tests.

After these changes the harness suite has 43 tests and the regression suite 7.
The evidence directories record the runtime hashes that produced them; those
hashes no longer match the current source, and replays under the current
verifier will report `environment_match: false` with both environments kept.
