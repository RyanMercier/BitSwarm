"""
BitSwarm Orchestrator — main entry point.

Usage:
    python orchestrator.py                          # uses spec.txt, outputs to ./output/
    python orchestrator.py --spec my_spec.txt       # custom spec file
    python orchestrator.py --output /path/to/out    # custom output dir
    python orchestrator.py --target /path/to/repo   # custom target repo

The output directory will contain:
    output/
      scaffolded_repo/   -- repo after coordinator scaffolding (before miners)
      merged_repo/       -- final merged result (run this to test)
      decomposition.json -- coordinator's full decomposition
      run.log            -- console output saved to file
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

from coordinator.decomposer import decompose
from coordinator.validator_checks import validate_decomposition
from coordinator.scaffolder import write_scaffolding
from miner.agent import execute_subtask
from merger.merge import merge_and_test


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SPEC_FILE = os.path.join(HERE, "spec.txt")
DEFAULT_TARGET_REPO = os.path.join(HERE, "target_repo")
DEFAULT_OUTPUT_DIR = os.path.join(HERE, "output")


def setup_working_repo(source_path, output_dir):
    """Copy source repo to output/workspace/repo and git-init it."""
    # Nuke the entire workspace dir — not just repo — to guarantee a
    # clean git history with no stale commits from previous runs.
    workspace_dir = os.path.join(output_dir, "workspace")
    if os.path.exists(workspace_dir):
        shutil.rmtree(workspace_dir)
    repo_path = os.path.join(workspace_dir, "repo")
    shutil.copytree(source_path, repo_path)

    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "BitSwarm",
        "GIT_AUTHOR_EMAIL": "bitswarm@local",
        "GIT_COMMITTER_NAME": "BitSwarm",
        "GIT_COMMITTER_EMAIL": "bitswarm@local",
    }
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial target repo"],
        cwd=repo_path, capture_output=True, env=git_env,
    )

    return repo_path


def copy_to_output(src, dest):
    """Copy a directory tree to the output dir, overwriting if it exists."""
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def print_decomposition_summary(decomposition):
    subtasks = decomposition["subtasks"]
    shared = decomposition.get("shared_files", {})
    stubs = decomposition.get("stub_files", {})
    tests = decomposition.get("stub_test_files", {})
    integration = decomposition.get("integration_test_files", {})

    print(f"\n{'='*60}")
    print("DECOMPOSITION SUMMARY")
    print(f"{'='*60}")
    print(f"  Subtasks:         {len(subtasks)}")
    print(f"  Shared files:     {len(shared)}")
    print(f"  Stub files:       {len(stubs)}")
    print(f"  Test files:       {len(tests)}")
    print(f"  Integration tests:{len(integration)}")
    print(f"  New requirements: {decomposition.get('requirements_additions', [])}")

    for st in subtasks:
        print(f"\n  [{st['subtask_id']}]  weight={st['complexity_weight']}")
        print(f"    {st['description']}")
        print(f"    stubs: {st['stub_files']}")
        print(f"    tests: {st['stub_test_files']}")
        if st.get("dependencies"):
            print(f"    deps:  {st['dependencies']}")


async def run(spec, target_repo_path, output_dir):
    """Run the full BitSwarm pipeline."""

    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'='*60}")
    print("BITSWARM PROTOTYPE")
    print(f"{'='*60}")
    print(f"  Output dir: {output_dir}")

    # Step 1: Set up working repo
    print("\n[1/6] Copying target repo...")
    repo_path = setup_working_repo(target_repo_path, output_dir)
    print(f"  Working repo: {repo_path}")

    # Step 2: Install base requirements
    print("\n[2/6] Installing base requirements...")
    req_file = os.path.join(repo_path, "requirements.txt")
    if os.path.isfile(req_file):
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", req_file],
            capture_output=True,
        )

    # Step 3: Coordinator decomposition
    print("\n[3/6] Running coordinator decomposition...")
    decomposition = decompose(
        repo_path=repo_path,
        feature_spec=spec,
        validate_fn=validate_decomposition,
        debug_dir=os.path.join(output_dir, "debug"),
    )

    if decomposition is None:
        print("\nFAILED: Coordinator could not produce valid decomposition.")
        sys.exit(1)

    print_decomposition_summary(decomposition)

    # Save decomposition JSON to output dir
    decomp_path = os.path.join(output_dir, "decomposition.json")
    with open(decomp_path, "w") as f:
        json.dump(decomposition, f, indent=2)
    print(f"\n  Decomposition saved to: {decomp_path}")

    # Step 4: Write scaffolding and commit
    print("\n[4/6] Writing scaffolding...")
    write_scaffolding(decomposition, repo_path)

    # Save scaffolded repo snapshot for inspection
    scaffolded_snapshot = os.path.join(output_dir, "scaffolded_repo")
    copy_to_output(repo_path, scaffolded_snapshot)
    print(f"  Scaffolded repo saved to: {scaffolded_snapshot}")

    # Step 5: Run miners in parallel
    print("\n[5/6] Running miners in parallel...")
    subtasks = decomposition["subtasks"]
    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    test_files = decomposition.get("stub_test_files", {})

    # Build allowed_files per subtask: stub files + that miner's own test files.
    # Test files must be writable so the miner can adapt tests to work around
    # stub dependencies (e.g. mock cross-subtask objects when tests depend on them).
    for st in subtasks:
        stub_f = st.get("stub_files", [])
        test_f = st.get("stub_test_files", [])
        st["allowed_files"] = list(dict.fromkeys(stub_f + test_f))  # dedup, preserve order

    all_subtask_files = {st["subtask_id"]: st["allowed_files"] for st in subtasks}

    miner_repos_dir = os.path.join(output_dir, "miner_repos")
    os.makedirs(miner_repos_dir, exist_ok=True)

    miner_tasks = []
    for st in subtasks:
        miner_repo = os.path.join(miner_repos_dir, st["subtask_id"])
        if os.path.exists(miner_repo):
            shutil.rmtree(miner_repo)
        shutil.copytree(repo_path, miner_repo)

        task = execute_subtask(
            subtask=st,
            repo_path=miner_repo,
            all_subtask_files=all_subtask_files,
            shared_files=shared_files,
            shared_files_content=shared_files,
            stub_files_content=stub_files,
            test_files_content=test_files,
            all_subtasks=subtasks,
        )
        miner_tasks.append((st["subtask_id"], task))

    results = await asyncio.gather(
        *[task for _, task in miner_tasks],
        return_exceptions=True,
    )

    miner_results = {}
    print(f"\n{'='*60}")
    print("MINER RESULTS")
    print(f"{'='*60}")
    for i, (sid, _) in enumerate(miner_tasks):
        result = results[i]
        if isinstance(result, Exception):
            print(f"  [{sid}] ERROR: {result}")
            import traceback
            traceback.print_exception(type(result), result, result.__traceback__)
            continue
        miner_results[sid] = result
        status = "PASSED" if result.tests_passed else "FAILED"
        print(f"  [{sid}] {status}  (iterations: {result.iterations_used}, "
              f"stop: {result.stop_reason})")

    # Step 6: Merge and score
    print(f"\n[6/6] Merging and testing...")
    merge_result = await merge_and_test(decomposition, miner_results, repo_path)

    # Save merged repo to output dir for inspection
    merged_snapshot = os.path.join(output_dir, "merged_repo")
    copy_to_output(merge_result["merge_repo"], merged_snapshot)
    print(f"\n  Merged repo saved to: {merged_snapshot}")

    # Final summary
    print(f"\n{'='*60}")
    print("FINAL SCORES")
    print(f"{'='*60}")
    repairs = merge_result.get("repairs_made", {})
    for st in subtasks:
        sid = st["subtask_id"]
        score = merge_result["scores"].get(sid, 0.0)
        stub_pass = merge_result["stub_results"].get(sid, False)
        patch_ok = merge_result["patch_applied"].get(sid, False)
        repaired = repairs.get(sid)
        repair_tag = ""
        if repaired is True:
            repair_tag = "  [repaired]"
        elif repaired is False:
            repair_tag = "  [repair FAILED]"
        print(f"  [{sid}]  score={score:.3f}  "
              f"patch={'OK  ' if patch_ok else 'FAIL'}  "
              f"stubs={'PASS' if stub_pass else 'FAIL'}{repair_tag}")

    integration_ok = merge_result["integration_passed"]
    integration_ratio = merge_result.get("integration_ratio", 1.0 if integration_ok else 0.0)
    total = sum(merge_result["scores"].values())
    ratio_pct = int(integration_ratio * 100)
    int_repair = repairs.get("_integration")
    int_tag = ""
    if int_repair is True:
        int_tag = "  [repaired]"
    elif int_repair is False:
        int_tag = "  [repair FAILED]"
    print(f"\n  Integration tests: {'PASSED' if integration_ok else 'FAILED'} ({ratio_pct}% passed){int_tag}")
    print(f"  Total score:       {total:.3f} / 1.000")

    if integration_ok and total >= 0.99:
        print(f"\n  ✓ FULL SUCCESS")
    elif total > 0:
        print(f"\n  ~ PARTIAL SUCCESS ({total:.1%})")
    else:
        print(f"\n  ✗ FAILED")

    print(f"\n  To inspect the result:")
    print(f"    cd {merged_snapshot}")
    print(f"    pip install -r requirements.txt")
    print(f"    python app.py")

    return merge_result


def main():
    parser = argparse.ArgumentParser(description="BitSwarm Prototype")
    parser.add_argument(
        "--spec", default=DEFAULT_SPEC_FILE,
        help=f"Path to feature spec file (default: spec.txt)",
    )
    parser.add_argument(
        "--target", default=DEFAULT_TARGET_REPO,
        help=f"Path to target repo (default: ./target_repo)",
    )
    parser.add_argument(
        "--output", default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: ./output)",
    )
    args = parser.parse_args()

    # Load spec
    if not os.path.isfile(args.spec):
        print(f"Error: spec file not found: {args.spec}")
        print(f"Create a file called 'spec.txt' with your feature description.")
        sys.exit(1)

    with open(args.spec) as f:
        spec = f.read().strip()

    if not spec:
        print(f"Error: spec file is empty: {args.spec}")
        sys.exit(1)

    # Validate target repo
    if not os.path.isdir(args.target):
        print(f"Error: target repo not found: {args.target}")
        sys.exit(1)

    print(f"Spec: {args.spec}")
    print(f"Target: {args.target}")
    print(f"Output: {args.output}")

    asyncio.run(run(spec, args.target, args.output))


if __name__ == "__main__":
    main()
