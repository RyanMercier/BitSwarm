"""Trusted container entry point, mounted read-only by the host verifier."""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path


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
