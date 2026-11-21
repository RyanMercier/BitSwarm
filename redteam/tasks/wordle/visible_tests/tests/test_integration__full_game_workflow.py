"""Integration tests for cross-subtask interaction."""

import pytest

from wordle.game import Game

from wordle.words import load_dictionary, is_valid_word, get_random_word

from wordle.scorer import score_guess

from wordle.renderer import render_board, render_guess

from wordle.cli import play_game

from wordle.types import Feedback, GameStatus


@pytest.mark.xfail(raises=NotImplementedError, strict=False)
def test_full_game_workflow():
    """Test complete game workflow from start to finish."""
    # Initialize game with known target
    game = Game("HELLO")

    # Make a guess and get result
    result = game.make_guess("WORLD")

    # Verify result structure
    assert result.guess == "WORLD"
    assert len(result.feedbacks) == 5
    assert all(isinstance(f, Feedback) for f in result.feedbacks)

    # Render the guess
    rendered = render_guess(result)
    assert isinstance(rendered, str)

    # Check game state
    status = game.get_status()
    assert status == GameStatus.IN_PROGRESS

    # Win the game
    win_result = game.make_guess("HELLO")
    assert game.get_status() == GameStatus.WON
    assert game.get_target_word() == "HELLO"
