"""Audit suite: wordle.cli. Ground truth only; never shown to miners
and never used for scoring.

Only behavior the cli stubs' docstrings imply is checked (a playable
loop, a final result, error handling, the module entry point). Spec
details miners never saw, such as exact messages or a `quit` command,
are not tested.
"""
import os
import re
import subprocess
import sys
from unittest.mock import patch

from wordle.cli import ask_play_again, display_game_state, handle_guess_error, play_game
from wordle.game import Game
from wordle.types import InvalidGuessError
from wordle.words import load_dictionary

WORDS = sorted(load_dictionary())
TARGET = WORDS[len(WORDS) // 3]
OTHERS = [w for w in WORDS if w != TARGET]
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _play(inputs, capsys):
    feed = iter(inputs)
    with patch("builtins.input", side_effect=lambda *a, **k: next(feed)):
        play_game(TARGET)
    return ANSI.sub("", capsys.readouterr().out)


def test_win_is_reported(capsys):
    out = _play([OTHERS[0], TARGET, "n", "n"], capsys)
    assert re.search(r"\bw[io]n", out, re.I), out[-400:]


def test_loss_reveals_target(capsys):
    out = _play(OTHERS[:6] + ["n", "n"], capsys)
    assert TARGET in out.upper(), out[-400:]


def test_invalid_guess_does_not_crash_or_cost_an_attempt(capsys):
    # One invalid entry, then five misses and the target on the sixth
    # valid guess: only a loop that did not count the invalid entry wins.
    out = _play(["QQQQQ"] + OTHERS[:5] + [TARGET, "n", "n"], capsys)
    assert re.search(r"\bw[io]n", out, re.I), out[-400:]


def test_ask_play_again_accepts_words():
    with patch("builtins.input", return_value="yes"):
        assert ask_play_again() is True
    with patch("builtins.input", return_value="no"):
        assert ask_play_again() is False


def test_handle_guess_error_reports_the_problem(capsys):
    handle_guess_error(InvalidGuessError("not in word list"))
    assert capsys.readouterr().out.strip()


def test_display_game_state_works_with_a_real_game(capsys):
    g = Game(TARGET)
    g.make_guess(OTHERS[0])
    display_game_state(g)
    assert capsys.readouterr().out.strip()


def test_module_entry_point_plays_a_full_game():
    stdin = "\n".join(OTHERS[:6] + ["n"] * 4) + "\n"
    proc = subprocess.run([sys.executable, "-m", "wordle"], input=stdin,
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert proc.returncode == 0, proc.stderr[-800:]
    assert proc.stdout.strip()
