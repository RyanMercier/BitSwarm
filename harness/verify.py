"""Independent, structured verification of a saved candidate patch."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.workspace import PolicyError, copy_tree, tree_hash


@dataclass
class Gate:
    outcome: str
    tests: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    output: str = ""
    resources: dict = field(default_factory=dict)


MUTATION_EXIT = 86
DEFAULT_PIDS_LIMIT = 64
DEFAULT_MEMORY_MB = 512
RESOURCES_MARKER = "\nBITSWARM_RESOURCES "


def parse_resources(full_output: str) -> dict:
    """Read the cgroup snapshot the container worker prints after pytest."""
    import json

    if full_output.count(RESOURCES_MARKER) != 1:
        return {}
    line = full_output.split(RESOURCES_MARKER, 1)[1].split("\n", 1)[0]
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def resource_limit_hit(resources: dict) -> str:
    """Describe any sandbox limit the run ran into, or return ''."""

    def count(events, key):
        try:
            return int((events or {}).get(key, 0))
        except (TypeError, ValueError):
            return 0

    reasons = []
    pids_hits = count(resources.get("pids.events"), "max")
    if pids_hits:
        reasons.append(
            f"process limit refused {pids_hits} fork or thread attempts "
            f"(peak {resources.get('pids.peak', '?')} of {resources.get('pids.max', '?')})"
        )
    for key in ("oom", "oom_kill"):
        hits = count(resources.get("memory.events"), key)
        if hits:
            reasons.append(
                f"memory {key} {hits} "
                f"(peak {resources.get('memory.peak', '?')} of {resources.get('memory.max', '?')})"
            )
    return "; ".join(reasons)


def with_resources(gate: Gate, resources: dict) -> Gate:
    """Attach the snapshot and refuse to blame code for an exhausted sandbox.

    A test that fails because the container ran out of processes or memory is
    evidence about the sandbox, not about the patch. A run that passed
    everything despite a refused fork keeps its PASS: every assertion ran.
    """
    gate.resources = resources
    hit = resource_limit_hit(resources)
    if hit and gate.outcome != "PASS":
        gate.outcome = "INCONCLUSIVE"
        gate.reason = f"sandbox resource limit reached: {hit}" + (
            f"; {gate.reason}" if gate.reason else ""
        )
    return gate


def parse_report(path: Path, returncode: int, output: str) -> Gate:
    if returncode == MUTATION_EXIT:
        return Gate(
            "INCONCLUSIVE",
            reason=f"test run mutated candidate source (runner exit {MUTATION_EXIT})",
            output=output,
        )
    if returncode not in (0, 1):
        return Gate("INCONCLUSIVE", reason=f"runner exit {returncode}", output=output)
    try:
        root = ET.parse(path).getroot()
        tests = {}
        for case in root.iter("testcase"):
            identity = f"{case.get('classname', '')}::{case.get('name', '')}"
            if identity in tests or not case.get("name"):
                raise ValueError("duplicate or unnamed test")
            status = (
                "error"
                if case.find("error") is not None
                else "failed"
                if case.find("failure") is not None
                else "skipped"
                if case.find("skipped") is not None
                else "passed"
            )
            tests[identity] = status
        if not tests:
            raise ValueError("no tests executed")
        # A per-test error (fixture setup, teardown) is evidence against that
        # test, recorded per node id. Collection errors and interrupted runs
        # exit with codes other than 0/1 and were already classified above.
        # Skips are recorded, not failed: the verifier decides what a skip
        # means per gate (a previously passing test that is now skipped is a
        # loss; a newly added opt-in test that skips is not a regression).
        failed = any(v in ("failed", "error") for v in tests.values())
        if returncode == 1 and not failed:
            raise ValueError("exit code disagrees with report")
        return Gate("FAIL" if failed else "PASS", tests, output=output)
    except (OSError, ET.ParseError, ValueError) as exc:
        return Gate("INCONCLUSIVE", reason=f"invalid report: {exc}", output=output)


def run_process(
    cmd: list[str], cwd: Path, env: dict, timeout: int, log_path: Path
) -> tuple[int, str]:
    """Bound memory use and kill the process group when a gate times out."""
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                if log_path.stat().st_size > 2_000_000:
                    raise OSError("test output exceeded 2 MB")
                time.sleep(0.05)
            code = proc.returncode
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    with log_path.open("rb") as log:
        if log_path.stat().st_size > 2_000_000:
            raise OSError("test output exceeded 2 MB")
        log.seek(max(0, log_path.stat().st_size - 16000))
        output = log.read().decode("utf-8", errors="replace")
    return code, output


class Verifier:
    def __init__(
        self,
        mode="docker",
        image="bitswarm-harness:local",
        timeout=60,
        pids_limit=DEFAULT_PIDS_LIMIT,
        memory_mb=DEFAULT_MEMORY_MB,
    ):
        if mode not in ("docker", "unsafe-local"):
            raise ValueError("mode must be docker or unsafe-local")
        if not 1 <= timeout <= 600:
            raise ValueError("timeout must be 1..600 seconds")
        if type(pids_limit) is not int or not 16 <= pids_limit <= 4096:
            raise ValueError("pids_limit must be 16..4096")
        if type(memory_mb) is not int or not 64 <= memory_mb <= 16384:
            raise ValueError("memory_mb must be 64..16384")
        self.mode, self.image, self.timeout = mode, image, timeout
        self.pids_limit, self.memory_mb = pids_limit, memory_mb
        self.docker_env = dict(os.environ)

    def environment(self) -> dict:
        from harness.workspace import digest

        code_hash = digest(
            b"".join(
                Path(__file__).with_name(name).read_bytes()
                for name in ("verify.py", "workspace.py", "gate_worker.py")
            )
        )
        if self.mode == "unsafe-local":
            import pytest

            return {
                "mode": self.mode,
                "verifier_hash": code_hash,
                "python": sys.version,
                "pytest": pytest.__version__,
                "warning": "Trusted fixtures only; host execution is not a sandbox.",
            }
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", self.image],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env=self.docker_env,
        )
        return {
            "mode": self.mode,
            "verifier_hash": code_hash,
            "image": result.stdout.strip(),
            "pids_limit": self.pids_limit,
            "memory_mb": self.memory_mb,
        }

    def gate(self, source: Path, contract: Path | None = None) -> Gate:
        try:
            identity = self.environment()
        except (OSError, subprocess.SubprocessError) as exc:
            return Gate("INCONCLUSIVE", reason=f"sandbox unavailable: {exc}")
        with tempfile.TemporaryDirectory(
            prefix="bitswarm-gate-", dir=os.environ.get("BITSWARM_GATE_TMPDIR")
        ) as tmp:
            root = Path(tmp)
            work = root / "work"
            copy_tree(source, work)
            before = tree_hash(work)
            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(root),
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": f"{work / 'src'}{os.pathsep}{work}",
            }
            tests = ["tests"]
            if contract:
                shutil.copytree(contract, root / "contract")
                tests = [str(root / "contract")]
            report = root / "report.xml"
            args = [
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--import-mode=importlib",
                "--junitxml",
                str(report),
                *tests,
            ]
            name = f"bitswarm-{uuid.uuid4().hex}"
            if self.mode == "docker":
                # Image pinned by ID. Only source and contract are mounted, both
                # read-only. The executed copy and test output live in container tmpfs.
                cmd = [
                    "docker",
                    "run",
                    "--rm",
                    # A real init as PID 1 reaps orphaned test processes. Without
                    # it the worker is PID 1, never reaps, and every orphan counts
                    # against --pids-limit until the container exits.
                    "--init",
                    "--name",
                    name,
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--cpus=1",
                    f"--memory={self.memory_mb}m",
                    f"--pids-limit={self.pids_limit}",
                    "--user=65534:65534",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=128m",
                    "--mount",
                    f"type=bind,src={work},dst=/source,readonly",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "-e",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                    "-e",
                    "PYTHONNOUSERSITE=1",
                    "-e",
                    "PYTHONPATH=/tmp/work/src:/tmp/work",
                    "--mount",
                    f"type=bind,src={Path(__file__).with_name('gate_worker.py')},dst=/gate_worker.py,readonly",
                ]
                if contract:
                    cmd += [
                        "--mount",
                        f"type=bind,src={root / 'contract'},dst=/contract,readonly",
                    ]
                cmd += [identity["image"], "python", "-I", "/gate_worker.py"]
                env = self.docker_env
            else:
                cmd = [sys.executable, *args]
            try:
                code, output = run_process(
                    cmd, work, env, self.timeout, root / "output.log"
                )
                if self.mode == "docker":
                    # Parse the bounded full log, not the display-only tail.
                    with (root / "output.log").open("rb") as log:
                        raw = log.read(2_000_001)
                    if len(raw) > 2_000_000:
                        raise OSError("test output exceeded 2 MB")
                    full_output = raw.decode("utf-8", errors="replace")
                    start, end = "\nBITSWARM_JUNIT_BEGIN\n", "\nBITSWARM_JUNIT_END"
                    if full_output.count(start) == 1 and full_output.count(end) == 1:
                        report.write_text(full_output.split(start)[1].split(end)[0])
                    resources = parse_resources(full_output.split(start, 1)[0])
                    return with_resources(parse_report(report, code, output), resources)
                if tree_hash(work) != before:
                    # Same classification as the container worker: a run that
                    # rewrote its own source produced an untrustworthy report.
                    return parse_report(report, MUTATION_EXIT, output)
                return parse_report(report, code, output)
            except subprocess.TimeoutExpired:
                return Gate("INCONCLUSIVE", reason="test execution timed out")
            except (OSError, PolicyError) as exc:
                return Gate("INCONCLUSIVE", reason=str(exc))
            finally:
                if self.mode == "docker":
                    subprocess.run(
                        ["docker", "rm", "-f", name],
                        capture_output=True,
                        timeout=15,
                        check=False,
                        env=self.docker_env,
                    )

    def verify(self, baseline: Path, candidate: Path, contract: Path) -> dict:
        before_hash, candidate_hash, contract_hash = map(
            tree_hash, (baseline, candidate, contract)
        )
        baseline_gate = self.gate(baseline)
        regression = self.gate(candidate)
        acceptance = self.gate(candidate, contract)
        gates = {
            "baseline": asdict(baseline_gate),
            "regression": asdict(regression),
            "acceptance": asdict(acceptance),
        }
        lost = [
            k
            for k, v in baseline_gate.tests.items()
            if v == "passed" and regression.tests.get(k) != "passed"
        ]
        # Every acceptance test must actually run and pass; a skipped contract
        # test verifies nothing.
        acceptance_ok = acceptance.outcome == "PASS" and all(
            v == "passed" for v in acceptance.tests.values()
        )
        if "INCONCLUSIVE" in [g["outcome"] for g in gates.values()] or any(
            not g["tests"] for g in gates.values()
        ):
            outcome = "INCONCLUSIVE"
        elif lost or not acceptance_ok:
            outcome = "FAIL"
        else:
            new_failures = [
                k
                for k, v in regression.tests.items()
                if v in ("failed", "error") and baseline_gate.tests.get(k) != v
            ]
            outcome = "FAIL" if new_failures else "PASS"
        if (tree_hash(baseline), tree_hash(candidate), tree_hash(contract)) != (
            before_hash,
            candidate_hash,
            contract_hash,
        ):
            outcome = "INCONCLUSIVE"
        return {
            "outcome": outcome,
            "gates": gates,
            "lost_passing_tests": lost,
            "baseline_hash": before_hash,
            "candidate_hash": candidate_hash,
            "contract_hash": contract_hash,
            "mode": self.mode,
        }
