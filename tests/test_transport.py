"""Test repo bundling round-trips intact through the transport layer."""
import os
import subprocess
import tempfile

from protocol.transport import bundle_repo, unbundle_repo


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


def _init_repo(path: str, files: dict[str, str]) -> None:
    os.makedirs(path, exist_ok=True)
    for rel, content in files.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full) or path, exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path, capture_output=True, check=True, env=GIT_ENV,
    )


def test_bundle_roundtrip_preserves_files():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        dst = os.path.join(tmp, "dst")
        files = {
            "app.py": "print('hi')\n",
            "pkg/mod.py": "x = 1\n",
            "tests/test_app.py": "def test(): assert True\n",
        }
        _init_repo(src, files)

        bundle_b64 = bundle_repo(src)
        assert bundle_b64, "bundle should not be empty"

        unbundle_repo(bundle_b64, dst)

        for rel, expected in files.items():
            full = os.path.join(dst, rel)
            assert os.path.isfile(full), f"{rel} missing after unbundle"
            with open(full) as f:
                assert f.read() == expected


def test_bundle_preserves_git_history():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src")
        dst = os.path.join(tmp, "dst")
        _init_repo(src, {"a.py": "1\n"})

        # Add a second commit so log has 2 entries
        with open(os.path.join(src, "a.py"), "w") as f:
            f.write("2\n")
        subprocess.run(["git", "add", "-A"], cwd=src, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "second"],
            cwd=src, capture_output=True, check=True, env=GIT_ENV,
        )

        bundle_b64 = bundle_repo(src)
        unbundle_repo(bundle_b64, dst)

        log = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=dst, capture_output=True, text=True, check=True,
        )
        subjects = log.stdout.strip().split("\n")
        assert subjects == ["second", "initial"]
