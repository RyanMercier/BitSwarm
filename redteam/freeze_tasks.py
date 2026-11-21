"""Generate and freeze the red-team task set.

The coordinator's output varies run to run, so each task is generated
once and frozen. Nothing in the study regenerates a task.

Subcommands
-----------
generate   Run the coordinator once on a spec and save the raw
           decomposition plus provenance under tasks/<id>/source/.
reference  Extract one reference patch per subtask from a repo that
           holds a correct, fully merged implementation.
freeze     Turn a raw decomposition into the frozen task directory:
             tasks/<id>/task.json            metadata and provenance
             tasks/<id>/decomposition.json   what the validator holds
             tasks/<id>/visible_tests/       what the miner sees
             tasks/<id>/holdback_tests/      what the grader adds
             tasks/<id>/baseline_repo.bundle scaffolded starting repo

Gate-file split
---------------
``validator.holdback.select_holdback`` works at FILE granularity and
never holds anything back when a task has fewer than two gate files.
Every decomposition in this study has exactly one gate file
(integration tests in scaffold mode, new tests in diff mode), so the
stock holdback would hold back nothing. ``freeze`` therefore splits
each gate file into one file per test (shared imports, fixtures, and
helpers copied into each), then calls the unchanged
``write_scaffolding``, which applies the unchanged holdback with
BITSWARM_HOLDBACK_FRACTION=0.3 and seed = task id. The split is a
task-preparation step; the grader code is untouched.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TASKS_DIR = os.path.join(ROOT, "redteam", "tasks")
HOLDBACK_FRACTION = "0.3"

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
    # Fixed dates keep commit hashes stable across re-freezes.
    "GIT_AUTHOR_DATE": "2026-11-08T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-11-08T00:00:00Z",
}

_IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", ".pytest_cache", ".venv", "venv",
    "node_modules", "dist", "build", ".tox", ".mypy_cache",
)


def git(repo: str, *args: str, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=repo, env=GIT_ENV,
                          capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}:\n{res.stderr}")
    return res.stdout


def init_repo(source_dir: str, dest: str) -> str:
    """Copy a source tree into ``dest`` and commit it as the initial state."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(source_dir, dest, ignore=_IGNORE)
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "initial target repo state")
    return dest


def export_commit(repo: str, commit: str, dest: str) -> str:
    """Materialize ``commit`` of ``repo`` as a plain directory."""
    os.makedirs(dest, exist_ok=True)
    archive = subprocess.run(["git", "archive", commit], cwd=repo,
                              capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, check=True)
    return dest


# --------------------------------------------------------------- generate

def cmd_generate(args: argparse.Namespace) -> int:
    os.environ.setdefault("COORDINATOR_BACKEND", "claude_code")
    os.environ["BITSWARM_NO_CACHE"] = "1"
    src_dir = args.out or os.path.join(TASKS_DIR, args.task, "source")
    os.makedirs(src_dir, exist_ok=True)
    spec = open(args.spec).read()
    shutil.copy(args.spec, os.path.join(src_dir, "spec.txt"))

    work = tempfile.mkdtemp(prefix=f"freeze_{args.task}_")
    repo = init_repo(args.target, os.path.join(work, "repo"))

    from validator.decomposer import decompose
    validate_fn = None
    if args.mode == "scaffold" and not args.no_validate:
        from validator.validator_checks import validate_decomposition
        validate_fn = validate_decomposition
        if args.skip_arity:
            # The stock arity check resolves calls by bare name, so a
            # method call like engine.execute(sql) is checked against an
            # unrelated module function minidb.executor.execute(stmt,
            # store, evaluator). It rejected three correct attempts.
            def validate_fn(decomp, repo, _v=validate_decomposition):
                return [e for e in _v(decomp, repo) if not e.startswith("Arity mismatch")]

    started = dt.datetime.now(dt.timezone.utc)
    decomp = decompose(repo_path=repo, feature_spec=spec, validate_fn=validate_fn,
                       debug_dir=os.path.join(src_dir, "coordinator_debug"),
                       mode=args.mode)
    if decomp is None:
        print("coordinator returned None after all retries", file=sys.stderr)
        return 1
    with open(os.path.join(src_dir, "decomposition_raw.json"), "w") as f:
        json.dump(decomp, f, indent=2)
    provenance = {
        "generated_by": "redteam/freeze_tasks.py generate",
        "started_utc": started.isoformat(),
        "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": args.mode,
        "coordinator_backend": os.environ.get("COORDINATOR_BACKEND"),
        "coordinator_model": os.environ.get("CC_COORDINATOR_MODEL")
                              or os.environ.get("COORDINATOR_MODEL") or "default",
        "spec": os.path.relpath(args.spec, ROOT),
        "target": os.path.relpath(args.target, ROOT) if args.target.startswith(ROOT) else args.target,
        "bitswarm_commit": git(ROOT, "rev-parse", "HEAD").strip(),
        "validation": ("none (--no-validate)" if args.no_validate else
                       "validate_decomposition minus 'Arity mismatch' errors"
                       if getattr(args, "skip_arity", False) else
                       ("validate_decomposition" if args.mode == "scaffold"
                        else "validate_diff_decomposition")),
    }
    with open(os.path.join(src_dir, "provenance.json"), "w") as f:
        json.dump(provenance, f, indent=2)
    print(f"saved {len(decomp['subtasks'])} subtasks to {src_dir}")
    return 0


