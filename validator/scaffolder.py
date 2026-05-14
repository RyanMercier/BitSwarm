import os
import subprocess
import sys


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "BitSwarm",
    "GIT_AUTHOR_EMAIL": "bitswarm@local",
    "GIT_COMMITTER_NAME": "BitSwarm",
    "GIT_COMMITTER_EMAIL": "bitswarm@local",
}


def write_file(repo_path, relative_path, content):
    """Write a file to the repo, creating directories as needed."""
    full_path = os.path.join(repo_path, relative_path)
    # Top-level files have an empty dirname; makedirs("") would crash.
    parent = os.path.dirname(full_path) or "."
    os.makedirs(parent, exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)


def ensure_init_files(repo_path, file_paths):
    """Create __init__.py files for all package directories."""
    for path in file_paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            init_path = os.path.join(repo_path, *parts[:i], "__init__.py")
            if not os.path.isfile(init_path):
                with open(init_path, "w") as f:
                    f.write("")


def write_scaffolding(decomposition, repo_path):
    """
    Write the full scaffolding to disk and commit it.

    Writes shared files, stub files, stub test files, integration test files,
    updates requirements.txt, ensures __init__.py files exist, and commits.
    """
    shared_files = decomposition.get("shared_files", {})
    stub_files = decomposition.get("stub_files", {})
    stub_test_files = decomposition.get("stub_test_files", {})
    integration_test_files = decomposition.get("integration_test_files", {})
    requirements_additions = decomposition.get("requirements_additions", [])

    all_files = {**shared_files, **stub_files, **stub_test_files, **integration_test_files}

    # Write all files
    for path, content in all_files.items():
        write_file(repo_path, path, content)
        print(f"  [Scaffold] Wrote {path}")

    # Ensure __init__.py for all package dirs
    ensure_init_files(repo_path, all_files.keys())

    # Update requirements.txt (create it if the repo doesn't ship one).
    if requirements_additions:
        req_path = os.path.join(repo_path, "requirements.txt")
        existing = ""
        if os.path.isfile(req_path):
            with open(req_path, "r") as f:
                existing = f.read()
        with open(req_path, "a") as f:
            for req in requirements_additions:
                if req not in existing:
                    f.write(f"{req}\n")
        print(f"  [Scaffold] Added to requirements.txt: {requirements_additions}")

        # Install new requirements
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q"] + requirements_additions,
            capture_output=True, cwd=repo_path,
        )

    # Git commit the scaffolding
    # Remove stale lock file if present (leftover from crashed git process)
    lock_file = os.path.join(repo_path, ".git", "index.lock")
    if os.path.exists(lock_file):
        os.remove(lock_file)

    result = subprocess.run(
        ["git", "add", "-A"], cwd=repo_path, capture_output=True,
    )
    commit = subprocess.run(
        ["git", "commit", "-m", "BitSwarm scaffolding"],
        cwd=repo_path, capture_output=True, text=True, env=GIT_ENV,
    )
    if commit.returncode == 0:
        print("  [Scaffold] Committed scaffolding to git")
    else:
        print(f"  [Scaffold] WARNING: git commit failed: {commit.stderr.strip()}")
