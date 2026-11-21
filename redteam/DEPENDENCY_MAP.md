# Dependency map for the red-team branch

Generated from `redteam/tools/trace_imports.py` (static AST walk that also
records lazy, in-function imports) plus a runtime `sys.modules` dump per entry
point. Branch point: `codex/collaborative-harness` at `1221154`, the newest
branch that contains `a265726` (the JUnit-XML regression-gate fix).

Entry points: `demo/run_pipeline.py`, `demo/run_pipeline_diff.py`,
`miner/runtime.py`, `validator/merge.py`, `validator/diff_merge.py`,
`validator/scorer.py`, `validator/holdback.py`, `validator/sandbox.py`.

## The rule used to classify

The grader is the object under study, and the plan forbids changing it before
Phase 5. So every module the grader imports, eagerly or lazily, is KEEP, even
when the experiment disables that code path through configuration. Cutting a
lazily imported grader dependency would be a grader change by another name.
The one exception is a code path that is non-default and unreachable under the
experiment configuration; those are listed explicitly below.

## Corrections to the plan's expected lists

| Plan said | Actually | Why |
| --- | --- | --- |
| CUT `validator/repair*.py` | KEEP | `validator/merge.py` runs `_select_repair_backend()` at import time, which imports `validator/repair.py` (and through it `anthropic`). `diff_merge.py` lazily imports `repair_cc.py`. The experiment sets `BITSWARM_REPAIR_MODE=off` instead. |
| CUT `protocol/transport.py` | KEEP | `miner/runtime.py` imports `unbundle_repo`; the frozen tasks ship as git bundles. |
| CUT `miner/agent.py` | KEEP | Every backend imports `MinerResult` and `_generate_patch` from it. |
| CUT `validator/preflight.py` | KEEP | `demo/run_pipeline.py` (an entry point) calls it unconditionally. |
| (not mentioned) `harness/` | partly KEEP | `diff_merge.collect_failing_nodeids` uses `harness.verify.parse_report` (the `a265726` fix), `validator/patch_scope.py` and `diff_merge.py` use `harness.workspace.normalize_diff_prefixes`, and `harness/verify.py` mounts `harness/gate_worker.py` into its container. The rest of `harness/` is the post-pivot product (implementer/reviewer runtime, GitHub Action) and is CUT. |
| UNSURE coordinator | KEEP | Needed to regenerate the frozen tasks reproducibly. `decomposer.py` lazily imports `cache.py`, `critique.py`, `diff_validator.py`, `diff_prompts.py`, `decomposer_cc.py`. |

## KEEP

| Module | Reason |
| --- | --- |
| `config.py` | Backend and model selection for every agent path. |
| `protocol/__init__.py`, `protocol/schemas.py` | `PROTOCOL_VERSION`, `TaskAssignment`, `MinerResponse`. |
| `protocol/transport.py` | git bundle create/unbundle (frozen tasks, `miner/runtime.py`). |
| `miner/agent.py` | `MinerResult`, `_generate_patch`; Anthropic SDK loop. |
| `miner/agent_openai.py` | Open-model backend (OpenAI-compatible endpoint). |
| `miner/agent_cc.py` | Claude Code CLI backend. |
| `miner/prompts.py` | `MINER_SYSTEM_PROMPT`, the base prompt for every condition. |
| `miner/tools.py` | Tool schemas, `allowed_files` enforcement, bash denylist, sandboxed bash. |
| `miner/recovery.py` | Retry and thrashing logic inside the agent loop. |
| `miner/warm_start.py` | Assignment message (annotated tree, pre-loaded files). |
| `miner/runtime.py` | Entry point; transport-agnostic `run_assignment`. |
| `validator/merge.py` | Scaffold-mode tiered merge and scoring (grader). |
| `validator/diff_merge.py` | Diff-mode merge, additive and regression gates (grader). |
| `validator/scorer.py` | `compute_scores` (grader). |
| `validator/holdback.py` | Held-back test split, commit, reveal (grader). |
| `validator/sandbox.py` | Docker `--network=none` wrapper for gates and agent bash. |
| `validator/test_runner.py` | Scaffold-mode stub and integration test execution and counting (grader). |
| `validator/test_runners.py` | Build-system dispatch used by diff gates and miner replay. |
| `validator/patch_scope.py` | Patch scope check used by both merges (grader). |
| `validator/repair.py`, `validator/repair_cc.py` | Imported by the grader; disabled by config in the experiment. |
| `validator/scaffolder.py` | Writes scaffolding, calls `apply_holdback`. |
| `validator/lang_profiles.py` | Imported by `agent_cc`, coordinator, and repair. |
| `validator/validator_checks.py`, `validator/validator_checks_common.py` | Decomposition validation. |
| `validator/parsers/__init__.py`, `python.py`, `types.py` | Python parser for validation. The registry loads other parsers with `_safe_load`, so removing them needs no code change. |
| `validator/decomposer.py`, `decomposer_cc.py`, `prompts.py`, `diff_prompts.py`, `diff_validator.py`, `cache.py`, `critique.py` | Coordinator, run once to freeze tasks. |
| `validator/preflight.py` | Called by `demo/run_pipeline.py`. |
| `harness/__init__.py`, `verify.py`, `workspace.py`, `gate_worker.py` | Regression-gate report parser and patch normalization used by the grader. |
| `demo/run_pipeline.py`, `demo/run_pipeline_diff.py` | Entry points. |
| `demo/spec_wordle.txt`, `demo/spec_minidb.txt`, `demo/target_repo/` | Task inputs. |
| `docker/Dockerfile.base` | Sandbox image. Trimmed to the Python toolchain. |

