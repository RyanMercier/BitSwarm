import argparse
import json
import sys
from pathlib import Path

from harness.report import report
from harness.runtime import Runtime, create_run, replay
from harness.store import Store


def main():
    parser = argparse.ArgumentParser(
        description="BitSwarm collaborative coding harness (Linux/WSL)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo", help="offline scripted implement/review/repair demonstration"
    )
    demo.add_argument("--out", type=Path, required=True)
    demo.add_argument("--sandbox", choices=["docker", "unsafe-local"], default="docker")
    demo.add_argument("--steps", type=int)
    run = commands.add_parser("run")
    run.add_argument("--repo", type=Path, required=True)
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--allow", nargs="+", required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--base", default="HEAD")
    run.add_argument("--steps", type=int)
    verify = commands.add_parser(
        "verify", help="verify a supplied patch without model calls"
    )
    verify.add_argument("--repo", type=Path, required=True)
    verify.add_argument("--contract", type=Path, required=True)
    verify.add_argument("--patch", type=Path, required=True)
    verify.add_argument("--allow", nargs="+", required=True)
    verify.add_argument("--out", type=Path, required=True)
    verify.add_argument("--base", default="HEAD")
    verify.add_argument(
        "--sandbox", choices=["docker", "unsafe-local"], default="docker"
    )
    evaluate = commands.add_parser(
        "evaluate", help="run explicit task/condition experiments"
    )
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--out", type=Path, required=True)
    for name in ("resume", "inspect", "replay", "report", "cancel"):
        p = commands.add_parser(name)
        p.add_argument("directory", type=Path)
        if name == "resume":
            p.add_argument("--steps", type=int)
    args = parser.parse_args()
    if args.command == "evaluate":
        from harness.evaluate import evaluate

        print(json.dumps(evaluate(args.manifest, args.out), indent=2))
        return 0
    if args.command == "demo":
        from harness.demo import prepare

        prepare(args.out, args.sandbox)
    elif args.command == "run":
        create_run(
            args.out,
            args.repo,
            args.contract,
            args.spec.read_text(),
            args.allow,
            json.loads(args.config.read_text()),
            args.base,
        )
    elif args.command == "verify":
        from harness.workspace import apply_patch, tree_hash

        store = create_run(
            args.out,
            args.repo,
            args.contract,
            "Verify supplied patch",
            args.allow,
            {"sandbox": args.sandbox},
            args.base,
        )
        state = store.load()
        apply_patch(store.directory / "candidate", args.patch.read_text(), args.allow)
        state.update(
            phase="verifying", candidate_hash=tree_hash(store.directory / "candidate")
        )
        store.save(state, "patch.supplied")
    if args.command in ("demo", "run", "resume", "verify"):
        directory = args.directory if args.command == "resume" else args.out
        state = Runtime(directory).run(getattr(args, "steps", None))
        output = {
            "phase": state["phase"],
            "report": str(report(directory)),
            "run": str(directory.resolve()),
        }
        print(json.dumps(output, indent=2))
        return 0 if state["phase"] == "passed" or getattr(args, "steps", None) else 2
    if args.command == "inspect":
        store = Store(args.directory)
        print(json.dumps({"state": store.load(), "events": store.events()}, indent=2))
    elif args.command == "replay":
        result = replay(args.directory)
        print(json.dumps(result, indent=2))
        return 0 if result["outcome"] == "PASS" else 2
    elif args.command == "report":
        print(report(args.directory))
    elif args.command == "cancel":
        (args.directory / "CANCEL").touch()
        print("Cancellation requested; runner will stop at its next action boundary.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"BitSwarm: {exc}", file=sys.stderr)
        sys.exit(2)