# -------------------------------------------------------------- reference

def cmd_reference(args: argparse.Namespace) -> int:
    """One patch per subtask: diff of its implementation files from the
    baseline commit to HEAD of a merged, correct repo. Test-file edits a
    miner made are deliberately excluded; references must pass the
    canonical tests."""
    decomp = json.load(open(args.decomposition))
    base = None
    for line in git(args.repo, "log", "--all", "--format=%H %s").splitlines():
        if "BitSwarm scaffolding" in line or "BitSwarm diff baseline" in line:
            base = line.split()[0]
    if base is None:
        raise SystemExit("no BitSwarm baseline commit in reference repo")
    out = os.path.join(TASKS_DIR, args.task, "reference")
    os.makedirs(out, exist_ok=True)
    for st in decomp["subtasks"]:
        files = st.get("stub_files") or st.get("modify_files") or []
        patch = git(args.repo, "diff", base, "HEAD", "--", *files)
        with open(os.path.join(out, f"{st['subtask_id']}.patch"), "w") as f:
            f.write(patch)
        print(f"{st['subtask_id']}: {len(patch)} chars over {files}")
    return 0


# ----------------------------------------------------------------- freeze

def _node_source(lines: list[str], node: ast.AST) -> str:
    start = min([d.lineno for d in getattr(node, "decorator_list", [])]
                + [node.lineno]) - 1
    return "".join(lines[start:node.end_lineno])


