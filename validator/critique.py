"""
Self-critique pass over a Phase 2 decomposition.

After the coordinator emits stubs + tests + integration tests, this
module asks Claude to read those files and identify cross-file
interface drift before scaffolding lands. Cheap relative to mining
(one Claude call vs N), catches the class of bugs Phase 1.5 can't
reach for non-Python languages (e.g. the C++ Game ctor drift).

The critique runs in two modes:

  - **SDK** (default when ``ANTHROPIC_API_KEY`` is set): uses the
    Anthropic Python SDK with a small structured-output request.
  - **Claude Code subprocess**: shells out to ``claude -p`` so
    subscription-only users get the same benefit at $0.

A successful critique returns ``[]``. Detected issues come back as
human-readable strings, so the existing ``validate_decomposition``
plumbing in ``decompose()`` treats them like any other error and
triggers a coordinator retry.

Disable with ``BITSWARM_SKIP_CRITIQUE=1``.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any


def is_disabled() -> bool:
    return os.environ.get("BITSWARM_SKIP_CRITIQUE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


_PROMPT_HEADER = """\
You are reviewing a parallel-coding decomposition before any miners run.

Your job: find cross-file interface drift -- places where one file
assumes a different signature, type, or import shape than another file
in the same decomposition. Be specific. If everything looks consistent,
say so.

Focus areas (in order):
  1. Constructor / function signatures used in tests vs declared in stubs.
  2. Type names referenced in tests but never declared in any stub or
     shared header.
  3. Imports/includes that don't resolve to any file in the
     decomposition.
  4. Cross-file name drift: a test calls ``thing.process(x, y)`` but
     the stub declares ``process(self, x)``.

For each issue you find, emit one ``ISSUE:`` line followed by a brief
problem statement. Example:
    ISSUE: tests/test_game.cpp constructs Game(words, "hello") but
           wordle/game.hpp declares Game(const std::string& target).

If nothing is wrong, emit exactly: ``OK: no interface drift detected``.

Do not propose fixes. Do not edit files. Just report.
"""


def _format_files(label: str, files: dict[str, str], limit_each: int = 6000) -> str:
    """Render a name -> content dict as a labelled block of code fences."""
    if not files:
        return f"## {label}: (none)\n"
    chunks = [f"## {label}"]
    for path in sorted(files):
        body = files[path]
        if len(body) > limit_each:
            body = body[:limit_each] + "\n... (truncated)"
        chunks.append(f"\n### {path}\n```\n{body}\n```")
    return "\n".join(chunks) + "\n"


def build_critique_prompt(decomposition: dict[str, Any]) -> str:
    shared = decomposition.get("shared_files", {}) or {}
    stubs = decomposition.get("stub_files", {}) or {}
    tests = decomposition.get("stub_test_files", {}) or {}
    integ = decomposition.get("integration_test_files", {}) or {}
    return "\n".join([
        _PROMPT_HEADER,
        _format_files("Shared types / headers", shared),
        _format_files("Stub files (will be mined)", stubs),
        _format_files("Stub test files", tests),
        _format_files("Integration test files", integ),
    ])


def _parse_issues(text: str) -> list[str]:
    """Pull ``ISSUE: ...`` lines out of the model's response."""
    issues: list[str] = []
    current: list[str] = []
    for raw in (text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("ISSUE:"):
            if current:
                issues.append(" ".join(current).strip())
                current = []
            current.append(stripped[len("ISSUE:"):].strip())
        elif current and stripped and not stripped.startswith("OK"):
            # Continuation lines of the previous ISSUE entry.
            current.append(stripped)
        elif current:
            issues.append(" ".join(current).strip())
            current = []
    if current:
        issues.append(" ".join(current).strip())
    return [i for i in issues if i]


def _run_via_sdk(prompt: str, model: str) -> str:
    import anthropic
    from config import ANTHROPIC_API_KEY, ANTHROPIC_BASE_URL
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY,
                                  base_url=ANTHROPIC_BASE_URL)
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts)


def _run_via_subprocess(prompt: str, model: str) -> str:
    binary = (os.environ.get("CC_COORDINATOR_BINARY", "")
              or os.environ.get("MINER_CC_BINARY", "")
              or "claude")
    # Prompt via stdin to avoid Linux ARG_MAX on large repo contexts.
    cmd = [
        binary,
        "-p",
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--output-format", "text",
        "--tools", "",  # critique is pure text, no tool calls
        "--model", model,
        "--setting-sources", "",
        "--disable-slash-commands",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        # Don't block the pipeline on a slow critique -- just skip it.
        return "OK: critique subprocess timed out (skipped)"
    if proc.returncode != 0:
        return f"OK: critique subprocess rc={proc.returncode} (skipped)"
    return proc.stdout or ""


def critique(decomposition: dict[str, Any]) -> list[str]:
    """Return a list of issue strings (empty means "looks fine").

    Picks the SDK or subprocess backend automatically based on the
    same ``COORDINATOR_BACKEND`` env var the rest of the pipeline
    uses. Failure to invoke the backend at all returns ``[]`` rather
    than blocking the pipeline.
    """
    if is_disabled():
        return []
    from config import COORDINATOR_BACKEND, COORDINATOR_MODEL
    prompt = build_critique_prompt(decomposition)
    backend = COORDINATOR_BACKEND or "sdk"
    try:
        if backend == "claude_code":
            text = _run_via_subprocess(prompt, COORDINATOR_MODEL)
        else:
            text = _run_via_sdk(prompt, COORDINATOR_MODEL)
    except Exception as exc:
        # Critique is advisory; don't block the pipeline if the call
        # fails (missing API key, network blip, etc.). Surface it once.
        print(f"[Critique] backend error ({type(exc).__name__}), skipping: {exc}")
        return []
    return _parse_issues(text)
