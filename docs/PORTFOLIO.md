# BitSwarm: portfolio and resume material

## Project entry

**BitSwarm | Coding-agent harness and patch verifier**
Python, Docker, SQLite, Git, pytest, Claude CLI, GitHub Actions

- Built a coding-agent runtime with scoped JSON tools, separate implementer and
  reviewer contexts, a bounded repair cycle, transactional checkpoints, and
  inspectable traces.
- Implemented sandboxed verification with frozen acceptance contracts, per-test
  regression detection, three-way outcomes, image and source hashes, and
  saved-patch replay; 43 harness unit tests plus six live container checks.
- Ran a solo-versus-reviewed pilot on three tasks with all attempts retained,
  then used post-hoc probes to show both "verified" retry-parser patches were
  wrong, versioned a stronger contract, and confirmed it rejected fresh attempts.
- Originally shipped as a Bittensor subnet (seven-language scaffold mode, diff
  mode on a 45,000-line open-source repo); pivoted after investor feedback to
  the verification layer and the evidence needed to sell it.

Use these as project bullets, not employment experience. If space is tight,
keep the first two and link the evidence. Do not describe the work as deployed
at scale, production hardened, a statistically established model improvement,
or a mixed-model evaluation. The repository suite also carries several hundred
legacy tests; do not imply they were all newly written.

## Thirty-second explanation

"I built a harness that compares a coding model working alone with the same
model working through an independent reviewer. The runtime owns the tools and
the state; a sandboxed verifier decides whether a patch passes. My first pilot
accepted all six attempts. When I audited one reviewer repair, it was wrong for
inputs the tests never covered. I kept the original results, versioned a
stronger contract, and made every verdict replayable. The project started as a
Bittensor subnet; the investor meeting taught me that the sellable part was
verification, and that I needed to know what my verification signal was worth
before selling it."

## Interview discussion

**Why did you pivot?** The subnet worked technically. What it lacked was a
clear path to monetization: who pays, for what, and why through a network
instead of a vendor. Generation competes with the labs. Verification of
AI-written patches has a buyer in every team adopting coding agents, and it is
cheap, deterministic, and replayable, which is also what a decentralized scoring
mechanism needs. Before selling verification I had to prove the signal meant
something, so I built the smallest thing that could measure it.

**Will anyone use this?** The harness as it stands is a research instrument:
single host, stdlib Python, tests under one directory. The reusable slice is
`bitswarm verify`, which turns a commit, a patch, an allow list, and a test
directory into a replayable verdict with no model in the loop. Wrapping that in
a GitHub Action and shipping dependency images is the path from instrument to
tool. I am honest about the gap because the gap is the plan.

**Why build the loop yourself?** To make tool permissions, state transitions,
failure classification, and cost accounting visible and testable. Provider
clients produce actions; they do not own the repository or the loop.

**What makes review independent?** A fresh role context without the
implementer's rationale, no edit rights, a finding schema that demands
evidence, and its own turn budget. It is the same model family in the pilot, so
correlated blind spots remain. Context separation is not statistical
independence.

**Why three outcomes?** A failing assertion is evidence against a candidate.
A timeout, a missing report, or a test run that rewrote its own source says the
measurement did not finish reliably. Folding those into pass or fail makes both
acceptance rates and debugging misleading.

**Why refuse automatic retry after a crash?** The runtime cannot know whether
an interrupted operation had effects or consumed usage. Completed action
boundaries are resumable; uncertain operations are surfaced. Availability was
traded for understandable semantics in the first version.

**What did the experiment teach you?** A plausible reviewer finding can lead to
an incomplete repair, and finite acceptance tests can miss it. The first
contract accepted both candidates; five extra cases rejected both. Keep
evaluation versions separate instead of rewriting history.

**What broke in review?** A second review pass found that one erroring fixture
in an existing test suite collapsed the whole regression gate to "suite
inconclusive" and halved every score, and that plain unified diffs with
directory paths were rejected because git strips one path component by
default. Both got regression tests. Being able to talk about the bugs you found
in your own verifier is the point of building a verifier.

**What would you build next?** A GitHub Action around `bitswarm verify`, a
dependency image per target repository, a real-repository task corpus with
repeated and counterbalanced trials, a pinned second provider, and a routing
policy evaluated against solo and always-review baselines. External-process
oracles before any claim about hostile code.

## Two-minute demonstration

1. Open the README: the audit paragraph, the architecture, the JSON action interface.
2. Open the evidence dashboard; follow one patch to its per-test acceptance results.
3. Show the retry-delay reviewer finding in its trace, then the post-hoc audit.
4. Run `bitswarm replay` on a retained run: it rebuilds from baseline and patch,
   checks hashes, reports whether the environment still matches, and reruns the gates.
5. Explain the three tiers: scripted demo, live pilot, strengthened-contract
   follow-up. None establishes broad model superiority.

## Sharing status

Article text and resume bullets are drafts for personal editing. The evidence
contains only authored synthetic tasks; full working traces remain local.
Nothing has been posted or pushed by this work. Use the repository's real
public URL when sharing; do not invent a deployment link.
