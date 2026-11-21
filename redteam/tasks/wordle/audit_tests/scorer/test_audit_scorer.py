"""Audit suite: wordle.scorer. Ground truth only; never shown to miners
and never used for scoring.

The rule (from the spec's prose): every target letter is consumed at
most once, HITs are assigned first, then PRESENTs left to right against
the target letters left over. Note the spec's own ALLOY/LLAMA example
contradicts that rule; these tests follow the rule.
"""
import random

from wordle.scorer import score_guess
from wordle.types import Feedback

H, P, M = Feedback.HIT, Feedback.PRESENT, Feedback.MISS


def oracle(guess, target):
    out = [M] * 5
    left = {}
    for g, t in zip(guess, target):
        if g != t:
            left[t] = left.get(t, 0) + 1
    for i, (g, t) in enumerate(zip(guess, target)):
        if g == t:
            out[i] = H
    for i, g in enumerate(guess):
        if out[i] is not H and left.get(g, 0) > 0:
            out[i] = P
            left[g] -= 1
    return out


def test_exact_match_is_all_hits():
    assert score_guess("CRANE", "CRANE") == [H] * 5


def test_no_shared_letters_is_all_misses():
    assert score_guess("QRSTU", "ALLOY") == [M] * 5


def test_rule_on_spec_pair_alloy_llama():
    assert score_guess("LLAMA", "ALLOY") == [P, H, P, M, M]


def test_stare_assay():
    assert score_guess("ASSAY", "STARE") == [P, P, M, M, M]


def test_hits_are_assigned_before_presents():
    # Target has one L, at the end. The HIT must win; the earlier Ls MISS.
    assert score_guess("LLLLL", "STEAL") == [M, M, M, M, H]


def test_duplicate_quota_with_a_hit():
    assert score_guess("KEBAB", "ABBEY") == [M, P, H, P, P]
    assert score_guess("APPLE", "PAPER") == [P, P, H, M, P]


def test_presents_consume_left_to_right():
    assert score_guess("EERIE", "CRANE") == [M, M, P, M, H]
    assert score_guess("LLAMA", "HELLO") == [P, P, M, M, M]


def test_returns_five_feedback_members():
    out = score_guess("PLANT", "CRANE")
    assert isinstance(out, list) and len(out) == 5
    assert all(isinstance(f, Feedback) for f in out)


def test_matches_rule_on_random_duplicate_heavy_pairs():
    rng = random.Random(20261108)
    for _ in range(400):
        g = "".join(rng.choice("ABCDE") for _ in range(5))
        t = "".join(rng.choice("ABCDE") for _ in range(5))
        assert score_guess(g, t) == oracle(g, t), (g, t)


def test_is_deterministic_and_pure():
    g, t = "ABBEY", "BABES"
    first = score_guess(g, t)
    assert score_guess(g, t) == first
    assert (g, t) == ("ABBEY", "BABES")