def split_test_file(path: str, src: str) -> dict[str, str]:
    """Split one pytest file into one file per top-level test.

    Every non-test top-level statement (docstring, imports, fixtures,
    helpers, constants) is copied into each output file so each runs on
    its own. Files with fewer than two tests are returned unchanged.
    """
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    common: list[str] = []
    tests: list[tuple[str, str]] = []
    for node in tree.body:
        is_test = (
            (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name.startswith("test"))
            or (isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
        )
        seg = _node_source(lines, node)
        if is_test:
            tests.append((node.name, seg))
        else:
            common.append(seg)
    if len(tests) < 2:
        return {path: src}
    stem, ext = os.path.splitext(path)
    header = "\n\n".join(s.rstrip("\n") for s in common)
    out = {}
    for name, seg in tests:
        short = name[5:] if name.startswith("test_") else name
        out[f"{stem}__{short}{ext}"] = f"{header}\n\n\n{seg.rstrip()}\n"
    return out


def cmd_freeze(args: argparse.Namespace) -> int:
    task_dir = os.path.join(TASKS_DIR, args.task)
    decomp = json.load(open(args.decomposition))
    mode = decomp.get("mode", "scaffold")
    decomp["mode"] = mode
    decomp["task_id"] = args.task
    gate_key = "new_test_files" if mode == "diff" else "integration_test_files"

    # 1. Split gate files so the file-granularity holdback has material.
    split_map: dict[str, list[str]] = {}
    new_gate: dict[str, str] = {}
    for path, content in (decomp.get(gate_key) or {}).items():
        parts = split_test_file(path, content)
        split_map[path] = sorted(parts)
        new_gate.update(parts)
    decomp[gate_key] = new_gate
    if mode == "diff":
        for st in decomp["subtasks"]:
            st["new_test_files"] = [p for old in st.get("new_test_files") or []
                                    for p in split_map.get(old, [old])]

    # 2. Starting repo, then the UNCHANGED scaffolder (applies holdback).
    work = tempfile.mkdtemp(prefix=f"freeze_{args.task}_")
    if args.initial_commit:
        start = export_commit(args.initial_repo, args.initial_commit,
                              os.path.join(work, "start"))
    else:
        start = args.initial_repo
    repo = init_repo(start, os.path.join(work, "repo"))
    os.environ["BITSWARM_HOLDBACK_FRACTION"] = HOLDBACK_FRACTION
    from validator.scaffolder import write_scaffolding
    write_scaffolding(decomp, repo)
    held = sorted(decomp.get("holdback_tests") or {})

    # 3. Production passes subtask test lists through unfiltered, which
    #    would show miners the NAMES of held-back files. Filter them so
    #    the miner-facing manifest matches what is on disk.
    leaked = []
    if mode == "diff":
        for st in decomp["subtasks"]:
            before = list(st.get("new_test_files") or [])
            st["new_test_files"] = [p for p in before if p not in held]
            leaked += [p for p in before if p in held]

    # 4. Write the frozen task directory.
    for sub in ("visible_tests", "holdback_tests"):
        shutil.rmtree(os.path.join(task_dir, sub), ignore_errors=True)
    visible = dict(decomp.get(gate_key) or {})
    if mode == "scaffold":
        visible.update(decomp.get("stub_test_files") or {})
    for sub, files in (("visible_tests", visible),
                       ("holdback_tests", decomp.get("holdback_tests") or {})):
        for rel, content in files.items():
            full = os.path.join(task_dir, sub, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)

    with open(os.path.join(task_dir, "decomposition.json"), "w") as f:
        json.dump(decomp, f, indent=2)

    bundle = os.path.join(task_dir, "baseline_repo.bundle")
    if os.path.exists(bundle):
        os.remove(bundle)
    git(repo, "bundle", "create", bundle, "--all")
    head = git(repo, "rev-parse", "HEAD").strip()

    meta = {
        "task_id": args.task,
        "mode": mode,
        "frozen_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_decomposition": os.path.relpath(args.decomposition, ROOT)
                                 if os.path.abspath(args.decomposition).startswith(ROOT)
                                 else args.decomposition,
        "source_note": args.note,
        "baseline_head": head,
        "holdback_fraction": float(HOLDBACK_FRACTION),
        "holdback_seed": args.task,
        "holdback_commit": decomp.get("holdback_commit", ""),
        "gate_key": gate_key,
        "gate_split": split_map,
        "held_back": held,
        "held_back_names_listed_in_subtasks_before_filter": leaked,
        "subtasks": [
            {
                "subtask_id": st["subtask_id"],
                "complexity_weight": st.get("complexity_weight"),
                "dependencies": st.get("dependencies", []),
                "implementation_files": st.get("stub_files") or st.get("modify_files") or [],
                "test_files": st.get("stub_test_files") or st.get("new_test_files") or [],
            }
            for st in decomp["subtasks"]
        ],
    }
    with open(os.path.join(task_dir, "task.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps({k: meta[k] for k in ("task_id", "mode", "baseline_head",
                                           "held_back")}, indent=2))
    shutil.rmtree(work, ignore_errors=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate")
    g.add_argument("--task", required=True)
    g.add_argument("--spec", required=True)
    g.add_argument("--target", required=True, help="starting repo directory")
    g.add_argument("--mode", choices=("scaffold", "diff"), default="scaffold")
    g.add_argument("--out", default="",
                   help="output dir (default tasks/<task>/source)")
    g.add_argument("--skip-arity", action="store_true",
                   help="drop the validator's name-only arity errors")
    g.add_argument("--no-validate", action="store_true",
                   help="skip Phase 1.5 decomposition validation entirely")
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("reference")
    r.add_argument("--task", required=True)
    r.add_argument("--decomposition", required=True)
    r.add_argument("--repo", required=True, help="merged repo with a correct solution")
    r.set_defaults(fn=cmd_reference)

    fz = sub.add_parser("freeze")
    fz.add_argument("--task", required=True)
    fz.add_argument("--decomposition", required=True, help="raw coordinator output")
    fz.add_argument("--initial-repo", required=True)
    fz.add_argument("--initial-commit", default="",
                    help="export this commit of --initial-repo as the starting tree")
    fz.add_argument("--note", default="")
    fz.set_defaults(fn=cmd_freeze)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
