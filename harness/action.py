"""Verify a pull request's patch against held-out acceptance tests, no model involved.

This is the GitHub Action entry point, and it works the same way locally:

    python -m harness.action --repo . --base <commit> --head <commit> \
        --contract-path acceptance --out out/verify

The acceptance directory is read from a git ref (the base branch by default),
never from the working tree, so the pull request cannot edit its own contract.
The patch is the diff between base and head. Paths it touches must match the
allow patterns and must not be tests or configuration. The verifier then runs
baseline, regression, and acceptance gates in a network-less container and
records hashes so the verdict can be replayed later.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from harness.report import report
from harness.runtime import Runtime, create_run, write_json
from harness.workspace import PolicyError, apply_patch, digest, safe_path, tree_hash


def sh(args: list[str], cwd: Path, check: bool = True, text: bool = True):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=text, check=check, timeout=300
    )


def ensure_commit(repo: Path, ref: str) -> str:
    """Resolve a ref to a commit id, fetching it from origin when absent.

    Shallow checkouts on CI runners usually hold only the head commit.
    """
    if not ref:
        raise ValueError("a base commit is required (set the action's base input)")
    probe = sh(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo, check=False
    )
    if probe.returncode == 0:
        return probe.stdout.strip()
    fetched = sh(["git", "fetch", "--no-tags", "--depth=1", "origin", ref], repo, check=False)
    if fetched.returncode:
        raise ValueError(f"cannot resolve {ref!r}: {fetched.stderr.strip()[:300]}")
    return sh(["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"], repo).stdout.strip()


def changed_paths(repo: Path, base: str, head: str) -> list[str]:
    raw = sh(["git", "diff", "--name-only", "--no-renames", "-z", base, head], repo).stdout
    return [p for p in raw.split("\0") if p]


def scope(changed: list[str], patterns: list[str]) -> tuple[list[str], list[str]]:
    """Split changed paths into allowed and outside. ``*`` matches across ``/``."""
    allowed = [p for p in changed if any(fnmatch.fnmatchcase(p, pat) for pat in patterns)]
    outside = [p for p in changed if p not in allowed]
    return allowed, outside


def export_contract(repo: Path, ref: str, rel: str, destination: Path) -> Path:
    """Copy the acceptance directory out of a commit, never out of the working tree."""
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref, "--", rel],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=300,
    )
    if archive.returncode:
        raise ValueError(
            f"no acceptance directory {rel!r} at {ref[:12]}: "
            f"{archive.stderr.decode(errors='replace').strip()[:200]}"
        )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        for member in tar.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                raise PolicyError(f"contract contains a non-regular entry: {member.name}")
            target = safe_path(destination, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())
    contract = destination / rel
    if not contract.is_dir():
        raise ValueError(f"{rel!r} at {ref[:12]} is not a directory")
    return contract


def verify_pull_request(
    repo: Path,
    base: str,
    head: str,
    contract_path: str,
    contract_ref: str | None,
    allow: list[str],
    out: Path,
    config: dict,
) -> dict:
    repo = repo.resolve()
    base = ensure_commit(repo, base)
    head = ensure_commit(repo, head)
    contract_ref = ensure_commit(repo, contract_ref or base)
    changed = changed_paths(repo, base, head)
    result = {
        "base": base,
        "head": head,
        "contract_ref": contract_ref,
        "contract_path": contract_path,
        "allow": allow,
        "changed_paths": changed,
        "run": str(out.resolve()),
    }
    if not changed:
        return {**result, "outcome": "INCONCLUSIVE", "reason": "no changes between base and head"}
    allowed, outside = scope(changed, allow)
    if outside:
        return {
            **result,
            "outcome": "FAIL",
            "reason": "changed paths outside the allow list",
            "unauthorized_paths": outside,
        }
    patch = sh(["git", "diff", "--binary", "--no-renames", base, head], repo).stdout
    result["patch_hash"] = digest(patch)
    with tempfile.TemporaryDirectory(prefix="bitswarm-contract-") as tmp:
        try:
            contract = export_contract(repo, contract_ref, contract_path, Path(tmp))
            store = create_run(
                out,
                repo,
                contract,
                f"Verify {head[:12]} against {base[:12]}",
                allowed,
                config,
                base,
            )
        except (ValueError, PolicyError) as exc:
            return {**result, "outcome": "INCONCLUSIVE", "reason": str(exc)[:500]}
    state = store.load()
    candidate = store.directory / "candidate"
    try:
        apply_patch(candidate, patch, allowed)
    except PolicyError as exc:
        # A patch that touches protected paths or does not apply is the verdict.
        verdict = {**result, "outcome": "FAIL", "reason": str(exc)[:500]}
        state["phase"] = "failed"
        state["result"] = verdict
        store.save(state, "patch.rejected", {"reason": verdict["reason"]})
        (store.directory / "patch.diff").write_text(patch)
        return verdict
    state.update(phase="verifying", candidate_hash=tree_hash(candidate))
    store.save(state, "patch.supplied", {"patch_hash": result["patch_hash"]})
    state = Runtime(out).run()
    report(out)
    verdict = state.get("result", {})
    return {**result, **verdict, "phase": state["phase"]}


def _count(tests: dict) -> str:
    counts = {}
    for status in tests.values():
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"


def summary_markdown(result: dict) -> str:
    outcome = result.get("outcome", "INCONCLUSIVE")
    icon = {"PASS": "white_check_mark", "FAIL": "x"}.get(outcome, "warning")
    lines = [f"## BitSwarm verify: :{icon}: {outcome}", ""]
    if result.get("reason"):
        lines += [f"**Reason:** {result['reason']}", ""]
    lines += [
        "| | |",
        "|---|---|",
        f"| Base | `{result.get('base', '')[:12]}` |",
        f"| Head | `{result.get('head', '')[:12]}` |",
        f"| Contract | `{result.get('contract_path', '')}` at `{result.get('contract_ref', '')[:12]}` |",
        f"| Patch sha256 | `{result.get('patch_hash', 'n/a')}` |",
        f"| Sandbox | `{result.get('environment', {}).get('mode', result.get('mode', 'n/a'))}` |",
        f"| Image | `{result.get('environment', {}).get('image', 'n/a')}` |",
        "",
    ]
    if result.get("unauthorized_paths"):
        lines += ["**Outside allow list:**", ""]
        lines += [f"- `{p}`" for p in result["unauthorized_paths"]]
        lines.append("")
    gates = result.get("gates")
    if gates:
        lines += ["| Gate | Outcome | Tests | Reason |", "|---|---|---|---|"]
        for name in ("baseline", "regression", "acceptance"):
            gate = gates.get(name, {})
            lines.append(
                f"| {name} | {gate.get('outcome', '')} | {_count(gate.get('tests', {}))} "
                f"| {gate.get('reason', '')} |"
            )
        lines.append("")
        failing = sorted(
            k for k, v in gates.get("acceptance", {}).get("tests", {}).items() if v != "passed"
        )
        if failing:
            lines += ["**Acceptance tests not passing:**", ""]
            lines += [f"- `{k}`" for k in failing[:20]]
            if len(failing) > 20:
                lines.append(f"- and {len(failing) - 20} more")
            lines.append("")
        lost = result.get("lost_passing_tests") or []
        if lost:
            lines += ["**Previously passing tests that no longer pass:**", ""]
            lines += [f"- `{k}`" for k in lost[:20]]
            lines.append("")
    lines += [
        "Changed paths: " + ", ".join(f"`{p}`" for p in result.get("changed_paths", [])),
        "",
        (
            "Replay from the uploaded run directory with `bitswarm replay <dir>`; "
            "the result records whether the verifier and image still match."
        ),
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--contract-path", default="acceptance")
    parser.add_argument("--contract-ref", default="")
    parser.add_argument("--allow", default="*", help="whitespace-separated fnmatch patterns")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sandbox", choices=["docker", "unsafe-local"], default="docker")
    parser.add_argument("--image", default="bitswarm-harness:local")
    parser.add_argument("--gate-timeout", type=int, default=300)
    parser.add_argument("--max-repo-files", type=int, default=3000)
    parser.add_argument("--max-repo-mb", type=int, default=30)
    parser.add_argument("--fail-on-inconclusive", default="true")
    args = parser.parse_args(argv)
    config = {
        "sandbox": args.sandbox,
        "image": args.image,
        "gate_timeout": args.gate_timeout,
        "max_repo_files": args.max_repo_files,
        "max_repo_mb": args.max_repo_mb,
    }
    result = verify_pull_request(
        args.repo,
        args.base,
        args.head,
        args.contract_path,
        args.contract_ref or None,
        args.allow.split(),
        args.out,
        config,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    write_json(args.out / "action.json", result)
    summary = summary_markdown(result)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"outcome={result.get('outcome', 'INCONCLUSIVE')}\n")
            f.write(f"patch-hash={result.get('patch_hash', '')}\n")
            f.write(f"run={result.get('run', '')}\n")
    outcome = result.get("outcome", "INCONCLUSIVE")
    if outcome == "PASS":
        return 0
    if outcome == "INCONCLUSIVE" and args.fail_on_inconclusive.lower() != "true":
        return 0
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"BitSwarm: {exc}", file=sys.stderr)
        sys.exit(2)
