# I built a Bittensor subnet for AI coding agents. Then the pitch meeting changed the project.

BitSwarm started as a Bittensor subnet. The idea was cooperative code
generation: a coordinator decomposes a feature spec into scaffolded subtasks,
independent miners implement them in parallel without seeing each other's
work, and a validator merges the pieces, runs the tests, and sets on-chain
weights so miners are paid only for verified work.

It worked better than I expected. One generic Wordle spec ran end to end in
seven languages (Python, TypeScript, Java, C#, C, C++, Rust) with five perfect
scores in a single overnight Docker run. Diff mode added a new parameter type
to pallets/click, a real 45,000-line repository with over a thousand existing
tests, from a four-sentence spec, with zero new failures in the existing suite.
The protocol layer, testnet runbook, sandboxed validator, and multi-provider
miner backends were all in place.

Then I took it to Yuma Labs.

## What the meeting taught me

I went in pitching cooperative multi-agent generation as the product and the
Bittensor incentive gradient as the thing that made it work. The feedback did
not attack the engineering. Their biggest criticism was that there was no
clear path to monetization. Who pays? For what? Why would they pay a network
of anonymous miners instead of a vendor with a support contract? I had a
working system, a token mechanism, a seven-language demo, and no crisp answer
to who would write a check.

Working through that question changed what I thought the project was.

**Generation was not the sellable thing.** Every lab and every IDE was already
shipping agents that write code. A subnet that competes on generation is
competing with the people who train the models, and nobody outside the token
economy was going to pay for that. The part of BitSwarm that had an obvious
buyer was the hermetic verification harness: the same computation the miner
runs locally, the validator runs at scoring time, and any auditor can replay.
Every team adopting coding agents has the same unsolved question: who signs
off on the machine's work? That is a service, and I had been treating it as
plumbing.

**Verification also fits the mechanism better.** Bittensor rewards work that
is cheap to score deterministically. Code quality is subjective and needs a
model to judge it. A test run against a pinned baseline in a pinned
environment is deterministic, replayable, and byte-comparable between
validators.

**But I could not yet say what my verification signal was worth.** Perfect
scores on tests the same system wrote are a weak signal. Nothing in the
pipeline could tell me whether a second agent caught defects the first one
missed, or whether the tests were simply too easy to fail. You cannot sell
verification until you know what a verified patch actually proves, and I had
to be able to answer that on one machine before asking anyone to fund a
network.

So I pivoted. The subnet code is still in the repository and still documented,
but the project is now a local coding-agent harness with two jobs: verify any
patch against frozen tests with no model in the loop, and answer one
measurable question about agents: **does adding a reviewer improve accepted
patches enough to justify the extra calls, latency, and cost?**

## What the harness does

BitSwarm owns the execution loop. Models return structured JSON actions to
read, search, patch, run development tests, finish, or submit review findings.
The runtime checks the actual changed paths before applying a patch. Models do
not get a shell through that interface.

The reviewer receives the task, the candidate patch, and its own observations.
It does not receive the implementer's explanations, and it cannot modify files.
That removes one source of anchoring; it does not remove correlated blind spots
between two calls to the same model.

Finishing is separate from passing. An independent verifier runs baseline,
regression, and acceptance gates inside a network-less container. The
acceptance contract is frozen and withheld from the model's tools. Assertion
failures produce a failure. Missing reports, collection errors, and timeouts
produce an inconclusive result, never a pass.

SQLite records state and events transactionally. Runs pause and resume at
completed action boundaries. If a crash leaves an operation's effects
uncertain, the runtime refuses to silently repeat it. A saved patch can be
reconstructed against its baseline and verified again under the recorded
environment.

## My coding agents passed every test. Then I tested the reviewer's fix.

The first pilot looked clean. Fable 5.1 completed three authored Python tasks
in two configurations: a solo implementer, and an implementer with a separate
reviewer and one repair cycle. All six attempts passed their acceptance
contracts.

Then I inspected the reviewer's repair.

One task parsed HTTP Retry-After delays. The reviewer noticed that converting
a string with thousands of digits to an integer can raise. The implementer
repaired that case by returning the maximum delay whenever the conversion
failed. That sounds plausible for an enormous number. It is wrong for thousands
of zeroes, or thousands of leading zeroes followed by a one.

I added five numerical probes as a separate post-hoc audit. The solo candidate
failed four. The reviewed candidate failed two. Both had passed the original
contract. I kept those original results intact and versioned a stronger
contract for the follow-up rather than quietly changing the benchmark.

Two fresh attempts against the stronger contract, one reviewed and one solo,
both failed final verification. The extra cases stayed withheld from the
models and the task wording stayed the same. The harness now rejects that
failure class. It has not made the models reliably solve it.

That was the same lesson from the Yuma meeting, arriving a second time with
data attached: the success signal was too weak, and the interesting work was
in making it stronger.

## What container testing caught

I used Fable as a code-review helper on the harness itself. Its review found
that large JUnit reports could be lost because the verifier parsed a truncated
display log, and that Docker inspection, execution, and cleanup could use
different client settings. Both findings became fixes and regression tests.

Live Docker testing caught a problem that mocks missed: Snap Docker's private
temp directory turned an intended source mount into an empty directory. The
harness now supports a daemon-visible staging directory and uses bind mounts
that fail loudly when their source is missing.

A second review pass on the harness found two more. One erroring fixture in an
existing test suite collapsed the whole regression gate into "suite
inconclusive" and halved every score, because a per-test error had been
classified as an infrastructure failure. And plain unified diffs with
directory paths were rejected as out of scope, because git strips one leading
path component by default and nothing put the prefix back. Both are the kind
of bug that only shows up on a real repository, and both now have regression
tests.

Six container integration tests exercise nonroot execution, network denial,
read-only mounts, timeout handling, candidate mutation, collection errors,
assertion failures, and a report containing 500 test records. The focused
harness suite has 43 passing unit tests.

## What I can claim from the pilot

| Original contract | Accepted | Model calls | Median recorded time | CLI cost estimate |
|---|---:|---:|---:|---:|
| Solo Fable | 3/3 | 17 | 45.9 seconds | $2.339 total |
| Reviewed Fable | 3/3 | 29 | 78.1 seconds | $4.110 total |

Those dollar figures are reported usage estimates, not subscription invoices.
The conditions had equal maximum budgets, not equal actual consumption. Runs
were sequential in a fixed order on one workstation, with no statistical claim.

These are three small synthetic tasks and one model family. They do not show
that review is unnecessary, that collaboration is better, or that the harness
is ready to judge hostile submissions. Candidate code and pytest still share an
interpreter inside the container.

What the project demonstrates is an inspectable way to run the comparison, and
a way to discover when the original success signal was too weak.

## What the pivot cost and what it kept

The subnet work was not wasted. The hermetic patch-replay verification, the
scope checks on miner patches, the hidden-test commit-reveal, and the sandboxed
gate runner all carried over. Several scoring defects in that older code were
found and fixed during the harness work, and they now have regression tests.

What I gave up was the story I walked into the meeting with. What I got was a
project where every claim points at a saved patch, a trace, a hash, and a
verifier that anyone can rerun.

Will anyone use it? Honestly: the harness as it stands is a research
instrument. It runs on one host, supports stdlib Python, and expects tests in
one directory. The piece with a real audience is the verifier: hand it a
commit, a patch, an allow list, and a directory of acceptance tests, and it
returns a replayable verdict with no model involved. Wrapped in a CI action
with per-repository dependency images, that is a product a team adopting
coding agents would recognize. That is the path to monetization I did not
have in the meeting, and it is now the roadmap.

The next experiment needs a broader repository-task corpus, repeated trials,
and an explicit comparison between solo execution, always-on review, and
selective review. Before adding more agents, I want stronger evidence about
which failures an additional agent actually prevents. If that evidence holds
up, the subnet version has a much better product to sell than the one I
pitched.

The repository includes the runtime, reproducible task preparation, tests,
selected live traces, saved patches, and the post-hoc audit. The earlier
subnet code and its documentation are archived alongside it. I used coding
assistants during development and keep their contributions tied to reviewable
code and test results.

*Repository and evidence: add the public project URL when publishing. This
draft has not been posted.*
