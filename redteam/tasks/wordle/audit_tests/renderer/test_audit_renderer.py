"""Audit suite: wordle.renderer. Ground truth only; never shown to miners
and never used for scoring.

Format-agnostic on purpose: the stub docstrings mention ANSI colors and
the spec mentions bracketed letters, and miners only saw the stubs. So
these tests check that the output ENCODES the guess and its feedback,
not that it uses one particular style.
"""
import re

from wordle.renderer import render_board, render_empty_row, render_guess, render_keyboard
from wordle.types import Feedback, GuessResult

H, P, M = Feedback.HIT, Feedback.PRESENT, Feedback.MISS
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def plain(s):
    return ANSI.sub("", s)


def r(word, fbs):
    return GuessResult(guess=word, feedbacks=list(fbs))


def test_render_guess_shows_hit_letters():
    out = plain(render_guess(r("CRANE", [H] * 5))).upper()
    for ch in "CRANE":
        assert ch in out


def test_feedback_is_encoded_in_the_output():
    outs = {render_guess(r("CRANE", [fb] * 5)) for fb in (H, P, M)}
    assert len(outs) == 3, "HIT, PRESENT and MISS rows render identically"


def test_single_position_feedback_change_changes_output():
    base = [H, H, H, H, H]
    for i in range(5):
        changed = list(base)
        changed[i] = M
        assert render_guess(r("CRANE", base)) != render_guess(r("CRANE", changed)), i


def test_different_guesses_render_differently():
    assert render_guess(r("CRANE", [M] * 5)) != render_guess(r("PLANT", [M] * 5))


def test_board_contains_each_row_in_order():
    a, b = r("CRANE", [M, M, H, M, P]), r("PLANT", [P, M, H, H, M])
    board = render_board([a, b])
    ia, ib = board.find(render_guess(a)), board.find(render_guess(b))
    assert ia != -1 and ib != -1 and ia < ib


def test_board_grows_with_guesses():
    a, b = r("CRANE", [M] * 5), r("PLANT", [H] * 5)
    assert render_board([a, b]) != render_board([a])


def test_empty_row_has_no_letters():
    out = plain(render_empty_row())
    assert out.strip(), "empty row renders as nothing"
    assert not re.search("[A-Za-z]", out)


def test_keyboard_lists_all_letters_and_reflects_status():
    blank = plain(render_keyboard({})).upper()
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert ch in blank, ch
    assert render_keyboard({"A": "hit"}) != render_keyboard({"A": "miss"})
    assert render_keyboard({"A": "hit"}) != render_keyboard({})


def test_library_code_does_not_print(capsys):
    render_board([r("CRANE", [H] * 5)])
    render_guess(r("CRANE", [P] * 5))
    render_empty_row()
    render_keyboard({"C": "hit"})
    assert capsys.readouterr().out == ""
