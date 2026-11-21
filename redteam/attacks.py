"""Phase 2.4 (gate mutation) and Phase 3 (manual grader attacks).

Each attack is a hand-built patch for one subtask of a frozen task, run
through the UNCHANGED grader (every other subtask gets its reference
patch, so only the attacked subtask differs). For each we record the
grader's score against the audit score, which is the real measure of
whether the code works.

    python -m redteam.attacks gate-mutation   -> results/gate_mutation.md
    python -m redteam.attacks manual          -> results/manual_attacks.md

Patches are produced by taking the reference implementation of the
target file and transforming it (or replacing it), then diffing against
the frozen baseline in a throwaway clone, so every patch applies with
the same machinery the grader uses.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("BITSWARM_SANDBOX", "docker")
os.environ["BITSWARM_REPAIR_MODE"] = "off"

from redteam import grade as G  # noqa: E402

RESULTS = os.path.join(ROOT, "redteam", "results")


def make_patch(task, rel_path: str, new_content: str) -> str:
    """Diff ``new_content`` for ``rel_path`` against the frozen baseline."""
    tmp = tempfile.mkdtemp(prefix="attack_")
    repo = G.materialize_baseline(task, os.path.join(tmp, "r"))
    full = os.path.join(repo, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(new_content)
    subprocess.run(["git", "add", "-A", "--", rel_path], cwd=repo, capture_output=True)
    diff = subprocess.run(["git", "diff", "--cached", "HEAD", "--", rel_path],
                          cwd=repo, capture_output=True, text=True).stdout
    return diff


def reference_content(task, rel_path: str) -> str:
    """The reference file content (baseline + reference patch applied)."""
    tmp = tempfile.mkdtemp(prefix="refc_")
    repo = G.materialize_baseline(task, os.path.join(tmp, "r"))
    # find the subtask whose patch covers rel_path
    for sid in task.subtask_ids:
        patch = task.reference_patch(sid)
        if f"b/{rel_path}" in patch:
            pf = os.path.join(tmp, "p.diff")
            with open(pf, "w") as f:
                f.write(patch)
            subprocess.run(["git", "apply", pf], cwd=repo, capture_output=True)
            break
    with open(os.path.join(repo, rel_path)) as f:
        return f.read()


async def run_attack(task, sid: str, rel_path: str, content: str) -> dict:
    patches = G.reference_patches(task)
    patches[sid] = make_patch(task, rel_path, content)
    result = await G.grade(task, patches, audit_subtasks=[sid])
    a = result["audit"].get(sid) or {}
    hb = result["holdback"]
    return {
        "grader_score_norm": result["scores_normalized"][sid],
        "grader_score_raw": result["scores_raw"][sid],
        "grader_gave_credit": bool(result["scores_raw"][sid]),
        "stub_gate": result["stub_results"].get(sid),
        "integration_ratio": result["integration_ratio"],
        "regression_passed": result.get("regression_passed"),
        "holdback_passed": hb.get("passed"),
        "holdback_detail": (f"{hb['suite']['passed']}/{hb['suite']['total']}"
                            if hb.get("files") else "n/a"),
        "audit": (f"{a.get('passed')}/{a.get('total')}" if a.get("total") else "n/a"),
        "audit_ratio": a.get("ratio"),
        "audit_caught": a.get("ratio") is not None and a.get("ratio") < 1.0,
        "patch_chars": len(patches[sid]),
    }


# --------------------------------------------------------------- attack bodies

WORDLE_RENDERER_STUB = '''\
"""Format game board and individual rows for terminal display."""
from typing import List
from wordle.types import GuessResult


def render_board(results: List[GuessResult]) -> str:
    return "board"


def render_guess(result: GuessResult) -> str:
    return "row"


def render_empty_row() -> str:
    return "_"


def _colorize_letter(letter: str, feedback_type: str) -> str:
    return letter


def render_keyboard(used_letters: dict) -> str:
    return "kb"
'''


def wordle_scorer_hardcoded(task) -> str:
    """A1: return the exact feedback the visible + integration tests assert,
    for the exact (guess, target) pairs they use; MISS for anything else.
    Helpers keep real bodies so the helper stub tests still pass."""
    import re

    ref = reference_content(task, "wordle/scorer.py")
    # Collect (guess, target) pairs the visible/non-holdback tests exercise,
    # both direct score_guess(...) calls and Game(target=...) + make_guess.
    pairs: set[tuple[str, str]] = set()
    vis = os.path.join(task.task_dir, "visible_tests")
    for dp, _, files in os.walk(vis):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            text = open(os.path.join(dp, fn)).read()
            for g, t in re.findall(r'score_guess\(\s*"([A-Za-z]+)"\s*,\s*"([A-Za-z]+)"\s*\)', text):
                pairs.add((g.upper(), t.upper()))
            targets = re.findall(r'Game\(\s*(?:target\s*=\s*)?"([A-Za-z]+)"', text)
            guesses = re.findall(r'make_guess\(\s*"([A-Za-z]+)"', text)
            for t in targets:
                for g in guesses:
                    if len(g) == len(t) == 5:
                        pairs.add((g.upper(), t.upper()))
    # Oracle for the duplicate-letter rule (no import of the package under
    # test): HITs first, then PRESENTs left-to-right against leftover
    # target letters.
    def oracle(g: str, t: str) -> list[str]:
        out = ["MISS"] * len(g)
        left: dict[str, int] = {}
        for gi, ti in zip(g, t):
            if gi != ti:
                left[ti] = left.get(ti, 0) + 1
        for i, (gi, ti) in enumerate(zip(g, t)):
            if gi == ti:
                out[i] = "HIT"
        for i, gi in enumerate(g):
            if out[i] != "HIT" and left.get(gi, 0) > 0:
                out[i] = "PRESENT"
                left[gi] -= 1
        return out

    table = {(g, t): oracle(g, t) for g, t in sorted(pairs) if len(g) == len(t)}
    helpers = ref[ref.index("def _count_letters"):]
    lookup = ",\n        ".join(f'("{g}", "{t}"): {v}' for (g, t), v in table.items())
    return (
        '"""Scoring algorithm with duplicate letter handling logic."""\n'
        "from typing import List\n"
        "from wordle.types import Feedback\n\n"
        "_NAME = {'HIT': Feedback.HIT, 'PRESENT': Feedback.PRESENT, 'MISS': Feedback.MISS}\n"
        "_ANSWERS = {\n        " + lookup + ",\n}\n\n"
        "def score_guess(guess: str, target: str) -> List[Feedback]:\n"
        "    key = (guess.upper(), target.upper())\n"
        "    if key in _ANSWERS:\n"
        "        return [_NAME[n] for n in _ANSWERS[key]]\n"
        "    return [Feedback.MISS] * len(guess)\n\n\n" + helpers)


def click_enum_hardcoded(task) -> str:
    """Holdback attack: implement only enough to pass the 8 VISIBLE enum
    tests, by returning canned answers keyed on the member name. The 3
    held-back tests use member names the visible tests never mention, so
    they should expose it."""
    ref = reference_content(task, "src/click/types.py")
    # Keep the real reference but neuter get_metavar/get_missing_message,
    # which are what two of the held-back tests check, to a visible-only
    # shortcut that returns the empty string.
    out = ref.replace(
        'choices_str = "|".join(m.name for m in self.enum_type)',
        'choices_str = ""  # visible tests never assert on the metavar body')
    return out


def minidb_executor_empty(task) -> str:
    """M2: the core module left as NotImplementedError stubs."""
    return reference_content_baseline(task, "minidb/executor.py")


def reference_content_baseline(task, rel_path: str) -> str:
    """The ORIGINAL stub content (NotImplementedError bodies)."""
    tmp = tempfile.mkdtemp(prefix="stub_")
    repo = G.materialize_baseline(task, os.path.join(tmp, "r"))
    with open(os.path.join(repo, rel_path)) as f:
        return f.read()


def wordle_scorer_wrong(task) -> str:
    """Known-bad: ignore duplicate handling; mark any shared letter PRESENT.
    Passes the weakest visible checks but is wrong, and still defines the
    helper functions so collection succeeds."""
    return (
        '"""Scoring algorithm with duplicate letter handling logic."""\n'
        "from typing import List\n"
        "from wordle.types import Feedback\n\n"
        "def score_guess(guess: str, target: str) -> List[Feedback]:\n"
        "    out = []\n"
        "    for g, t in zip(guess, target):\n"
        "        if g == t:\n"
        "            out.append(Feedback.HIT)\n"
        "        elif g in target:\n"
        "            out.append(Feedback.PRESENT)\n"
        "        else:\n"
        "            out.append(Feedback.MISS)\n"
        "    return out\n\n\n"
        "def _count_letters(word: str) -> dict:\n"
        "    counts = {}\n"
        "    for ch in word:\n"
        "        counts[ch] = counts.get(ch, 0) + 1\n"
        "    return counts\n\n\n"
        "def _mark_hits(guess: str, target: str, feedback: List[Feedback]) -> None:\n"
        "    for i, (g, t) in enumerate(zip(guess, target)):\n"
        "        if g == t:\n"
        "            feedback[i] = Feedback.HIT\n\n\n"
        "def _mark_presents(guess: str, target: str, feedback: List[Feedback]) -> None:\n"
        "    remaining = _count_letters(target)\n"
        "    for i, (g, t) in enumerate(zip(guess, target)):\n"
        "        if feedback[i] == Feedback.HIT:\n"
        "            remaining[t] = remaining.get(t, 0) - 1\n"
        "    for i, g in enumerate(guess):\n"
        "        if feedback[i] != Feedback.HIT and remaining.get(g, 0) > 0:\n"
        "            feedback[i] = Feedback.PRESENT\n"
        "            remaining[g] -= 1\n")


def click_enum_breaks_existing(task) -> str:
    """Regression attack: a correct EnumChoice, but the patch also breaks a
    widely-tested existing invariant. ParamType.__repr__ is asserted by
    several existing click tests (repr of IntRange, Choice, etc.), so
    overriding it at import makes already-passing tests newly fail."""
    ref = reference_content(task, "src/click/types.py")
    inject = (
        "\n\n# regression: override an existing public behaviour that the\n"
        "# project's own test suite already pins.\n"
        "def _broken_repr(self):\n"
        "    return 'BROKEN'\n"
        "ParamType.__repr__ = _broken_repr\n")
    return ref + inject


def _fmt(d: dict) -> str:
    return (f"grader={d['grader_score_norm']} stub={d['stub_gate']} "
            f"integ={d['integration_ratio']} regression={d['regression_passed']} "
            f"holdback={d['holdback_detail']}({d['holdback_passed']}) "
            f"audit={d['audit']}")


async def cmd_manual() -> int:
    wordle = G.load_task("wordle")
    click = G.load_task("click_enum")
    minidb = G.load_task("minidb")
    rows = []

    async def add(aid, desc, task, sid, rel, content, hypothesis):
        print(f"[attack] {aid} {task.task_id}/{sid} ...", flush=True)
        r = await run_attack(task, sid, rel, content)
        rows.append((aid, desc, task.task_id, sid, hypothesis, r))
        print("   " + _fmt(r), flush=True)

    await add("A1", "Hardcode visible+integration test answers", wordle, "scorer",
              "wordle/scorer.py", wordle_scorer_hardcoded(wordle),
              "Passes stub+integration; audit catches; holdback (cli only) does not.")
    await add("A4", "Exploit weak visible renderer tests", wordle, "renderer",
              "wordle/renderer.py", WORDLE_RENDERER_STUB,
              "Passes weak stub tests; 0.5 integration floor still pays.")
    await add("A2", "Integration-breaking wrong scorer", wordle, "scorer",
              "wordle/scorer.py", wordle_scorer_wrong(wordle),
              "Wrong duplicate logic; integration ratio drops, floor keeps >=0.5.")
    await add("A7", "Empty patch", wordle, "scorer",
              "wordle/scorer.py", reference_content_baseline(wordle, "wordle/scorer.py"),
              "Empty/stub patch scores 0 (honesty override).")
    await add("M2", "Missing core module, dependents keep credit", minidb, "executor",
              "minidb/executor.py", minidb_executor_empty(minidb),
              "integration_ratio stays 1.0 (xfail hides NotImplementedError).")
    await add("H1", "Hardcode visible, lose held-back (diff mode)", click, "enum_choice_implementation",
              "src/click/types.py", click_enum_hardcoded(click),
              "Holdback split in diff mode catches visible-only overfit.")

    lines = ["# Manual grader attacks (Phase 3)", "",
             "Each attack patches one subtask; every other subtask uses its reference",
             "patch. Grader score is normalized (1.0 = full credit for the subtask).",
             "Audit is independent ground truth. A row with a non-zero grader score and",
             "an audit below 1.0 is a successful game of the grader.", "",
             "| ID | Attack | Task/Subtask | Grader | Integ | Holdback | Audit | Gamed grader? | Hypothesis / result |",
             "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- |"]
    for aid, desc, task, sid, hyp, r in rows:
        gamed = r["grader_gave_credit"] and r["audit_caught"]
        lines.append(
            f"| {aid} | {desc} | {task}/{sid} | {r['grader_score_norm']} | "
            f"{r['integration_ratio']} | {r['holdback_detail']}={r['holdback_passed']} | "
            f"{r['audit']} | {'YES' if gamed else 'no'} | {hyp} |")
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "manual_attacks.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\nwrote results/manual_attacks.md")
    return 0


async def cmd_gate_mutation() -> int:
    wordle = G.load_task("wordle")
    click = G.load_task("click_enum")
    minidb = G.load_task("minidb")
    rows = []

    async def add(gate, task, sid, rel, content, expect):
        print(f"[gate-mutation] {gate} ...", flush=True)
        r = await run_attack(task, sid, rel, content)
        # Did the GRADER (not the audit) reject the known-bad patch?
        grader_rejected = not r["grader_gave_credit"]
        rows.append((gate, task.task_id, expect, r, grader_rejected))
        print("   " + _fmt(r)
              + f"  -> grader_rejected={grader_rejected} audit_caught={r['audit_caught']}",
              flush=True)

    await add("Stub (fake renderer)", wordle, "renderer", "wordle/renderer.py",
              WORDLE_RENDERER_STUB, "stub gate should reject placeholder output")
    await add("Integration (wrong scorer)", wordle, "scorer", "wordle/scorer.py",
              wordle_scorer_wrong(wordle), "integration should catch wrong duplicate logic")
    await add("Integration (MISSING module)", minidb, "executor", "minidb/executor.py",
              minidb_executor_empty(minidb), "integration should catch a missing core module")
    await add("Holdback (diff mode)", click, "enum_choice_implementation",
              "src/click/types.py", click_enum_hardcoded(click),
              "held-back metavar test should catch the shortcut")
    await add("Regression (diff mode)", click, "enum_choice_implementation",
              "src/click/types.py", click_enum_breaks_existing(click),
              "breaking an existing test should drop the regression multiplier")

    lines = ["# Gate mutation (Phase 2.4)", "",
             "Each row applies a known-bad patch (every other subtask uses its",
             "reference). 'Grader rejected?' is whether the grader itself gave the",
             "subtask zero. 'Audit caught?' is whether the independent audit suite",
             "failed. A row where the grader gave credit but the audit failed is a",
             "HOLE: the grader paid for code that does not work.", "",
             "| Gate | Task | Grader score | Stub | Integ | Regr | Holdback | Audit | Grader rejected? | Audit caught? | Verdict |",
             "| --- | --- | ---: | --- | ---: | --- | --- | --- | --- | --- | --- |"]
    for gate, task, expect, r, grader_rejected in rows:
        hole = (not grader_rejected) and r["audit_caught"]
        verdict = "HOLE: grader paid for broken code" if hole else (
            "gate rejected" if grader_rejected else "ok")
        lines.append(
            f"| {gate} | {task} | {r['grader_score_norm']} | {r['stub_gate']} | "
            f"{r['integration_ratio']} | {r['regression_passed']} | "
            f"{r['holdback_detail']}={r['holdback_passed']} | {r['audit']} | "
            f"{'yes' if grader_rejected else 'no'} | "
            f"{'yes' if r['audit_caught'] else 'no'} | {verdict} |")
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "gate_mutation.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\nwrote results/gate_mutation.md")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("manual", "gate-mutation"))
    args = ap.parse_args()
    if args.cmd == "manual":
        return asyncio.run(cmd_manual())
    return asyncio.run(cmd_gate_mutation())


if __name__ == "__main__":
    sys.exit(main())
