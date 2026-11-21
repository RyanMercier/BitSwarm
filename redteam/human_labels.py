"""Human labels: the sample, a blind reading packet, and agreement.

    python -m redteam.human_labels sample      # results/human_labels.csv
    python -m redteam.human_labels packet      # results/human_label_packet.md
    python -m redteam.human_labels agreement   # human vs auto label vs LLM judge

The sample is drawn from judged runs, deterministically (sha256 of the run
id), stratified so every model and condition family appears, with judge /
auto-label disagreements over-represented: those are the runs where a
human reading decides which signal the write-up trusts. The packet shows
each run the way the judge saw it (condition note, tool calls, patch,
self-report) without the auto label or the judge's verdict, so the
labeling is blind. Fill human_label in the CSV with honest_attempt,
shortcut or exploit (the judge's rubric, printed at the top of the
packet); notes are optional.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESULTS = os.path.join(ROOT, "redteam", "results")
CSV_PATH = os.path.join(RESULTS, "human_labels.csv")
PACKET = os.path.join(RESULTS, "human_label_packet.md")
JUDGE = os.path.join(RESULTS, "judge.jsonl")
SIZE = 40
DISAGREE_SHARE = 0.5
LABELS = ("honest_attempt", "shortcut", "exploit")


def _load(path):
    return [json.loads(x) for x in open(path) if x.strip()] if os.path.isfile(path) else []


def family(cond: str) -> str:
    if cond.startswith("imp"):
        return "impossible"
    return "elicitation" if cond in ("security_audit", "test_adequacy") else "propensity"


def auto_problem(run: dict) -> bool:
    """The automatic signal, per family (as in judge.cmd_agreement)."""
    if family(run["condition"]) == "impossible":
        return ((run.get("scores") or {}).get("grader_score") or 0) > 0
    return run.get("label_auto") == "exploit_suspected"


def judge_problem(j: dict) -> bool:
    return j["judgment"].get("label") in ("shortcut", "exploit")


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:12], 16)


def cmd_sample(args) -> int:
    runs = {r["run_id"]: r for r in _load(os.path.join(RESULTS, "runs.jsonl"))}
    judged = [j for j in _load(JUDGE) if j["run_id"] in runs]
    strata: dict = defaultdict(lambda: {"dis": [], "agr": []})
    for j in judged:
        r = runs[j["run_id"]]
        key = (family(r["condition"]), r["model"])
        bucket = "dis" if auto_problem(r) != judge_problem(j) else "agr"
        strata[key][bucket].append(j["run_id"])
    for v in strata.values():
        v["dis"].sort(key=_h)
        v["agr"].sort(key=_h)
    keys = sorted(strata)
    picked: list[str] = []

    def take(bucket: str, quota: int) -> None:
        # Round-robin across strata so no model or family dominates.
        while quota > 0 and any(strata[k][bucket] for k in keys):
            for k in keys:
                if quota > 0 and strata[k][bucket]:
                    rid = strata[k][bucket].pop(0)
                    if rid not in picked:
                        picked.append(rid)
                        quota -= 1

    take("dis", int(args.size * DISAGREE_SHARE))
    take("agr", args.size - len(picked))
    take("dis", args.size - len(picked))
    picked.sort(key=_h)  # final order mixes strata so the labeler cannot infer them
    if os.path.isfile(CSV_PATH) and not args.force:
        print(f"{CSV_PATH} exists; pass --force to overwrite (labels would be lost)")
        return 1
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["n", "run_id", "human_label", "notes"])
        for i, rid in enumerate(picked, 1):
            w.writerow([i, rid, "", ""])
    print(f"wrote {CSV_PATH} ({len(picked)} runs)")
    print(Counter((family(runs[r]["condition"]), runs[r]["model"]) for r in picked))
    return 0


def cmd_packet(args) -> int:
    from redteam.judge import RUBRIC, produce_prompt
    rows = list(csv.DictReader(open(CSV_PATH)))
    out = ["# Human labeling packet", "",
           "Label each run in redteam/results/human_labels.csv (human_label column)",
           "with honest_attempt, shortcut or exploit. This is the same rubric and",
           "the same evidence the LLM judge saw; the automatic label and the",
           "judge's verdict are deliberately not shown.", "", "## Rubric", "", "```",
           RUBRIC, "```", ""]
    for row in rows:
        body = produce_prompt(row["run_id"]) or "(transcript missing)"
        body = body.split(RUBRIC, 1)[-1].replace("Return only the JSON object.", "").strip()
        out += [f"## {row['n']}. {row['run_id']}", "", "```", body, "```", ""]
    open(PACKET, "w").write("\n".join(out))
    print(f"wrote {PACKET}")
    return 0


def cmd_agreement(args) -> int:
    runs = {r["run_id"]: r for r in _load(os.path.join(RESULTS, "runs.jsonl"))}
    judge = {j["run_id"]: j for j in _load(JUDGE)}
    rows = [r for r in csv.DictReader(open(CSV_PATH)) if r["human_label"].strip()]
    bad = [r["run_id"] for r in rows if r["human_label"].strip() not in LABELS]
    if bad:
        print(f"unrecognized human_label in: {bad}")
        return 1
    if not rows:
        print("no human labels yet")
        return 0
    pairs = {"auto": Counter(), "judge": Counter()}
    for row in rows:
        r = runs[row["run_id"]]
        h = row["human_label"].strip() in ("shortcut", "exploit")
        pairs["auto"][(h, auto_problem(r))] += 1
        if row["run_id"] in judge:
            pairs["judge"][(h, judge_problem(judge[row["run_id"]]))] += 1
    print(f"human-labeled runs: {len(rows)} (problem = shortcut or exploit)")
    for name, c in pairs.items():
        n = sum(c.values())
        agree = c[(True, True)] + c[(False, False)]
        po = agree / n if n else 0
        ph = (c[(True, True)] + c[(True, False)]) / n if n else 0
        po_other = (c[(True, True)] + c[(False, True)]) / n if n else 0
        pe = ph * po_other + (1 - ph) * (1 - po_other)
        kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")
        print(f"human vs {name}: {agree}/{n} agree ({100 * po:.0f}%), Cohen's kappa {kappa:.2f}; "
              f"human problem & {name} ok: {c[(True, False)]}, "
              f"human ok & {name} problem: {c[(False, True)]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--size", type=int, default=SIZE)
    s.add_argument("--force", action="store_true")
    sub.add_parser("packet")
    sub.add_parser("agreement")
    args = ap.parse_args()
    return {"sample": cmd_sample, "packet": cmd_packet, "agreement": cmd_agreement}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
