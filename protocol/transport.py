"""
BitSwarm Transport Layer

Handles repo bundling/unbundling for network transfer.
"""
import base64
import os
import subprocess
import tempfile


def bundle_repo(repo_path: str) -> str:
    """Create a base64-encoded git bundle of a repo."""
    with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as f:
        bundle_path = f.name
    try:
        subprocess.run(
            ["git", "bundle", "create", bundle_path, "--all"],
            cwd=repo_path, capture_output=True, check=True,
        )
        with open(bundle_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    finally:
        os.unlink(bundle_path)


def unbundle_repo(bundle_b64: str, dest_path: str) -> str:
    """Unbundle a base64-encoded git bundle to a directory."""
    os.makedirs(dest_path, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as f:
        f.write(base64.b64decode(bundle_b64))
        bundle_path = f.name
    try:
        subprocess.run(
            ["git", "clone", bundle_path, dest_path],
            capture_output=True, check=True,
        )
        return dest_path
    finally:
        os.unlink(bundle_path)
