"""
BitSwarm Transport Layer

Handles repo bundling/unbundling for network transfer.

Bundles ride inside synapse/HTTP bodies, so an unbounded bundle is a
denial-of-service vector on both sides: a validator could freeze a
miner with a giant repo, and a hostile response could balloon memory.
BITSWARM_MAX_BUNDLE_MB (default 64) caps the encoded size in both
directions with a clear error instead of an OOM.
"""
import base64
import os
import subprocess
import tempfile


def _max_bundle_bytes() -> int:
    mb = float(os.environ.get("BITSWARM_MAX_BUNDLE_MB", "64"))
    return int(mb * 1024 * 1024)


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
            encoded = base64.b64encode(f.read()).decode("ascii")
        limit = _max_bundle_bytes()
        if len(encoded) > limit:
            raise ValueError(
                f"repo bundle is {len(encoded) / 1e6:.1f} MB encoded, "
                f"over the {limit / 1e6:.0f} MB limit "
                f"(BITSWARM_MAX_BUNDLE_MB). Scope the task to a smaller "
                f"repo or raise the limit on both peers.")
        return encoded
    finally:
        os.unlink(bundle_path)


def unbundle_repo(bundle_b64: str, dest_path: str) -> str:
    """Unbundle a base64-encoded git bundle to a directory."""
    limit = _max_bundle_bytes()
    if len(bundle_b64) > limit:
        raise ValueError(
            f"incoming bundle is {len(bundle_b64) / 1e6:.1f} MB encoded, "
            f"over the {limit / 1e6:.0f} MB limit (BITSWARM_MAX_BUNDLE_MB); "
            f"refusing to decode.")
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
