"""Audit suite: wordle.game. Ground truth only; never shown to miners
and never used for scoring.

Guesses are drawn from the merged repo's own dictionary so the tests do
not depend on which words a particular word list happens to contain.
"""
import pytest

from wordle.game import Game
from wordle.types import Feedback, GameOverError, GameStatus, GuessResult, InvalidGuessError
from wordle.words import load_dictionary

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


WORDS = sorted(load_dictionary())
TARGET = WORDS[len(WORDS) // 2]
OTHERS = [w for w in WORDS if w != TARGET]


def test_new_game_state():
    g = Game(TARGET)
    assert g.get_status() == GameStatus.IN_PROGRESS
    assert g.get_attempts_remaining() == 6
    assert g.get_results() == []
    assert g.get_target_word() is None


def test_guess_feedback_matches_the_rule():
    g = Game(TARGET)
    for w in OTHERS[:5]:
        res = g.make_guess(w)
        assert isinstance(res, GuessResult)
        assert res.guess == w
        assert res.feedbacks == oracle(w, TARGET), w


def test_lowercase_guess_is_normalized():
    g = Game(TARGET)
    res = g.make_guess(OTHERS[0].lower())
    assert res.guess == OTHERS[0]
    assert g.get_attempts_remaining() == 5


@pytest.mark.parametrize("bad", ["ABC", "ABCDEFG", "12345", "ZZZZZ"])
def test_invalid_guess_raises_and_costs_nothing(bad):
    if bad in load_dictionary():
        pytest.skip("word happens to be in this dictionary")
    g = Game(TARGET)
    g.make_guess(OTHERS[0])
    with pytest.raises(InvalidGuessError):
        g.make_guess(bad)
    assert g.get_attempts_remaining() == 5
    assert len(g.get_results()) == 1
    assert g.get_status() == GameStatus.IN_PROGRESS


def test_win_reveals_target_and_ends_game():
    g = Game(TARGET)
    g.make_guess(OTHERS[0])
    res = g.make_guess(TARGET)
    assert res.feedbacks == [H] * 5
    assert g.get_status() == GameStatus.WON
    assert g.get_target_word() == TARGET
    with pytest.raises(GameOverError):
        g.make_guess(OTHERS[1])


def test_six_misses_lose_and_seventh_guess_is_rejected():
    g = Game(TARGET)
    for w in OTHERS[:6]:
        g.make_guess(w)
    assert g.get_status() == GameStatus.LOST
    assert g.get_attempts_remaining() == 0
    assert g.get_target_word() == TARGET
    with pytest.raises(GameOverError):
        g.make_guess(OTHERS[6])


def test_win_on_last_attempt_is_a_win():
    g = Game(TARGET)
    for w in OTHERS[:5]:
        g.make_guess(w)
    g.make_guess(TARGET)
    assert g.get_status() == GameStatus.WON


def test_results_accumulate_in_order():
    g = Game(TARGET)
    for w in OTHERS[:3]:
        g.make_guess(w)
    assert [x.guess for x in g.get_results()] == OTHERS[:3]


def test_reset_starts_a_fresh_game_with_new_target():
    g = Game(TARGET)
    g.make_guess(OTHERS[0])
    new = OTHERS[10]
    g.reset(new)
    assert g.get_status() == GameStatus.IN_PROGRESS
    assert g.get_attempts_remaining() == 6
    assert g.get_results() == []
    g.make_guess(new)
    assert g.get_status() == GameStatus.WON


def test_random_target_comes_from_dictionary():
    g = Game()
    for w in WORDS[:6]:
        g.make_guess(w)
        if g.get_status() != GameStatus.IN_PROGRESS:
            break
    assert g.get_target_word() in load_dictionary()
