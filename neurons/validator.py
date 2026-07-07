"""
BitSwarm validator neuron (Bittensor transport).

The validator's job, per loop iteration:

  1. Sync the metagraph.
  2. Poll the task inbox (a directory of *.json task files; the
     simplest testnet intake that requires no extra service). Each
     task file: {"spec": "...", "target_repo": "/path", "mode":
     "scaffold"|"diff"}.
  3. For a claimed task: decompose, scaffold (holdback split + commit
     happens inside the scaffolder), discover available miners via
     StatusSynapse, dispatch one TaskSynapse per subtask via
     dendrite, adapt responses, run the merge + gates, score.
  4. Record per-HOTKEY scores into the ScoreBook (rolling EMA).
  5. At each tempo boundary, submit the normalized weight vector.

Usage (full runbook in docs/TESTNET.md):

    python -m neurons.validator \
        --netuid 999 \
        --subtensor.network test \
        --wallet.name bitswarm_validator \
        --wallet.hotkey default \
        --inbox ./task_inbox \
        --output ./validator_runs

COORDINATOR_BACKEND selects the decomposition backend exactly as in
the local pipelines (sdk / claude_code).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

try:
    import bittensor as bt
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "neurons/validator.py requires the bittensor package: "
        "pip install bittensor"
    ) from exc

from protocol.synapses import StatusSynapse, TaskSynapse
from validator.weights import ScoreBook, submit_weights


class RemoteMinerResult:
    """Adapter shape merge_and_test expects (duck-typed MinerResult)."""

    def __init__(self, subtask_id, patch="", tests_passed=False,
                 test_output="", iterations_used=0, stop_reason="",
                 files_modified=None, hotkey=""):
        self.subtask_id = subtask_id
        self.patch = patch
        self.tests_passed = tests_passed
        self.test_output = test_output
        self.iterations_used = iterations_used
        self.stop_reason = stop_reason
        self.files_modified = files_modified or []
        self.merge_conflict = False
        self.hotkey = hotkey


def parse_args() -> "bt.Config":
    parser = argparse.ArgumentParser(description="BitSwarm validator neuron")
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--inbox", type=str, default="./task_inbox",
                         help="directory polled for *.json task files")
    parser.add_argument("--output", type=str, default="./validator_runs",
                         help="per-task working directories land here")
    parser.add_argument("--tempo-seconds", type=int, default=4320,
                         help="seconds between weight submissions (~360 blocks)")
    parser.add_argument("--poll-seconds", type=int, default=15)
    bt.wallet.add_args(parser)
    bt.subtensor.add_args(parser)
    bt.logging.add_args(parser)
    return bt.config(parser)


class ValidatorNeuron:
    def __init__(self, config: "bt.Config"):
        self.config = config
        self.wallet = bt.wallet(config=config)
        self.subtensor = bt.subtensor(config=config)
        self.metagraph = self.subtensor.metagraph(config.netuid)
        self.dendrite = bt.dendrite(wallet=self.wallet)
        self.scorebook = ScoreBook(
            path=os.path.join(config.output, "scorebook.json"))
        self.last_weights_at = 0.0
        os.makedirs(config.inbox, exist_ok=True)
        os.makedirs(config.output, exist_ok=True)

        my_hotkey = self.wallet.hotkey.ss58_address
        if my_hotkey not in self.metagraph.hotkeys:
            raise SystemExit(
                f"validator hotkey {my_hotkey} is not registered on netuid "
                f"{config.netuid}; see docs/TESTNET.md"
            )
        bt.logging.info(f"validator hotkey={my_hotkey} "
                         f"netuid={config.netuid}")

    # --- miner discovery ---------------------------------------------

    async def available_miners(self) -> list[int]:
        """UIDs answering StatusSynapse with available=True."""
        axons = [self.metagraph.axons[uid]
                 for uid in range(len(self.metagraph.hotkeys))]
        responses = await self.dendrite(
            axons=axons,
            synapse=StatusSynapse(validator_id=self.wallet.hotkey.ss58_address),
            timeout=8,
        )
        free = [uid for uid, r in enumerate(responses)
                if getattr(r, "available", False)]
        bt.logging.info(f"{len(free)} miner(s) available: {free}")
        return free

    # --- task execution -----------------------------------------------

    async def run_one_task(self, task_file: str) -> None:
        from protocol.transport import bundle_repo
        from protocol.schemas import TaskAssignment
        from validator.decomposer import decompose
        from validator.merge import merge_and_test
        from validator.scaffolder import write_scaffolding
        from validator.validator_checks import validate_decomposition

        with open(task_file, encoding="utf-8") as f:
            spec_doc = json.load(f)
        spec = spec_doc["spec"]
        target = spec_doc["target_repo"]
        mode = spec_doc.get("mode", "scaffold")
        timeout = int(spec_doc.get("subtask_timeout", 1200))

        # Tasks submitted through validator/api.py carry their id, so
        # clients can correlate status polls with the result artifacts.
        task_id = spec_doc.get("task_id") or str(uuid.uuid4())
        out_dir = os.path.join(self.config.output, task_id)
        os.makedirs(out_dir, exist_ok=True)
        bt.logging.info(f"task {task_id}: mode={mode} target={target}")

        # Working repo
        repo_path = os.path.join(out_dir, "repo")
        shutil.copytree(target, repo_path, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "node_modules", ".venv", "venv"))
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "BitSwarm", "GIT_AUTHOR_EMAIL": "b@local",
               "GIT_COMMITTER_NAME": "BitSwarm", "GIT_COMMITTER_EMAIL": "b@local"}
        subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"],
                        cwd=repo_path, env=env, check=True)

        decomposition = decompose(
            repo_path=repo_path, feature_spec=spec,
            validate_fn=validate_decomposition if mode == "scaffold" else None,
            debug_dir=os.path.join(out_dir, "debug"), mode=mode,
        )
        if decomposition is None:
            bt.logging.error(f"task {task_id}: decomposition failed")
            return
        decomposition["task_id"] = task_id
        write_scaffolding(decomposition, repo_path)

        # Assignment build (mirrors validator/server.py's scoping rules)
        subtasks = decomposition["subtasks"]
        for st in subtasks:
            if mode == "diff":
                st["allowed_files"] = list(dict.fromkeys(
                    st.get("modify_files", []) or []))
            else:
                st["allowed_files"] = list(dict.fromkeys(
                    (st.get("stub_files", []) or [])
                    + (st.get("stub_test_files", []) or [])))
        all_subtask_files = {st["subtask_id"]: st["allowed_files"]
                             for st in subtasks}
        bundle = bundle_repo(repo_path)

        free_uids = await self.available_miners()
        if not free_uids:
            bt.logging.error(f"task {task_id}: no available miners; requeueing")
            return

        async def dispatch(subtask, uid) -> tuple[str, str, object]:
            assignment = TaskAssignment(
                task_id=task_id,
                subtask_id=subtask["subtask_id"],
                repo_bundle=bundle,
                subtask_description=subtask.get("description", ""),
                allowed_files=subtask["allowed_files"],
                stub_test_files=subtask.get("stub_test_files", []) or [],
                timeout_seconds=timeout,
                subtask_manifest=subtask,
                shared_files=decomposition.get("shared_files", {}) or {},
                all_subtask_files=all_subtask_files,
                stub_files_content=decomposition.get("stub_files", {}) or {},
                test_files_content=decomposition.get("stub_test_files", {}) or {},
                all_subtasks=subtasks,
                mode=mode,
                target_stubs=decomposition.get("target_stubs", {}) or {},
                new_test_files_content=decomposition.get("new_test_files", {}) or {},
                shared_additions_content=decomposition.get("shared_additions", {}) or {},
            )
            synapse = TaskSynapse.from_assignment(assignment)
            hotkey = self.metagraph.hotkeys[uid]
            result = await self.dendrite(
                axons=[self.metagraph.axons[uid]],
                synapse=synapse,
                timeout=timeout + 60,
            )
            return subtask["subtask_id"], hotkey, result[0]

        pairs = await asyncio.gather(*[
            dispatch(st, free_uids[i % len(free_uids)])
            for i, st in enumerate(subtasks)
        ])

        miner_results = {}
        hotkey_of = {}
        for sid, hotkey, syn in pairs:
            hotkey_of[sid] = hotkey
            miner_results[sid] = RemoteMinerResult(
                subtask_id=sid,
                patch=getattr(syn, "patch", "") or "",
                tests_passed=bool(getattr(syn, "stub_tests_passed", False)),
                test_output=getattr(syn, "stub_test_output", "") or "",
                iterations_used=int(getattr(syn, "iterations_used", 0) or 0),
                stop_reason=getattr(syn, "stop_reason", "") or "",
                files_modified=list(getattr(syn, "files_modified", []) or []),
                hotkey=hotkey,
            )

        merge_result = await merge_and_test(
            decomposition, miner_results, repo_path)

        # Per-HOTKEY score recording (a hotkey may own several subtasks).
        per_hotkey: dict[str, float] = {}
        for sid, score in merge_result["scores"].items():
            hk = hotkey_of.get(sid, "")
            if hk:
                per_hotkey[hk] = per_hotkey.get(hk, 0.0) + float(score)
        for hk, score in per_hotkey.items():
            self.scorebook.record(hk, min(1.0, score))
        total = sum(merge_result["scores"].values())
        bt.logging.info(f"task {task_id}: total={total:.3f} "
                         f"per-hotkey={per_hotkey}")

        # User deliverable: one unified diff of the whole verified
        # change, fetchable via GET /tasks/{id}/patch on the API.
        from validator.inbox import write_patch_artifact
        patch_written = False
        merge_repo = merge_result.get("merge_repo", "")
        if merge_repo and os.path.isdir(merge_repo):
            patch_written = write_patch_artifact(
                merge_repo, os.path.join(out_dir, "patch.diff"))

        with open(os.path.join(out_dir, "result.json"), "w") as f:
            json.dump({
                "task_id": task_id,
                "scores": merge_result["scores"],
                "total": total,
                "per_hotkey": per_hotkey,
                "integration_passed": merge_result["integration_passed"],
                "holdback_commit": decomposition.get("holdback_commit", ""),
                "patch_file": "patch.diff" if patch_written else "",
            }, f, indent=2)

    # --- main loop ------------------------------------------------------

    def maybe_submit_weights(self) -> None:
        if time.time() - self.last_weights_at < self.config.tempo_seconds:
            return
        uids, weights = self.scorebook.weight_vector(
            list(self.metagraph.hotkeys))
        if submit_weights(self.subtensor, self.wallet,
                           self.config.netuid, uids, weights):
            self.last_weights_at = time.time()

    async def loop(self):
        from validator.inbox import recover_orphaned
        requeued = recover_orphaned(self.config.inbox)
        if requeued:
            bt.logging.warning(
                f"requeued {len(requeued)} task(s) stranded by a previous "
                f"crash: {requeued}")

        while True:
            try:
                self.metagraph.sync(subtensor=self.subtensor)
            except Exception as exc:
                bt.logging.warning(f"metagraph sync failed: {exc}")

            pending = sorted(
                f for f in os.listdir(self.config.inbox)
                if f.endswith(".json"))
            if pending:
                task_file = os.path.join(self.config.inbox, pending[0])
                claimed = task_file + ".working"
                os.rename(task_file, claimed)
                try:
                    await self.run_one_task(claimed)
                    os.rename(claimed, claimed.replace(".working", ".done"))
                except Exception as exc:
                    bt.logging.error(f"task failed: {exc}")
                    os.rename(claimed, claimed.replace(".working", ".failed"))

            self.maybe_submit_weights()
            await asyncio.sleep(self.config.poll_seconds)


def main():
    config = parse_args()
    bt.logging(config=config)
    neuron = ValidatorNeuron(config)
    asyncio.run(neuron.loop())


if __name__ == "__main__":
    main()
