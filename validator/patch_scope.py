"""Parse patch paths with Git, including ordinary unified diffs and renames."""

import subprocess

from harness.workspace import normalize_diff_prefixes


def unauthorized_paths(patch_text, allowed_files):
    # Traditional "--- src/x.py" headers must gain a/ b/ prefixes first, or
    # git's default -p1 strips a real directory and every path looks foreign.
    patch_text = normalize_diff_prefixes(patch_text)
    proc = subprocess.run(
        ["git", "apply", "--numstat", "-z", "-"],
        input=patch_text.encode(),
        capture_output=True,
        timeout=15,
        check=False,
    )
    if proc.returncode:
        return ["<invalid patch>"]
    fields = proc.stdout.split(b"\0")
    paths = []
    i = 0
    try:
        while i < len(fields) and fields[i]:
            _, _, name = fields[i].split(b"\t", 2)
            i += 1
            if name:
                paths.append(name.decode())
            else:
                paths.extend([fields[i].decode(), fields[i + 1].decode()])
                i += 2
    except (ValueError, IndexError, UnicodeError):
        return ["<invalid patch paths>"]
    if not paths:
        return ["<empty patch>"]
    return [p for p in paths if p not in allowed_files]