## CUT

| Module | Reason |
| --- | --- |
| `neurons/` | Bittensor axon/dendrite layer; no local run uses it. |
| `protocol/synapses.py` | Bittensor synapses; imported only by `neurons/`. |
| `validator/weights.py` | On-chain weight submission; imported only by `neurons/`. |
| `validator/api.py`, `validator/inbox.py`, `validator/server.py` | HTTP submission API and orchestration server. |
| `miner/server.py` | HTTP miner transport. Only other importer is `drop_replace.py`. |
| `validator/drop_replace.py` | Reached only when `BITSWARM_REPAIR_MODE=replace`, which is non-default and never used here. With it gone, `replace` mode raises ImportError instead of running; `patch` and `off` are unaffected. |
| `validator/parsers/{typescript,java,csharp,c,cpp,rust}.py` | Non-Python languages. |
| `harness/{__main__,action,demo,evaluate,models,report,runtime,store}.py` | Post-pivot harness product and GitHub Action. Not imported by the grader. |
| `action.yml`, `acceptance/`, `benchmarks/`, `docs/evidence/`, `.github/workflows/` | Action, harness evaluation scripts, and their evidence site. |
| `demo/run_all_languages.py`, `demo/smoke_*.py`, `demo/spec.txt`, `demo/spec_wordle_cpp.txt`, `demo/spec_wordle_generic.txt` | Multi-language demo, CLI smoke scripts, toy and non-Python specs. |
| `docker/Dockerfile.{harness,self,miner,validator}`, `docker/docker-compose.yml` | Images for the cut servers and the harness product. |
| `docs/ACTION.md`, `docs/HARNESS.md`, `docs/HARNESS_REVIEW.md`, `docs/VALIDATION.md` | Document cut code. `docs/CASE_STUDY.md` and `docs/legacy/` stay as background. |
| `bittensor`, `fastapi`, `uvicorn`, `httpx`, `flask`, `flask-sqlalchemy`, `gitpython`, `tree-sitter*` requirements | No remaining importer (flask and gitpython had none even before the cut). |

## Tests

| Test file | Decision |
| --- | --- |
| `test_chain_layer.py` | Holdback and `run_assignment` tests move to `test_holdback.py` and `test_runtime.py`; synapse, ScoreBook, and neuron tests are cut. |
| `test_bug_fixes.py` | Drop the Rust, TypeScript, C# parser cases and the `miner.server` case; keep the rest. |
| `test_harness.py` | Tests of `harness.verify` and `harness.workspace` move to `test_harness_verify.py`; the rest cover cut modules. |
| `test_api_inbox.py`, `test_dispatch.py`, `test_drop_replace.py`, `test_action.py`, `test_harness_docker.py`, `test_validator_multilang.py`, `test_parsers_{c,cpp,csharp,java,rust,typescript}.py` | Cover only cut modules. |
| All others | Cover KEEP modules; unchanged. |

## Raw tracer output

`python redteam/tools/trace_imports.py --json <file>` regenerates the graph.
Runtime eager closures at the branch point:

```
demo.run_pipeline:      config miner.{agent,agent_cc,prompts,recovery,tools,warm_start}
                        validator.{decomposer,merge,parsers,parsers.python,parsers.types,
                        prompts,repair_cc,scaffolder,scorer,test_runner,test_runners,
                        validator_checks,validator_checks_common}
demo.run_pipeline_diff: validator.{diff_merge,sandbox,test_runners}
miner.runtime:          config protocol.{schemas,transport}
validator.merge:        config miner.tools validator.{repair,scorer,test_runner}
validator.diff_merge:   validator.{sandbox,test_runners}
validator.scorer / holdback / sandbox: no repo imports
```
