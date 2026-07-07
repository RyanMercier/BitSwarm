"""
Sandboxed execution for gate test commands.

The validator executes miner-supplied code every time it runs a gate:
applying a patch is inert, but running the tests afterward executes
whatever the patch shipped. On a production validator that code must
not run on the host with the host's network, filesystem, and
environment (wallet keys, API keys). This module wraps gate commands
in a locked-down docker container.

BITSWARM_SANDBOX selects the mode:

  auto (default)  use docker when the daemon and image are present;
                  otherwise run on the host and print one loud warning
  docker          require docker; gate runs fail hard without it
  off             host execution (development only)

BITSWARM_SANDBOX_IMAGE names the container image (default
``bitswarm-base:latest``, built by docker/Dockerfile.base with all
seven language toolchains).

The container gets:

  --network=none            no egress: no exfiltration, no callbacks
  --cpus / --memory /       resource ceilings so a hostile patch
  --pids-limit              cannot starve the validator
  --user <uid>:<gid>        files written in the mount stay owned by
                            the operator, not root
  -v <repo>:/work -w /work  only the repo under test is visible

Environment forwarding is allowlist-only (PYTHON*, LANG, LC_ALL,
plus HOME=/tmp): the host environment, including any key material,
never crosses into the container. Env values and argv entries that
contain the host repo path are rewritten to /work so PYTHONPATH-style
import pinning survives the mount, and a host-specific ``python``
interpreter path becomes plain ``python3``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid

DEFAULT_IMAGE = "bitswarm-base:latest"
_ENV_ALLOWLIST_PREFIXES = ("PYTHON",)
_ENV_ALLOWLIST_EXACT = ("LANG", "LC_ALL", "NODE_OPTIONS", "CARGO_TARGET_DIR")

# Cached docker availability probe and the warn-once flag for auto
# fallback. Module-level on purpose: one probe and one warning per
# process, not per gate run.
_docker_ok: bool | None = None
_warned_fallback = False


def sandbox_mode() -> str:
    mode = os.environ.get("BITSWARM_SANDBOX", "auto").strip().lower()
    if mode not in ("auto", "docker", "off"):
        raise RuntimeError(
            f"BITSWARM_SANDBOX={mode!r}: expected auto, docker, or off"
        )
    return mode


def sandbox_image() -> str:
    return os.environ.get("BITSWARM_SANDBOX_IMAGE", DEFAULT_IMAGE)


def docker_available(image: str | None = None) -> bool:
    """One-time probe: docker binary present, daemon up, image pulled."""
    global _docker_ok
    if _docker_ok is not None:
        return _docker_ok
    image = image or sandbox_image()
    if shutil.which("docker") is None:
        _docker_ok = False
        return False
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, timeout=15,
        )
        _docker_ok = probe.returncode == 0
    except Exception:
        _docker_ok = False
    return _docker_ok


def reset_probe_cache() -> None:
    """Testing hook: forget the docker probe result and warning state."""
    global _docker_ok, _warned_fallback
    _docker_ok = None
    _warned_fallback = False


def is_active() -> bool:
    """Whether gate commands will actually run inside docker."""
    global _warned_fallback
    mode = sandbox_mode()
    if mode == "off":
        return False
    if docker_available():
        return True
    if mode == "docker":
        raise RuntimeError(
            "BITSWARM_SANDBOX=docker but docker (or the image "
            f"{sandbox_image()!r}) is unavailable. Build it with: "
            "docker build -f docker/Dockerfile.base -t "
            f"{sandbox_image()} ."
        )
    if not _warned_fallback:
        print("[sandbox] WARNING: docker unavailable; gate tests run "
              "UNSANDBOXED on the host. Fine for development, not for "
              "a production validator. Set BITSWARM_SANDBOX=docker to "
              "make this a hard failure, or build the image: "
              f"docker build -f docker/Dockerfile.base -t "
              f"{sandbox_image()} .")
        _warned_fallback = True
    return False


def wrap_command(cmd: list, repo_root: str,
                  env: dict | None = None,
                  image: str | None = None,
                  container_name: str | None = None) -> list:
    """Rewrite a host gate command into its docker equivalent.

    Pure function (no docker calls) so the translation is unit-testable:
    host repo paths become /work in both argv and forwarded env values,
    the host Python interpreter becomes ``python3``, and only
    allowlisted env vars cross the boundary.
    """
    repo_abs = os.path.abspath(repo_root)
    image = image or sandbox_image()

    def translate(value: str) -> str:
        return value.replace(repo_abs, "/work")

    docker_cmd = [
        "docker", "run", "--rm",
        "--network=none",
        f"--cpus={os.environ.get('BITSWARM_SANDBOX_CPUS', '2')}",
        f"--memory={os.environ.get('BITSWARM_SANDBOX_MEM', '4g')}",
        "--pids-limit=512",
    ]
    if container_name:
        docker_cmd += ["--name", container_name]
    if hasattr(os, "getuid"):
        docker_cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    docker_cmd += ["-v", f"{repo_abs}:/work", "-w", "/work"]

    for key in sorted(env or {}):
        if (key.startswith(_ENV_ALLOWLIST_PREFIXES)
                or key in _ENV_ALLOWLIST_EXACT):
            docker_cmd += ["-e", f"{key}={translate(env[key])}"]
    docker_cmd += ["-e", "HOME=/tmp"]
    docker_cmd.append(image)

    argv = []
    for arg in cmd:
        if arg == sys.executable:
            argv.append("python3")
        else:
            argv.append(translate(arg))
    return docker_cmd + argv


def run(cmd: list, repo_root: str, env: dict | None = None,
        timeout: int = 300, cwd: str | None = None
        ) -> subprocess.CompletedProcess:
    """Run a gate command, sandboxed when the mode allows.

    Host mode preserves the original semantics exactly (cwd defaults
    to repo_root, env passed through). Docker mode mounts repo_root at
    /work; on timeout the container is force-removed so a hung test
    cannot outlive its gate.
    """
    if not is_active():
        return subprocess.run(
            cmd, cwd=cwd or repo_root, env=env if env is not None else None,
            capture_output=True, text=True, timeout=timeout,
        )

    name = f"bitswarm-gate-{uuid.uuid4().hex[:12]}"
    wrapped = wrap_command(cmd, repo_root, env=env, container_name=name)
    try:
        return subprocess.run(
            wrapped, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name],
                        capture_output=True, timeout=30)
        raise
