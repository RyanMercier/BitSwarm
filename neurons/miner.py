"""
BitSwarm miner neuron (Bittensor axon transport).

Serves TaskSynapse and StatusSynapse on an axon, advertises the
endpoint on chain, and executes assignments through the same
miner/runtime.py the HTTP server uses. One task at a time, gated by
an asyncio.Lock; a busy miner answers a TaskSynapse immediately with
stop_reason="busy" so validators can reroute rather than block.

Usage (see docs/TESTNET.md for the full runbook):

    python -m neurons.miner \
        --netuid 999 \
        --subtensor.network test \
        --wallet.name bitswarm_miner \
        --wallet.hotkey default \
        --axon.port 8091

Environment: MINER_BACKEND selects the inference backend exactly as
in the HTTP deployment (sdk / claude_code / openai plus the
MINER_OPENAI_* provider vars).

Validator gating: requests are accepted only from hotkeys registered
on the subnet with at least BITSWARM_MIN_VALIDATOR_STAKE (default
1000 TAO on mainnet, 0 on testnet where stake is play money).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import threading
import time
import traceback

try:
    import bittensor as bt
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "neurons/miner.py requires the bittensor package: pip install bittensor"
    ) from exc

from miner.runtime import MINER_ID, run_assignment, select_backend
from protocol.synapses import StatusSynapse, TaskSynapse


def parse_args() -> "bt.Config":
    parser = argparse.ArgumentParser(description="BitSwarm miner neuron")
    parser.add_argument("--netuid", type=int, required=True,
                         help="subnet netuid to serve on")
    bt.wallet.add_args(parser)
    bt.subtensor.add_args(parser)
    bt.axon.add_args(parser)
    bt.logging.add_args(parser)
    return bt.config(parser)


class MinerNeuron:
    def __init__(self, config: "bt.Config"):
        self.config = config
        self.wallet = bt.wallet(config=config)
        self.subtensor = bt.subtensor(config=config)
        self.metagraph = self.subtensor.metagraph(config.netuid)
        self.execute_subtask = select_backend()
        self.lock = asyncio.Lock()
        self.current_task_id = ""
        self.min_validator_stake = float(
            os.environ.get("BITSWARM_MIN_VALIDATOR_STAKE", "0"))

        my_hotkey = self.wallet.hotkey.ss58_address
        if my_hotkey not in self.metagraph.hotkeys:
            raise SystemExit(
                f"hotkey {my_hotkey} is not registered on netuid "
                f"{config.netuid}. Run: btcli subnet register "
                f"--netuid {config.netuid} --subtensor.network "
                f"{self.subtensor.network} --wallet.name "
                f"{config.wallet.name} --wallet.hotkey {config.wallet.hotkey}"
            )
        self.uid = self.metagraph.hotkeys.index(my_hotkey)
        bt.logging.info(f"miner uid={self.uid} hotkey={my_hotkey} "
                         f"backend={os.environ.get('MINER_BACKEND', 'sdk')}")

        self.axon = bt.axon(wallet=self.wallet, config=config)
        self.axon.attach(
            forward_fn=self.forward_task,
            blacklist_fn=self.blacklist_task,
        ).attach(
            forward_fn=self.forward_status,
            blacklist_fn=self.blacklist_status,
        )

    # --- gating -------------------------------------------------------

    def _is_permitted(self, hotkey: str) -> tuple[bool, str]:
        if hotkey not in self.metagraph.hotkeys:
            return False, "hotkey not registered on subnet"
        uid = self.metagraph.hotkeys.index(hotkey)
        stake = float(self.metagraph.S[uid])
        if stake < self.min_validator_stake:
            return False, (f"stake {stake:.1f} below required "
                            f"{self.min_validator_stake:.1f}")
        return True, ""

    def blacklist_task(self, synapse: TaskSynapse) -> tuple[bool, str]:
        hotkey = synapse.dendrite.hotkey if synapse.dendrite else ""
        ok, why = self._is_permitted(hotkey)
        return (not ok), (why or "ok")

    def blacklist_status(self, synapse: StatusSynapse) -> tuple[bool, str]:
        hotkey = synapse.dendrite.hotkey if synapse.dendrite else ""
        ok, why = self._is_permitted(hotkey)
        return (not ok), (why or "ok")

    # --- handlers -----------------------------------------------------

    async def forward_status(self, synapse: StatusSynapse) -> StatusSynapse:
        synapse.available = not self.lock.locked()
        synapse.current_task_id = self.current_task_id
        synapse.miner_id = MINER_ID
        synapse.backend = os.environ.get("MINER_BACKEND", "sdk")
        return synapse

    async def forward_task(self, synapse: TaskSynapse) -> TaskSynapse:
        # Non-blocking single-task gate: a busy miner reports busy
        # rather than queueing (queues hide capacity from validators).
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=0)
        except asyncio.TimeoutError:
            synapse.stop_reason = "busy"
            synapse.error_message = "miner busy with another subtask"
            return synapse

        try:
            self.current_task_id = synapse.task_id or synapse.subtask_id
            assignment = synapse.to_assignment()
            try:
                response = await run_assignment(assignment, self.execute_subtask)
                synapse.fill_response(response)
            except Exception as exc:
                synapse.stop_reason = "error"
                synapse.error_message = (
                    f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                )
            return synapse
        finally:
            self.current_task_id = ""
            self.lock.release()

    # --- lifecycle ----------------------------------------------------

    def run(self):
        self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
        self.axon.start()
        bt.logging.info(
            f"axon serving on {self.axon.external_ip}:{self.axon.external_port}"
        )
        # Periodic metagraph resync so validator-set changes (new
        # registrations, stake moves) reflect in gating.
        stop = threading.Event()
        try:
            while not stop.is_set():
                time.sleep(60)
                try:
                    self.metagraph.sync(subtensor=self.subtensor)
                except Exception as exc:
                    bt.logging.warning(f"metagraph sync failed: {exc}")
        except KeyboardInterrupt:
            bt.logging.info("shutting down")
        finally:
            self.axon.stop()


def main():
    config = parse_args()
    bt.logging(config=config)
    MinerNeuron(config).run()


if __name__ == "__main__":
    main()
