"""
Per-miner score tracking and on-chain weight submission.

Design (from BITSWARM_SPEC section 6):

  - Every completed task yields a per-miner score in [0, 1]
    (complexity weight x gate results, computed by the merge
    pipeline).
  - Scores accumulate per HOTKEY into a rolling window (default 20
    tasks). The effective score is an exponential moving average over
    that window, so recent work dominates but a single lucky or
    unlucky task cannot swing a miner's weight.
  - At each tempo boundary the validator normalizes the effective
    scores across the active metagraph into a weight vector and
    submits it. Yuma Consensus aggregates across validators.

The ScoreBook persists to disk between runs so a validator restart
does not zero every miner's history. The chain call itself lives
behind ``submit_weights`` so everything else in this module is
testable without bittensor installed.
"""
from __future__ import annotations

import json
import os
import threading


DEFAULT_WINDOW = int(os.environ.get("BITSWARM_SCORE_WINDOW", "20"))
DEFAULT_ALPHA = float(os.environ.get("BITSWARM_EMA_ALPHA", "0.3"))


class ScoreBook:
    """Rolling per-hotkey task scores with EMA aggregation."""

    def __init__(self, path: str | None = None,
                 window: int = DEFAULT_WINDOW,
                 alpha: float = DEFAULT_ALPHA):
        self.path = path
        self.window = window
        self.alpha = alpha
        self._scores: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                self._scores = {k: [float(x) for x in v][-window:]
                                 for k, v in raw.items()}
            except (OSError, ValueError, TypeError):
                # A corrupt score file must not brick the validator;
                # start fresh and let persistence rebuild it.
                self._scores = {}

    def record(self, hotkey: str, score: float) -> None:
        """Append a task score for a hotkey, trimming to the window."""
        score = max(0.0, min(1.0, float(score)))
        with self._lock:
            history = self._scores.setdefault(hotkey, [])
            history.append(score)
            del history[:-self.window]
            self._persist()

    def effective(self, hotkey: str) -> float:
        """EMA over the hotkey's recorded window (0.0 if unseen).

        Computed oldest-to-newest so the most recent task has the
        highest influence: ema = alpha*score + (1-alpha)*ema.
        """
        history = self._scores.get(hotkey, [])
        if not history:
            return 0.0
        ema = history[0]
        for s in history[1:]:
            ema = self.alpha * s + (1 - self.alpha) * ema
        return ema

    def weight_vector(self, hotkeys: list[str]) -> tuple[list[int], list[float]]:
        """Normalized weights over the given metagraph hotkey list.

        Returns (uids, weights) covering only hotkeys with a nonzero
        effective score. An empty result means "no opinion yet";
        callers should skip submission rather than send zeros (an
        all-zero vector is rejected by the chain anyway).
        """
        raw = [(uid, self.effective(hk)) for uid, hk in enumerate(hotkeys)]
        nonzero = [(uid, s) for uid, s in raw if s > 0.0]
        if not nonzero:
            return [], []
        total = sum(s for _, s in nonzero)
        return ([uid for uid, _ in nonzero],
                [s / total for _, s in nonzero])

    def snapshot(self) -> dict[str, list[float]]:
        with self._lock:
            return {k: list(v) for k, v in self._scores.items()}

    def _persist(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._scores, f)
            os.replace(tmp, self.path)
        except OSError:
            pass


def submit_weights(subtensor, wallet, netuid: int,
                    uids: list[int], weights: list[float]) -> bool:
    """Submit the weight vector on chain. Returns True on acceptance.

    Thin by design: everything above this line is chain-free and unit
    tested; everything below is what testnet week exercises live.
    """
    if not uids:
        print("[weights] no scored miners yet; skipping submission")
        return False
    try:
        result = subtensor.set_weights(
            wallet=wallet,
            netuid=netuid,
            uids=uids,
            weights=weights,
            wait_for_inclusion=True,
        )
        # bittensor returns bool or (bool, msg) depending on version.
        ok = result[0] if isinstance(result, tuple) else bool(result)
        print(f"[weights] set_weights -> {'accepted' if ok else 'rejected'} "
              f"({len(uids)} uids)")
        return ok
    except Exception as exc:
        print(f"[weights] set_weights failed: {exc}")
        return False
