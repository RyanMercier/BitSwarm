"""Trusted container entry point, mounted read-only by the host verifier."""

import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")


def sandbox_resources():
    """Snapshot the container's own cgroup counters after the test run.

    With a private cgroup namespace (Docker's default on cgroup v2) the root of
    /sys/fs/cgroup is this container's cgroup, so the counters cover every
    process and thread the tests started, including ones that already exited.
    """
    snapshot = {
        "cpus_visible": os.cpu_count(),
        "rlimit_nproc": list(resource.getrlimit(resource.RLIMIT_NPROC)),
        "tmp_used_bytes": shutil.disk_usage("/tmp").used,
    }
    for name in ("pids.max", "pids.peak", "pids.current", "memory.max", "memory.peak"):
        try:
            snapshot[name] = (CGROUP / name).read_text().strip()
        except OSError:
            pass
    for name in ("pids.events", "memory.events"):
        try:
            lines = (CGROUP / name).read_text().splitlines()
            snapshot[name] = dict(line.split(None, 1) for line in lines if line.strip())
        except (OSError, ValueError):
            pass
    return snapshot


def hash_tree(root):
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("candidate contains a symlink")
        if path.is_file():
            result.update(str(path.relative_to(root)).encode())
            result.update(str(path.stat().st_mode & 0o777).encode())
            result.update(path.read_bytes())
    return result.hexdigest()


if __name__ == "__main__":
    work = Path("/tmp/work")
    shutil.copytree("/source", work)
    before = hash_tree(work)
    proc = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "--junitxml=/tmp/report.xml",
            "/contract" if Path("/contract").exists() else "tests",
        ],
        cwd=work,
        check=False,
    )
    print("\nBITSWARM_RESOURCES " + json.dumps(sandbox_resources()))
    if hash_tree(work) != before:
        print("BITSWARM_CANDIDATE_MUTATED")
        sys.exit(86)  # keep in sync with harness.verify.MUTATION_EXIT
    report = Path("/tmp/report.xml")
    print(
        "\nBITSWARM_JUNIT_BEGIN\n"
        + (report.read_text() if report.exists() else "")
        + "\nBITSWARM_JUNIT_END"
    )
    sys.exit(proc.returncode)
