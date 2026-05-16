"""
Run the BitSwarm Wordle pipeline across every supported language.

For each language in the profile registry (python, typescript, java,
csharp, c, cpp, rust), this script:

  1. Spawns ``python demo/run_pipeline.py`` in a subprocess with:
       COORDINATOR_LANGUAGE=<lang>
       MINER_LANGUAGE=<lang>
       COORDINATOR_BACKEND=claude_code
       MINER_BACKEND=claude_code
  2. Pipes its stdout+stderr into ``<out>/<lang>/run.log``.
  3. Parses the final "TOTAL  N.NNN / 1.000" line for the score.
  4. Continues to the next language regardless of failures (one bad
     language doesn't kill the batch).
  5. Prints a summary table at the end.

The same generic Wordle spec is used for every language; per-language
idioms are driven by the language profile registry inside the
coordinator.

Usage:
    python demo/run_all_languages.py --out out/all_langs
    python demo/run_all_languages.py --out out/all_langs --languages python,cpp
    python demo/run_all_languages.py --out out/all_langs --parallel 2

Notes:
  - Per-language toolchains must already be installed in the runtime:
      python:     python + pytest
      typescript: node + npm + vitest
      java:       JDK 17 + maven (or gradle)
      csharp:     .NET 8 SDK
      c:          gcc + make
      cpp:        g++ + make
      rust:       cargo
    Languages whose toolchain is missing will fail at the merge / cross-
    compile step; this script still records the result and moves on.
  - Each language run can take several minutes (Python Wordle ~3 min,
    C++ Wordle ~6 min on Max). Budget accordingly.
  - --parallel runs N language pipelines concurrently. Be careful:
    each pipeline spawns ~5 claude subprocesses internally, so
    parallel=2 already means 10 claude processes peak.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


DEFAULT_LANGUAGES = ("python", "typescript", "java", "csharp", "c", "cpp", "rust")
DEFAULT_SPEC = os.path.join(HERE, "spec_wordle_generic.txt")
DEFAULT_TARGET_REPO = os.path.join(HERE, "target_repo")

# Pattern for the pipeline's final score line:
#   "  TOTAL                    1.000 / 1.000"
_SCORE_RE = re.compile(
    r"^\s*TOTAL\s+(\d+\.\d+)\s*/\s*(\d+\.\d+)\s*$",
    re.MULTILINE,
)

# Pattern for the integration-test line:
#   "  integration_tests        PASS (100%)"
_INTEG_RE = re.compile(
    r"^\s*integration_tests\s+(PASS|FAIL)(?:\s+\((\d+)%\))?\s*$",
    re.MULTILINE,
)


@dataclasses.dataclass
class LangResult:
    language: str
    returncode: int
    elapsed_seconds: float
    score: float | None
    integration_passed: bool | None
    integration_pct: int | None
    log_path: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.score is not None


def _parse_score(log_text: str) -> tuple[float | None, bool | None, int | None]:
    score = None
    integ_ok = None
    integ_pct = None
    m = _SCORE_RE.search(log_text)
    if m:
        try:
            score = float(m.group(1))
        except ValueError:
            score = None
    m2 = _INTEG_RE.search(log_text)
    if m2:
        integ_ok = m2.group(1) == "PASS"
        if m2.group(2):
            try:
                integ_pct = int(m2.group(2))
            except ValueError:
                integ_pct = None
    return score, integ_ok, integ_pct


async def _run_one(language: str, spec: str, target_repo: str,
                    out_root: str, env_overrides: dict[str, str] | None = None) -> LangResult:
    """Run the pipeline for a single language. Returns a LangResult
    even on failure; never raises."""
    lang_out = os.path.join(out_root, language)
    if os.path.exists(lang_out):
        shutil.rmtree(lang_out)
    os.makedirs(lang_out, exist_ok=True)
    log_path = os.path.join(lang_out, "run.log")

    env = dict(os.environ)
    env.update({
        "COORDINATOR_LANGUAGE": language,
        "MINER_LANGUAGE": language,
        "COORDINATOR_BACKEND": "claude_code",
        "MINER_BACKEND": "claude_code",
        # Make sure neither sub-pipeline reaches for an SDK fallback
        # if the user has one configured for other work.
        "PYTHONUNBUFFERED": "1",
    })
    if env_overrides:
        env.update(env_overrides)

    pipeline_out = os.path.join(lang_out, "pipeline")
    cmd = [
        sys.executable, "-u", os.path.join(HERE, "run_pipeline.py"),
        "--spec", spec,
        "--target", target_repo,
        "--out", pipeline_out,
    ]

    print(f"\n[all_langs] === {language} === starting")
    print(f"[all_langs] {language}: log -> {log_path}")
    started = time.perf_counter()
    error_msg = ""
    rc = -1

    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            logf.write(f"# command: {' '.join(cmd)}\n")
            logf.write(f"# language: {language}\n")
            logf.write(f"# backend: claude_code (both coordinator and miner)\n\n")
            logf.flush()

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=ROOT,
            )

            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                logf.write(text)
                logf.flush()
                # Mirror to console with a prefix so parallel runs
                # are legible.
                sys.stdout.write(f"[{language[:4]:>4s}] {text}")
                sys.stdout.flush()

            rc = await proc.wait()
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        rc = -2

    elapsed = time.perf_counter() - started

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
    except OSError:
        log_text = ""
    score, integ_ok, integ_pct = _parse_score(log_text)

    res = LangResult(
        language=language,
        returncode=rc,
        elapsed_seconds=elapsed,
        score=score,
        integration_passed=integ_ok,
        integration_pct=integ_pct,
        log_path=log_path,
        error=error_msg,
    )
    status = "OK" if res.ok else f"FAIL(rc={rc})"
    print(f"[all_langs] === {language} === done {status} "
          f"in {elapsed:.1f}s score={score}")
    return res


def _print_summary(results: list[LangResult], out_root: str) -> None:
    print("\n" + "=" * 78)
    print("BitSwarm multi-language Wordle results")
    print("=" * 78)
    header = f"{'language':<12} {'status':<10} {'score':>7} {'integ':>10} {'time':>8}  log"
    print(header)
    print("-" * len(header))
    for r in results:
        if r.ok:
            status = "OK"
        elif r.returncode == -2:
            status = "ERROR"
        else:
            status = f"rc={r.returncode}"
        score_str = f"{r.score:.3f}" if r.score is not None else "  -  "
        if r.integration_passed is None:
            integ_str = "   -  "
        else:
            tag = "PASS" if r.integration_passed else "FAIL"
            if r.integration_pct is not None:
                integ_str = f"{tag} {r.integration_pct:>3d}%"
            else:
                integ_str = f"{tag}     "
        time_str = f"{r.elapsed_seconds:>5.0f}s"
        rel_log = os.path.relpath(r.log_path, ROOT)
        print(f"{r.language:<12} {status:<10} {score_str:>7} {integ_str:>10} {time_str:>8}  {rel_log}")

    print("-" * len(header))
    passed = sum(1 for r in results if r.ok)
    print(f"{passed}/{len(results)} languages produced a final score.")
    print(f"summary saved to {os.path.join(out_root, 'summary.txt')}")

    # Persist the summary as a plain text file too.
    summary_path = os.path.join(out_root, "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(header + "\n")
        f.write("-" * len(header) + "\n")
        for r in results:
            status = "OK" if r.ok else f"FAIL(rc={r.returncode})"
            score_str = f"{r.score:.3f}" if r.score is not None else "-"
            integ = "-" if r.integration_passed is None else (
                f"{'PASS' if r.integration_passed else 'FAIL'}"
                + (f" {r.integration_pct}%" if r.integration_pct is not None else "")
            )
            f.write(f"{r.language:<12} {status:<14} score={score_str:<6} "
                    f"integ={integ:<10} time={r.elapsed_seconds:.1f}s\n")
            if r.error:
                f.write(f"             error: {r.error}\n")


async def _run_all(languages: list[str], spec: str, target_repo: str,
                    out_root: str, parallel: int,
                    env_overrides: dict[str, str] | None) -> list[LangResult]:
    if parallel <= 1:
        results: list[LangResult] = []
        for lang in languages:
            res = await _run_one(lang, spec, target_repo, out_root, env_overrides)
            results.append(res)
        return results

    sem = asyncio.Semaphore(parallel)

    async def _bounded(lang: str) -> LangResult:
        async with sem:
            return await _run_one(lang, spec, target_repo, out_root, env_overrides)

    return list(await asyncio.gather(*(_bounded(l) for l in languages)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out", required=True,
        help="directory to drop per-language pipeline output + summary.txt",
    )
    parser.add_argument(
        "--spec", default=DEFAULT_SPEC,
        help=f"path to the spec file (default: {os.path.relpath(DEFAULT_SPEC, ROOT)})",
    )
    parser.add_argument(
        "--target", default=DEFAULT_TARGET_REPO,
        help="starter repo passed through to run_pipeline.py",
    )
    parser.add_argument(
        "--languages", default=",".join(DEFAULT_LANGUAGES),
        help=("comma-separated subset to run "
              f"(default: {','.join(DEFAULT_LANGUAGES)})"),
    )
    parser.add_argument(
        "--parallel", type=int, default=1,
        help=("max number of language pipelines to run concurrently. "
              "Each pipeline spawns ~5 claude subprocesses internally, "
              "so keep this small (1-3) on a laptop."),
    )
    parser.add_argument(
        "--repair-mode", default="patch",
        choices=("patch", "replace", "off"),
        help="BITSWARM_REPAIR_MODE passed to each run (default: patch)",
    )
    parser.add_argument(
        "--miner-timeout", type=int, default=1200,
        help="MINER_TIMEOUT_SECONDS for each subtask (default: 1200)",
    )
    args = parser.parse_args()

    languages = [l.strip().lower() for l in args.languages.split(",") if l.strip()]
    if not languages:
        print("error: --languages produced an empty list", file=sys.stderr)
        return 2

    unknown = [l for l in languages if l not in DEFAULT_LANGUAGES]
    if unknown:
        print(f"error: unknown languages: {unknown}", file=sys.stderr)
        print(f"       supported: {', '.join(DEFAULT_LANGUAGES)}", file=sys.stderr)
        return 2

    if not os.path.isfile(args.spec):
        print(f"error: spec not found: {args.spec}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.target):
        print(f"error: target repo not found: {args.target}", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)

    env_overrides = {
        "BITSWARM_REPAIR_MODE": args.repair_mode,
        "MINER_TIMEOUT_SECONDS": str(args.miner_timeout),
    }

    print(f"[all_langs] spec:        {args.spec}")
    print(f"[all_langs] target_repo: {args.target}")
    print(f"[all_langs] out_root:    {args.out}")
    print(f"[all_langs] languages:   {', '.join(languages)}")
    print(f"[all_langs] parallel:    {args.parallel}")
    print(f"[all_langs] repair_mode: {args.repair_mode}")

    overall_start = time.perf_counter()
    results = asyncio.run(_run_all(
        languages, args.spec, args.target, args.out,
        args.parallel, env_overrides,
    ))
    overall_elapsed = time.perf_counter() - overall_start
    print(f"\n[all_langs] all done in {overall_elapsed:.1f}s")

    _print_summary(results, args.out)
    failed = sum(1 for r in results if not r.ok)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
