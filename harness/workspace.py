"""Git and path boundaries. Never execute repository hooks or filters."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


class PolicyError(ValueError):
    pass


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or path.is_absolute()
        or any(p in ("..", ".git") for p in path.parts)
        or ":" in relative
        or "\x00" in relative
    ):
        raise PolicyError(f"unsafe path: {relative!r}")
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()):
        raise PolicyError(f"path escapes workspace: {relative!r}")
    if any(p.is_symlink() for p in (target, *target.parents)):
        raise PolicyError(f"symlink not allowed: {relative!r}")
    return target


def git(
    root: Path,
    *args: str,
    input: bytes | None = None,
    extra_env: dict | None = None,
    check=True,
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="BitSwarm",
        GIT_AUTHOR_EMAIL="bitswarm@local",
        GIT_COMMITTER_NAME="BitSwarm",
        GIT_COMMITTER_EMAIL="bitswarm@local",
    )
    env.update(extra_env or {})
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.autocrlf=false",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        cwd=root,
        input=input,
        capture_output=True,
        env=env,
        timeout=60,
        check=check,
    )


def snapshot(
    source: Path,
    destination: Path,
    revision: str,
    max_files: int = 3000,
    max_total_bytes: int = 30_000_000,
    max_file_bytes: int = 2_000_000,
) -> str:
    """Export tracked regular blobs only; omit source config, hooks and secrets.

    One ls-tree call supplies modes and sizes, one cat-file batch supplies
    contents, so cost does not scale with the number of git processes.
    """
    commit = (
        git(source, "rev-parse", "--verify", f"{revision}^{{commit}}")
        .stdout.decode()
        .strip()
    )
    entries = git(source, "ls-tree", "-rlz", commit).stdout.split(b"\0")
    blobs = []
    total = 0
    for entry in entries:
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, oid, size_text = meta.decode().split()
        rel = name.decode("utf-8")
        if kind != "blob" or mode not in ("100644", "100755"):
            raise PolicyError(f"unsupported tracked file mode: {rel} ({mode})")
        safe_path(destination, rel)
        size = int(size_text)
        total += size
        if size > max_file_bytes or total > max_total_bytes or len(blobs) >= max_files:
            raise PolicyError(
                f"repository exceeds harness size limits "
                f"({max_files} files, {max_total_bytes} bytes, {max_file_bytes} per file)"
            )
        blobs.append((rel, mode, oid))
    contents = _cat_file_batch(source, [oid for _, _, oid in blobs])
    destination.mkdir(parents=True, exist_ok=False)
    for rel, mode, oid in blobs:
        path = safe_path(destination, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[oid])
        path.chmod(0o755 if mode == "100755" else 0o644)
    git(destination, "init", "-q")
    # Disable repository .gitattributes clean filters by using only exported
    # content and no inherited global/local filter definitions.
    git(destination, "add", "-f", "--all")
    git(destination, "commit", "-qm", "harness baseline", "--allow-empty")
    return commit


def _cat_file_batch(source: Path, oids: list[str]) -> dict[str, bytes]:
    """Read many blobs with a single git process."""
    if not oids:
        return {}
    raw = git(source, "cat-file", "--batch", input=("\n".join(oids) + "\n").encode()).stdout
    contents: dict[str, bytes] = {}
    cursor = 0
    for _ in oids:
        newline = raw.index(b"\n", cursor)
        header = raw[cursor:newline].decode().split()
        if len(header) != 3:
            raise PolicyError(f"git cat-file could not read object {header[0]}")
        oid, size = header[0], int(header[2])
        start = newline + 1
        contents[oid] = raw[start : start + size]
        cursor = start + size + 1  # trailing newline after each object
    return contents


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def normalize_diff_prefixes(patch: str) -> str:
    """Give traditional ``--- path`` / ``+++ path`` headers git's a/ and b/ prefixes.

    ``git apply`` strips one leading path component by default. That is right
    for ``a/src/module.py`` and wrong for ``src/module.py``, which would resolve
    to ``module.py`` and then fail scope checks or apply to the wrong file.
    Hunk bodies are skipped by line count so removed lines that happen to start
    with ``--`` are never rewritten.
    """
    lines = patch.splitlines(keepends=True)
    out = []
    old_left = new_left = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if old_left > 0 or new_left > 0:
            first = line[:1]
            if first == "-":
                old_left -= 1
            elif first == "+":
                new_left -= 1
            elif first == "\\":
                pass  # "\ No newline at end of file"
            else:
                old_left -= 1
                new_left -= 1
            out.append(line)
            i += 1
            continue
        hunk = _HUNK.match(line)
        if hunk:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
            out.append(line)
            i += 1
            continue
        if (
            line.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
        ):
            out.append(_prefix_header(line, "a/"))
            out.append(_prefix_header(lines[i + 1], "b/"))
            i += 2
            continue
        out.append(line)
        i += 1
    return "".join(out)


def _prefix_header(line: str, prefix: str) -> str:
    marker, rest = line[:4], line[4:]
    body = rest.rstrip("\r\n")
    ending = rest[len(body) :]
    path, sep, trailer = body.partition("\t")
    if path == "/dev/null" or path.startswith((prefix, '"')):
        return line
    return f"{marker}{prefix}{path}{sep}{trailer}{ending}"


def tree_hash(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".git" in rel.parts:
            continue
        if path.is_symlink():
            raise PolicyError(f"symlink in artifact: {rel}")
        if path.is_file():
            hasher.update(str(rel).encode() + b"\0")
            hasher.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
            hasher.update(path.read_bytes())
    return hasher.hexdigest()


def apply_patch(root: Path, patch: str, allowed: list[str]) -> list[str]:
    """Validate the result in a separate index before touching the workspace."""
    if not patch.strip() or len(patch.encode()) > 1_000_000:
        raise PolicyError("patch is empty or exceeds 1 MB")
    patch = normalize_diff_prefixes(patch)
    for path in allowed:
        safe_path(root, path)
        # Acceptance/configuration paths never belong to an implementation.
        p = PurePosixPath(path)
        if (
            any(part.startswith(".") for part in p.parts)
            or any(part in ("tests", "test", "__pycache__") for part in p.parts)
            or p.name.startswith("test_")
            or p.name.endswith("_test.py")
            or p.name
            in (
                "conftest.py",
                "pyproject.toml",
                "pytest.ini",
                "setup.cfg",
                "tox.ini",
                "setup.py",
            )
        ):
            raise PolicyError(f"protected path in allowed files: {path}")
    with tempfile.TemporaryDirectory(prefix="bitswarm-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(root, "read-tree", "HEAD", extra_env=env)
        checked = git(
            root,
            "apply",
            "--cached",
            "--whitespace=nowarn",
            "-",
            input=patch.encode(),
            extra_env=env,
            check=False,
        )
        if checked.returncode:
            reason = checked.stderr.decode(errors="replace")[:1500]
            raise PolicyError(f"patch rejected without modifying workspace: {reason}")
        raw = git(
            root, "diff", "--cached", "--raw", "-z", "--no-renames", extra_env=env
        ).stdout
        parts = raw.split(b"\0")
        changed = []
        for i in range(0, len(parts) - 1, 2):
            meta, name = parts[i].decode(), parts[i + 1].decode()
            safe_path(root, name)
            if name not in allowed:
                raise PolicyError(f"patch changes unauthorized path: {name}")
            if meta.split()[1] not in ("100644", "100755", "000000"):
                raise PolicyError(f"patch changes unsupported file mode: {name}")
            changed.append(name)
        if not changed:
            raise PolicyError("patch makes no change")
        checked = git(root, "apply", "--check", "-", input=patch.encode(), check=False)
        if checked.returncode:
            raise PolicyError("patch does not apply to the current working tree")
        git(root, "apply", "--whitespace=nowarn", "-", input=patch.encode())
        git(root, "add", "-f", "--all")
        git(root, "commit", "-qm", "candidate update")
    return changed


def export_patch(root: Path) -> str:
    first = git(root, "rev-list", "--max-parents=0", "HEAD").stdout.decode().strip()
    return git(root, "diff", "--binary", first, "HEAD").stdout.decode()


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git"))
