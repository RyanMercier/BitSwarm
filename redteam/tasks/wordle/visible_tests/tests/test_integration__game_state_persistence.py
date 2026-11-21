"""Integration tests for cross-subtask interaction."""

import pytest

from wordle.game import Game

from wordle.words import load_dictionary, is_valid_word, get_random_word

from wordle.scorer import score_guess

from wordle.renderer import render_board, render_guess

from wordle.cli import play_game

from wordle.types import Feedback, GameStatus


@pytest.mark.xfail(raises=NotImplementedError, strict=False)
def test_game_state_persistence():
    """Test that game maintains state correctly across multiple guesses."""
    game = Game("TESTS")

    # Make multiple guesses
    result1 = game.make_guess("HELLO")
    result2 = game.make_guess("WORLD")
    result3 = game.make_guess("GREAT")

    # Check all results are stored
    all_results = game.get_results()
    assert len(all_results) == 3
    assert all_results[0].guess == "HELLO"
    assert all_results[1].guess == "WORLD"
    assert all_results[2].guess == "GREAT"

    # Check attempts remaining
    assert game.get_attempts_remaining() == 3

    # Render the board
    board = render_board(all_results)
    assert isinstance(board, str)
