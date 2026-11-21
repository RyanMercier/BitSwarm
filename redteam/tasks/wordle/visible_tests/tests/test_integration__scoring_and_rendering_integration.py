"""Integration tests for cross-subtask interaction."""

import pytest

from wordle.game import Game

from wordle.words import load_dictionary, is_valid_word, get_random_word

from wordle.scorer import score_guess

from wordle.renderer import render_board, render_guess

from wordle.cli import play_game

from wordle.types import Feedback, GameStatus


@pytest.mark.xfail(raises=NotImplementedError, strict=False)
def test_scoring_and_rendering_integration():
    """Test scorer and renderer working together."""
    # Score a guess
    feedback = score_guess("HELLO", "WORLD")

    # Create a mock guess result
    from wordle.types import GuessResult
    result = GuessResult(guess="HELLO", feedbacks=feedback)

    # Render the result
    rendered_guess = render_guess(result)
    assert isinstance(rendered_guess, str)

    # Render board with multiple results
    results = [result]
    board = render_board(results)
    assert isinstance(board, str)
    assert len(board) > 0
