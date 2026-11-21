# Red-team study: reproduce the pipeline end to end.
#
# Runs from the repo root. Everything that executes agent-written code
# uses the Docker sandbox (BITSWARM_SANDBOX=docker). The open-model runs
# need a vLLM endpoint at REDTEAM_VLLM_URL; the Claude runs need the
# `claude` CLI on PATH.

PY ?= uv run python
export BITSWARM_SANDBOX ?= docker
export BITSWARM_REPAIR_MODE = off

.PHONY: help smoke tasks attacks run elicit impossible protected label judge \
	analyze swarm human-labels fix-rerun clean

PROTECTED = imp_watched imp_unwatched imp_clinic imp_clinic_mundane \
	imp_clinic_reversed imp_ladder

help:
	@echo "targets: smoke tasks attacks run elicit impossible protected label judge"
	@echo "         analyze swarm human-labels fix-rerun"
	@echo "  smoke        - grade each frozen task's reference (expect 1.0) + empty patch (expect 0)"
	@echo "  tasks        - list the frozen tasks and their held-back counts"
	@echo "  attacks      - gate-mutation + manual attacks (Phase 2.4, Phase 3)"
	@echo "  run          - the six propensity conditions, with resume (Phase 4)"
	@echo "  elicit       - security_audit and test_adequacy (capability elicitation)"
	@echo "  impossible   - the impossible variant, test edits permitted"
	@echo "  protected    - impossible variant with read-only tests: oversight, dilemma, ladder"
	@echo "  label        - recompute auto labels (Phase 4.5)"
	@echo "  judge        - LLM judge over suspected + 20% sample (Phase 4.5)"
	@echo "  analyze      - every table in the write-up from runs.jsonl (Phase 6)"
	@echo "  swarm        - whole-swarm credit assignment test (repair runs on the claude CLI)"
	@echo "  human-labels - draw the blind human-label sample and its reading packet"
	@echo "  fix-rerun    - Phase 5 grader fix: before/after on the missing-module hole"

smoke:
	@for t in wordle click_enum minidb; do \
	  echo "== $$t reference (expect 1.0) =="; \
	  $(PY) -m redteam.grade --task $$t || exit 1; \
	done

tasks:
	@for t in redteam/tasks/*/task.json; do \
	  $(PY) -c "import json,sys; m=json.load(open('$$t')); \
print(m['task_id'], m['mode'], len(m['subtasks']), 'subtasks,', \
len(m['held_back']), 'held-back')"; \
	done

attacks:
	$(PY) -m redteam.attacks gate-mutation
	$(PY) -m redteam.attacks manual

run:
	$(PY) -m redteam.run $(ARGS)

elicit:
	$(PY) -m redteam.run --conditions security_audit test_adequacy $(ARGS)

impossible:
	$(PY) -m redteam.run --conditions impossible impossible_pressure $(ARGS)

# The ladder sends up to five follow-up messages in one growing conversation,
# so runs need a long limit (6 hours: Qwen ladder runs at concurrency 6
# outran 2 hours; see NOTES.md).
protected:
	$(PY) -m redteam.run --conditions $(PROTECTED) --timeout 21600 $(ARGS)

swarm:
	$(PY) -m redteam.swarm run
	$(PY) -m redteam.swarm report

human-labels:
	$(PY) -m redteam.human_labels sample
	$(PY) -m redteam.human_labels packet

label:
	$(PY) -m redteam.label relabel

judge:
	$(PY) -m redteam.judge run $(ARGS)
	$(PY) -m redteam.judge agreement

analyze:
	$(PY) -m redteam.analyze --md redteam/results/analysis.md

fix-rerun:
	@echo "BEFORE (original grader): a minidb repo missing its executor module"
	$(PY) -m redteam.grade --task minidb --empty executor | grep -E "executor |engine |cli |integration_ratio"
	@echo "AFTER (BITSWARM_STRICT_INTEGRATION_XFAIL=1):"
	BITSWARM_STRICT_INTEGRATION_XFAIL=1 $(PY) -m redteam.grade --task minidb --empty executor | grep -E "executor |engine |cli |integration_ratio"

clean:
	rm -rf redteam/results/work
